"""Isolated publication regressions; no production, network or model calls."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from ebooklib import epub

spec = importlib.util.spec_from_file_location('publish_reviewed', Path(__file__).with_name('publish-reviewed-book.py'))
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class PublishReviewedBookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        output_dir = self.root/'backend/outputs'; output_dir.mkdir(parents=True)
        self.source, self.candidate, self.old = self.root/'source.epub', self.root/'candidate.epub', output_dir/'old.epub'
        for path, translated in [(self.source, False), (self.candidate, True)]:
            book = epub.EpubBook(); book.set_identifier('offline-fixture')
            book.set_title('人物简介' if translated else 'Profile'); book.set_language('zh-CN' if translated else 'en')
            chapter = epub.EpubHtml(title='KELVIN CHIU', file_name='chapter.xhtml', uid='chapter')
            chapter.content = '<h2>KELVIN CHIU</h2><p>' + ('Kelvin Chiu 是一位交易者。' if translated else 'Kelvin Chiu is a trader.') + '</p>'
            book.add_item(chapter); book.toc = [epub.Link('chapter.xhtml', 'KELVIN CHIU', 'one')]
            book.add_item(epub.EpubNcx()); book.add_item(epub.EpubNav()); book.spine = [chapter]
            epub.write_epub(str(path), book)
        shutil.copy2(self.source, self.old)
        self.db = self.root/'jobs.db'
        previous = {'failed_chunks':0, 'audit_failed_chunks':1, 'total_chunks':2, 'audit_warn_chunks':0,
                    'api_calls':9, 'translated_chunks':2, 'translation_attempt':3, 'model':'historical-model',
                    'book_title_original':'Profile', 'book_title_translated':'人物简介'}
        with sqlite3.connect(self.db) as c:
            c.execute('CREATE TABLE epub_jobs (id TEXT PRIMARY KEY,status TEXT,enable_translation INTEGER,bilingual INTEGER,input_path TEXT,output_path TEXT,translation_stats_json TEXT,glossary_json TEXT,error_code TEXT,message TEXT,updated_at TEXT,access_token TEXT,expected_amount TEXT)')
            c.execute('INSERT INTO epub_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('fixture','success',1,0,str(self.source),str(self.old),json.dumps(previous),'{}',None,'old','old','unchanged-auth','12.00'))
            c.execute('CREATE TABLE job_chunks (id TEXT PRIMARY KEY,job_id TEXT,chunk_id TEXT,audit_json TEXT)')
            c.execute('INSERT INTO job_chunks VALUES (?,?,?,?)', ('one','fixture','chapter_0001',json.dumps({'risk_level':'fail','flags':['likely_untranslated']})))
        self.args = argparse.Namespace(project_root=self.root,database=self.db,job_id='fixture',candidate=self.candidate,
            candidate_sha256=publisher.digest(self.candidate),source_sha256=publisher.digest(self.source),
            previous_sha256=publisher.digest(self.old),preserve_term=['KELVIN CHIU'],epubcheck_jar=self.root/'stub.jar',apply=False)
        self.check = patch.object(publisher.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='',stderr=''))
        self.check.start()

    def tearDown(self):
        self.check.stop(); self.tmp.cleanup()

    def job(self):
        with sqlite3.connect(self.db) as c:
            c.row_factory = sqlite3.Row
            return dict(c.execute('SELECT * FROM epub_jobs WHERE id="fixture"').fetchone())

    def test_dry_run_never_changes_order_or_creates_output(self):
        before = self.job(); result = publisher.publish(self.args)
        self.assertTrue(result['qa_report']['can_deliver'])
        self.assertEqual(before,self.job()); self.assertEqual(len(list(self.old.parent.glob('*.epub'))),1)
        self.assertFalse((self.root/'deploy-backups').exists())

    def test_apply_preserves_auth_payment_model_counters_and_old_file(self):
        self.args.apply=True; before=self.job(); result=publisher.publish(self.args); after=self.job()
        for key in ('id','status','access_token','expected_amount','input_path','enable_translation','bilingual'):
            self.assertEqual(before[key],after[key])
        stats=json.loads(after['translation_stats_json']); old=json.loads(before['translation_stats_json'])
        for key in ('api_calls','translated_chunks','translation_attempt','model','total_chunks'):
            self.assertEqual(old[key],stats[key])
        self.assertEqual(json.loads(after['glossary_json']),{'KELVIN CHIU':'KELVIN CHIU'})
        self.assertTrue(stats['qa_report']['can_deliver']); self.assertEqual(stats['audit_failed_chunks'],0)
        self.assertEqual(publisher.digest(after['output_path']),self.args.candidate_sha256)
        self.assertEqual(publisher.digest(self.old),self.args.previous_sha256)
        self.assertEqual(publisher.digest(Path(result['backup'])/'previous.epub'),self.args.previous_sha256)
        with sqlite3.connect(self.db) as c:
            audit=json.loads(c.execute('SELECT audit_json FROM job_chunks').fetchone()[0])
        self.assertEqual(audit['risk_level'],'ok'); self.assertEqual(audit['previous_execution_audit']['risk_level'],'fail')
        self.assertTrue(publisher.publish(self.args)['already_published'])

    def test_wrong_hashes_and_missing_confirmation_fail_closed(self):
        before=self.job()
        for key in ('source_sha256','previous_sha256','candidate_sha256'):
            value=getattr(self.args,key); setattr(self.args,key,'0'*64)
            with self.assertRaises(ValueError): publisher.publish(self.args)
            setattr(self.args,key,value)
        self.args.preserve_term=[]
        with self.assertRaises(ValueError): publisher.publish(self.args)
        self.assertEqual(before,self.job())

    def test_running_job_and_epubcheck_failure_prevent_publication(self):
        with sqlite3.connect(self.db) as c: c.execute('UPDATE epub_jobs SET status="running"')
        with self.assertRaises(ValueError): publisher.publish(self.args)
        with sqlite3.connect(self.db) as c: c.execute('UPDATE epub_jobs SET status="success"')
        with patch.object(publisher.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='invalid',stderr='')):
            with self.assertRaises(ValueError): publisher.publish(self.args)
        self.assertEqual(self.job()['output_path'],str(self.old))

    def test_order_drift_after_review_prevents_pointer_update(self):
        self.args.apply=True
        original=publisher.prepare_stats
        def drift(*args):
            result=original(*args)
            with sqlite3.connect(self.db) as c: c.execute('UPDATE epub_jobs SET updated_at="other-writer"')
            return result
        with patch.object(publisher,'prepare_stats',side_effect=drift):
            with self.assertRaises(ValueError): publisher.publish(self.args)
        self.assertEqual(self.job()['output_path'],str(self.old))

    def test_source_or_candidate_drift_during_review_prevents_publication(self):
        self.args.apply=True
        original=publisher.prepare_stats
        for path in (self.source,self.candidate):
            before=path.read_bytes()
            def drift(*args):
                result=original(*args)
                path.write_bytes(b'changed-version')
                return result
            with patch.object(publisher,'prepare_stats',side_effect=drift):
                with self.assertRaises(ValueError): publisher.publish(self.args)
            path.write_bytes(before)
            self.assertEqual(self.job()['output_path'],str(self.old))


if __name__=='__main__': unittest.main()
