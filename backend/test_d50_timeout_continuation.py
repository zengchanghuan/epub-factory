"""Fault injection for timeout -> durable continuation -> accepted delivery."""
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch
import json
import sqlite3
import zipfile

import pytest
from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import create_engine

import app.job_runner as runner
from app.domain.translation_checkpoints import TranslationCheckpoints
from app.domain.translation_continuation import (
    TranslationSliceExpired, checkpoint_progress, continuation_decision,
    slice_expired, slice_seconds, translation_slice, cumulative_slice_usage,
)
from app.infra.execution_lease import execution_lease
from app.models import ConversionResult, Job, JobStatus, OutputMode
from app.storage import JobStore
from app.domain.dispatch_intent import dispatch_identity
from app.storage_db import Base, PersistentJobStore


def epub(path):
    from test_d42_translation_contract import epub_bytes
    path.write_bytes(epub_bytes("这是一份用于验证完整交付的中文译文。", translated=True))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('EPUB_TRANSLATION_CHECKPOINT_DB', str(tmp_path / 'cache.db'))
    monkeypatch.setenv('EPUB_TRANSLATION_MAX_CONTINUATIONS', '48')
    monkeypatch.setenv('EPUB_TRANSLATION_MAX_STALLED_SLICES', '2')
    monkeypatch.setenv('EPUB_FAST_TRANSLATION', '1')
    monkeypatch.delenv('REDIS_URL', raising=False)
    monkeypatch.delenv('CELERY_BROKER_URL', raising=False)
    source = tmp_path / 'book.epub'
    epub(source)
    job = Job(id='timeout-book', trace_id='test', source_filename='book.epub',
        input_path=str(source), output_mode=OutputMode.simplified, enable_translation=True,
        status=JobStatus.pending, expected_amount='59.27', access_token='preserved-token',
        cache_policy='fresh', translation_stats={'attempt_id': 'same-attempt',
            'translation_attempt': 1, 'free_retry_count': 0, 'payment_verified': True,
            'prompt_tokens': 123, 'cost_history': [{'cost_usd': 0.1}]})
    store = JobStore(); store.add(job)
    monkeypatch.setattr(runner, 'job_store', store)
    monkeypatch.setattr(runner, 'OUTPUT_DIR', tmp_path)
    monkeypatch.setattr(runner, 'usage_scope', lambda *a, **kw: nullcontext())
    notify = Mock(); monkeypatch.setattr(runner, 'notify_job_completed', notify)
    monkeypatch.setattr(runner, 'report_error', Mock())
    return store, job, notify


def save_chunk(job, key, phase='final', error=None):
    TranslationCheckpoints(job.id, 'scope').put('chunk:' + key, {
        'attempt_id': 'same-attempt', 'phase': phase,
        'result': {'translated_html': '已保存译文', 'error': error},
    })


@pytest.mark.parametrize('signal', [TranslationSliceExpired, SoftTimeLimitExceeded])
@pytest.mark.parametrize('stage_unavailable', [False, True])
def test_timeout_continues_same_paid_attempt_until_delivery(setup, signal, stage_unavailable, monkeypatch):
    store, job, notify = setup
    if stage_unavailable:
        add_stage = store.add_stage
        def stage(stage):
            if stage.stage_name == 'auto_continuation':
                raise RuntimeError('stage audit temporarily unavailable')
            return add_stage(stage)
        monkeypatch.setattr(store, 'add_stage', stage)
    calls = []
    before = deepcopy(job.translation_stats)
    def convert(**kwargs):
        calls.append(job.translation_stats['attempt_id'])
        if len(calls) < 3:
            save_chunk(job, str(len(calls)))
            raise signal()
        assert checkpoint_progress(job.id) == 4
        assert job.translation_stats['free_retry_count'] == 0
        epub(kwargs['output_path'])
        return ConversionResult(validation_passed=True, message='完成',
            translation_stats={'total_chunks': 2, 'failed_chunks': 0})
    with patch('app.domain.fast_translation_runner.run_fast_translation_job', side_effect=convert):
        runner.run_job(job.id, 'same-attempt')
    job = store.get(job.id)
    assert calls == ['same-attempt'] * 3
    assert job.status == JobStatus.success and Path(job.output_path).is_file()
    assert job.translation_stats['qa_report']['can_deliver'] is True
    assert job.translation_stats['artifact_manifest']['attempt_id'] == 'same-attempt'
    assert job.translation_stats['continuation']['count'] == 2
    assert job.cache_policy == 'fresh' and job.expected_amount == '59.27'
    assert job.access_token == 'preserved-token'
    for name in ['attempt_id', 'translation_attempt', 'free_retry_count', 'payment_verified', 'cost_history']:
        assert job.translation_stats[name] == before[name]
    notify.assert_called_once()
    assert notify.call_args.args[1] == JobStatus.success


