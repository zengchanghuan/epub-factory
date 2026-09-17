#!/usr/bin/env python3
"""Publish one reviewed EPUB with hash guards, final QA and server-local backups.

Dry-run by default. Does not submit translations, alter payment/authentication,
change model-call counters, or clear caches. Run using the server virtualenv.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from app.domain.translation_qa_service import attach_translation_qa_report

spec = importlib.util.spec_from_file_location('offline_book_review', ROOT / 'scripts/audit-translated-book.py')
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare_stats(previous, report, preserved_terms):
    counts = report['counts']
    if (report['alignment_failures'] or counts.get('fail', 0)
            or not report['expected_paired_chunks']
            or counts.get('paired_chunks') != report['expected_paired_chunks']
            or report['artifact_audit']['status'] != 'passed'):
        raise ValueError('最终正文/目录/定位质检未通过，拒绝发布')
    stats = dict(previous)
    history = {k: stats.get(k) for k in ('audit_failed_chunks', 'audit_warn_chunks', 'audit_flags_count',
                                       'audit_examples', 'artifact_audit', 'qa_report')}
    stats.update(audit_failed_chunks=0, audit_warn_chunks=counts.get('warn', 0),
                 audit_flags_count={flag: value for flag, value in counts.items()
                                    if flag not in {'paired_chunks', 'ok', 'warn', 'fail'}},
                 audit_examples=report['findings'][:20], artifact_audit=report['artifact_audit'],
                 reviewed_revision={'mode': report['mode'], 'rules_version': report['rules_version'],
                                    'source_sha256': report['source_sha256'], 'output_sha256': report['target_sha256'],
                                    'paired_chunks': counts['paired_chunks'], 'confirmed_preserved_terms': preserved_terms,
                                    'previous_execution_audit': history,
                                    'reviewed_at': datetime.now(timezone.utc).isoformat()})
    return stats


def publish(args):
    root, db, candidate = args.project_root.resolve(), args.database.resolve(), args.candidate.resolve()
    if not db.is_file() or not candidate.is_file(): raise ValueError('数据库或候选文件不存在')
    if digest(candidate) != args.candidate_sha256: raise ValueError('候选文件版本不符')
    with (root / '.deploy.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute('SELECT * FROM epub_jobs WHERE id=?', (args.job_id,)).fetchone()
            if row is None: raise ValueError('订单不存在')
            row = dict(row)
            if connection.execute("SELECT COUNT(*) FROM epub_jobs WHERE status IN ('pending','running')").fetchone()[0]:
                raise ValueError('有排队或运行中的任务，拒绝发布')
        if row['status'] != 'success' or not row['enable_translation'] or row['bilingual']:
            raise ValueError('仅支持已成功的单语翻译订单修订')
        source, old_output = Path(row['input_path']), Path(row['output_path'])
        previous = json.loads(row['translation_stats_json'] or '{}')
        if digest(source) != args.source_sha256: raise ValueError('原书版本不符')
        if (digest(old_output) == args.candidate_sha256
                and previous.get('reviewed_revision', {}).get('output_sha256') == args.candidate_sha256
                and previous.get('qa_report', {}).get('can_deliver')):
            return {'already_published': True, 'output_sha256': args.candidate_sha256}
        if digest(old_output) != args.previous_sha256: raise ValueError('线上旧译本版本已变化')
        output_dir = root / 'backend/outputs'
        if old_output.resolve().parent != output_dir.resolve(): raise ValueError('订单输出不在受控目录')
        glossary = json.loads(row['glossary_json'] or '{}')
        glossary.update({term: term for term in args.preserve_term})
        report = review.audit_book(source, candidate, glossary, include_chunks=True)
        if report['source_sha256'] != args.source_sha256 or report['target_sha256'] != args.candidate_sha256:
            raise ValueError('复核期间文件版本已变化')
        stats = prepare_stats(previous, report, args.preserve_term)
        stats = attach_translation_qa_report(stats, output_path=candidate, error_code=row['error_code'])
        if not stats['qa_report']['can_deliver']: raise ValueError('聚合交付质检未通过')
        check = subprocess.run(['java', '-jar', str(args.epubcheck_jar), str(candidate), '--quiet'],
                               capture_output=True, text=True, timeout=120)
        if check.returncode: raise ValueError('EPUBCheck 未通过：' + (check.stdout + check.stderr)[-1000:])
        if digest(source) != args.source_sha256 or digest(candidate) != args.candidate_sha256:
            raise ValueError('校验期间文件版本已变化')
        summary = {'job_id': args.job_id, 'apply': args.apply, 'output_sha256': args.candidate_sha256,
                   'paired_chunks': report['counts']['paired_chunks'], 'qa_report': stats['qa_report']}
        if not args.apply: return summary
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup = root / 'deploy-backups' / f'{stamp}-book-{args.job_id}-{os.getpid()}'
        backup.mkdir(mode=0o700, parents=True, exist_ok=False)
        backup.chmod(0o700)
        with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as source_db, sqlite3.connect(backup/'jobs.sqlite3') as target_db:
            source_db.backup(target_db)
            if target_db.execute('PRAGMA quick_check').fetchone()[0] != 'ok': raise ValueError('数据库备份校验失败')
        (backup/'jobs.sqlite3').chmod(0o600)
        shutil.copy2(old_output, backup/'previous.epub')
        (backup/'previous.epub').chmod(0o600)
        (backup/'review.json').write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
        (backup/'review.json').chmod(0o600)
        output = output_dir / f'{old_output.stem}_修正版_{args.candidate_sha256[:8]}.epub'
        if output.exists() and digest(output) != args.candidate_sha256: raise ValueError('目标文件已存在且内容不同')
        if not output.exists():
            fd, temporary = tempfile.mkstemp(prefix='.reviewed-', suffix='.epub', dir=output_dir)
            try:
                with os.fdopen(fd, 'wb') as file:
                    file.write(candidate.read_bytes()); file.flush(); os.fsync(file.fileno())
                if digest(temporary) != args.candidate_sha256: raise ValueError('写入校验失败')
                os.replace(temporary, output)
                directory_fd = os.open(output_dir, os.O_RDONLY | os.O_DIRECTORY)
                try: os.fsync(directory_fd)
                finally: os.close(directory_fd)
            finally:
                Path(temporary).unlink(missing_ok=True)
        final = {a['chunk_id']: a for a in report['chunk_audits']}
        with sqlite3.connect(db) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute('BEGIN IMMEDIATE')
            latest = connection.execute('SELECT * FROM epub_jobs WHERE id=?', (args.job_id,)).fetchone()
            if (dict(latest) != row or digest(old_output) != args.previous_sha256 or digest(output) != args.candidate_sha256
                    or digest(source) != args.source_sha256 or digest(candidate) != args.candidate_sha256):
                raise ValueError('订单或文件已变化；拒绝更新指针')
            if connection.execute("SELECT COUNT(*) FROM epub_jobs WHERE status IN ('pending','running')").fetchone()[0]:
                raise ValueError('有新任务进入；拒绝更新指针')
            for chunk in connection.execute('SELECT id,chunk_id,audit_json FROM job_chunks WHERE job_id=?', (args.job_id,)).fetchall():
                old = json.loads(chunk['audit_json'] or '{}')
                if old.get('risk_level') != 'fail': continue
                current = final.get(chunk['chunk_id'])
                if not current or current['risk_level'] == 'fail': raise ValueError('旧失败明细缺少有效的最终复核')
                current = {**current, 'review_scope': 'published_epub', 'previous_execution_audit': old}
                connection.execute('UPDATE job_chunks SET audit_json=? WHERE id=? AND job_id=?',
                                   (json.dumps(current, ensure_ascii=False), chunk['id'], args.job_id))
            stats = attach_translation_qa_report(stats, output_path=output, error_code=row['error_code'])
            connection.execute('UPDATE epub_jobs SET output_path=?,glossary_json=?,translation_stats_json=?,message=?,updated_at=? WHERE id=?',
                (str(output), json.dumps(glossary, ensure_ascii=False), json.dumps(stats, ensure_ascii=False),
                 '修正版已交付；英文姓名按用户确认保留，辅助复核提示不等于确定错误。',
                 datetime.now(timezone.utc).isoformat(), args.job_id))
        summary.update(output=str(output), backup=str(backup))
        return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, default=ROOT)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--job-id', required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    for name in ('source', 'previous', 'candidate'):
        parser.add_argument(f'--{name}-sha256', required=True)
    parser.add_argument('--preserve-term', action='append', default=[])
    parser.add_argument('--epubcheck-jar', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    print(json.dumps(publish(parser.parse_args()), ensure_ascii=False, indent=2))
