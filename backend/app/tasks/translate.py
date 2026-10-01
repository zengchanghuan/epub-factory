"""
章节级翻译 Celery 任务：接收 job_id + chapter_id，在 Worker 内并发翻译该章所有 chunk。
"""

from app.infra.celery_app import celery_app
from app.domain.chapter_translation_service import translate_chapter, assert_chapter_execution
from app.domain.book_reduce_service import set_chapter_output, make_get_chapter_content
from app.domain.job_write_fence import job_write_scope, JobWriteConflict


@celery_app.task(name="jobs.translate_chapter")
def translate_chapter_task(job_id: str, chapter_id: str, expected_attempt_id: str | None = None,
                           expected_execution_owner: str | None = None) -> dict:
    """
    在 Celery Worker 中执行单章翻译，章节内 chunk 并发。
    若有 reduced_html 则写入 reduce_work 供全书打包使用。
    """
    if not expected_attempt_id or not expected_execution_owner:
        raise JobWriteConflict('Chapter task requires a captured attempt and execution owner')
    with job_write_scope(job_id, expected_attempt_id, expected_execution_owner):
        assert_chapter_execution(job_id)
        make_get_chapter_content(job_id, attempt_id=expected_attempt_id,
                                 execution_owner=expected_execution_owner)
        result = translate_chapter(job_id, chapter_id)
        assert_chapter_execution(job_id)
        if result.reduced_html and result.file_path:
            set_chapter_output(job_id, result.file_path, result.reduced_html, attempt_id=expected_attempt_id,
                               execution_owner=expected_execution_owner)
    return {
        "job_id": result.job_id,
        "chapter_id": result.chapter_id,
        "file_path": result.file_path,
        "chapter_kind": result.chapter_kind,
        "skipped": result.skipped,
        "error": result.error,
        "chunks_count": len(result.chunks),
        "chunks_ok": sum(1 for c in result.chunks if not c.error),
        "chunks_cached": sum(1 for c in result.chunks if c.cached),
        "chunks_failed": sum(1 for c in result.chunks if c.error),
    }