def test_repeated_timeout_without_new_durable_work_stops(setup):
    store, job, notify = setup
    with patch('app.domain.fast_translation_runner.run_fast_translation_job', side_effect=SoftTimeLimitExceeded()) as convert:
        runner.run_job(job.id, 'same-attempt')
    job = store.get(job.id)
    assert convert.call_count == 2
    assert job.status == JobStatus.failed
    assert '没有新增已保存进度' in job.message
    assert job.translation_stats['continuation']['stalled_slices'] == 2
    assert job.translation_stats['qa_report']['status'] == 'blocked'
    assert job.translation_stats['qa_report']['summary'] == job.message
    assert job.translation_stats['free_retry_count'] == 0
    notify.assert_called_once()


def test_progressing_translation_is_still_bounded(setup, monkeypatch):
    store, job, notify = setup
    monkeypatch.setenv('EPUB_TRANSLATION_MAX_CONTINUATIONS', '2')
    calls = []
    def convert(**kwargs):
        calls.append(1); save_chunk(job, str(len(calls))); raise SoftTimeLimitExceeded()
    with patch('app.domain.fast_translation_runner.run_fast_translation_job', side_effect=convert):
        runner.run_job(job.id, 'same-attempt')
    job = store.get(job.id)
    assert len(calls) == 3 and job.status == JobStatus.failed
    assert '次数上限' in job.message
    notify.assert_called_once()


@pytest.mark.parametrize('transition', ['cancel', 'new_attempt'])
def test_timeout_never_reopens_cancelled_or_superseded_work(setup, transition):
    store, job, notify = setup
    def convert(**kwargs):
        if transition == 'cancel':
            store.update_status(job.id, JobStatus.cancelled, '用户取消')
        else:
            from contextvars import Context
            Context().run(store.update_status, job.id, JobStatus.pending, '新尝试', translation_stats={'attempt_id': 'replacement'})
        raise SoftTimeLimitExceeded()
    with patch('app.domain.fast_translation_runner.run_fast_translation_job', side_effect=convert):
        runner.run_job(job.id, 'same-attempt')
    job = store.get(job.id)
    assert job.status == (JobStatus.cancelled if transition == 'cancel' else JobStatus.pending)
    assert not job.translation_stats['continuation'].get('count')
    notify.assert_not_called()


def test_local_continuation_does_not_adopt_replacement_after_releasing_lease(setup, monkeypatch):
    from contextlib import contextmanager
    store, job, notify = setup
    real_lease = runner.execution_lease

    @contextmanager
    def replaced_after_release(*args):
        with real_lease(*args) as lease:
            yield lease
        store.update_status(job.id, JobStatus.pending, "New attempt queued",
                            translation_stats={"attempt_id": "replacement"})

    monkeypatch.setattr(runner, "execution_lease", replaced_after_release)

    def convert(**kwargs):
        save_chunk(job, "one")
        raise TranslationSliceExpired()

    with patch('app.domain.fast_translation_runner.run_fast_translation_job', side_effect=convert) as conversion:
        runner.run_job(job.id, 'same-attempt')
    conversion.assert_called_once()
    current = store.get(job.id)
    assert current.status == JobStatus.pending
    assert current.translation_stats['attempt_id'] == 'replacement'
    notify.assert_not_called()


def test_broker_failure_keeps_durable_continuation_and_releases_lease(setup, tmp_path, monkeypatch):
    _, job, notify = setup
    engine = create_engine('sqlite:///' + str(tmp_path / 'jobs.db'))
    Base.metadata.create_all(engine)
    store = PersistentJobStore(engine); store.add(job)
    monkeypatch.setattr(runner, 'job_store', store)
    # Force the broker branch but use a real local execution lease in this test.
    from contextlib import contextmanager
    @contextmanager
    def local_lease(*args):
        with patch.dict('os.environ', {'REDIS_URL': '', 'CELERY_BROKER_URL': ''}):
            with execution_lease(*args) as lease:
                yield lease
    monkeypatch.setattr(runner, 'execution_lease', local_lease)
    monkeypatch.setenv('CELERY_BROKER_URL', 'redis://offline-test')
    def convert(**kwargs):
        save_chunk(job, 'one'); raise SoftTimeLimitExceeded()
    def enqueue(*args):
        with local_lease(job.id, 'same-attempt') as lease:
            assert lease is not None, 'Dispatch must happen after releasing execution lease'
        raise ConnectionError('offline')
    with patch('app.domain.fast_translation_runner.run_fast_translation_job', side_effect=convert), patch(
            'app.infra.job_dispatch_publisher.publish_conversion', side_effect=enqueue) as send:
        runner.run_job(job.id, 'same-attempt')
    job = store.get(job.id)
    send.assert_called_once_with(job.id, 'same-attempt')
    assert store.get(job.id).status == JobStatus.pending
    with store._Session() as session:
        dispatch = store.get_dispatch(dispatch_identity(job.id, 'same-attempt'))
        assert dispatch['attempt_id'] == 'same-attempt' and dispatch['status'] == 'pending'
        assert dispatch['attempts'] >= 1
    notify.assert_not_called()
    engine.dispose()


def test_progress_counts_preparation_and_review_but_not_replays_or_errors(setup):
    _, job, _ = setup
    cache = TranslationCheckpoints(job.id, 'scope')
    cache.put('profile', {'profile': {'status': 'ok'}})
    save_chunk(job, 'one', 'draft'); save_chunk(job, 'bad', error='inflight request interrupted')
    assert checkpoint_progress(job.id) == 2
    save_chunk(job, 'one', 'draft')
    assert checkpoint_progress(job.id) == 2
    save_chunk(job, 'one', 'final')
    assert checkpoint_progress(job.id) == 3


def test_unreadable_checkpoint_stops_automatic_spending(setup, monkeypatch, tmp_path):
    _, job, _ = setup
    broken = tmp_path / 'broken.db'; broken.write_text('not a database')
    monkeypatch.setenv('EPUB_TRANSLATION_CHECKPOINT_DB', str(broken))
    assert checkpoint_progress(job.id) is None
    state, error = continuation_decision({'continuation': {'slice_start_progress': 0}}, job.id, 'soft_time_limit')
    assert error and '断点存储' in error


def test_slice_deadline_is_scoped_and_leaves_soft_limit_reserve(monkeypatch):
    monkeypatch.setenv('EPUB_BOOK_SOFT_TIME_LIMIT', '100')
    monkeypatch.setenv('EPUB_TRANSLATION_SLICE_SECONDS', '1800')
    assert slice_seconds() == 90
    assert not slice_expired()
    with patch('app.domain.translation_continuation.time.monotonic', side_effect=[10, 99, 100]):
        with translation_slice():
            assert not slice_expired()
            assert slice_expired()
    assert not slice_expired()


def test_usage_accumulates_without_adding_cached_chunk_progress():
    prior = {'continuation': {'usage_totals': {'api_calls': 2, 'prompt_tokens': 20,
        'completion_tokens': 10, 'total_tokens': 30, 'cost_usd': 0.25}}}
    current = {'api_calls': 1, 'prompt_tokens': 5, 'completion_tokens': 3, 'total_tokens': 8,
        'cost_usd': 0.125, 'cached_chunks': 5, 'chunks_processed': 6}
    total = cumulative_slice_usage(current, prior)
    assert total['api_calls'] == 3 and total['total_tokens'] == 38
    assert total['cost_usd'] == 0.375
    assert total['cached_chunks'] == 5 and total['chunks_processed'] == 6
    # Repeated progress snapshots use the fixed preceding-slice baseline.
    assert cumulative_slice_usage(current, prior) == total
    prior['continuation']['usage_totals']['cost_usd'] = None
    assert cumulative_slice_usage(current, prior)['cost_usd'] is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
