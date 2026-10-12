"""Offline fault injection: transactional handoff, retry admission and artifact delivery."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import json
import os
import threading
import time

import pytest
from fastapi import BackgroundTasks, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, update

from app.models import Job, JobStatus, OutputMode
from app.storage_db import Base, PersistentJobStore, JobRecord
from app.domain.job_dispatch_service import dispatch_pending
from app.domain.artifact_receipt import receipt, matches
from app.domain.retry_policy import blocked_reason
from app.domain.upload_guard import upload_guard
from app.infra.execution_lease import execution_lease


@pytest.fixture
def store(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'jobs.db'}")
    Base.metadata.create_all(engine)
    result = PersistentJobStore(engine)
    yield result
    engine.dispose()


def add(store, key='order', **kwargs):
    values = dict(id=key, source_filename='book.epub', trace_id=key,
                  input_path='/unused/book.epub', output_mode=OutputMode.simplified,
                  status=JobStatus.pending_payment, expected_amount='5.99',
                  creator_session='session-a')
    values.update(kwargs)
    job = Job(**values)
    if job.enable_translation and job.status != JobStatus.cancelled:
        from app.domain.payment_entitlement import quote_entitlement
        job.is_test_order = True
        job.payment_entitlement = quote_entitlement(job, test_bypass=True)
    store.add(job)
    return store.get(key)


def intents(store):
    return store.list_dispatches()


def test_payment_and_intent_rollback_together(store):
    add(store)
    with patch.object(store, '_ensure_dispatch_in_session', side_effect=RuntimeError('disk full')):
        with pytest.raises(RuntimeError):
            store.try_mark_paid('order')
    assert store.get('order').status == JobStatus.pending_payment
    assert intents(store) == []




def test_batch_activation_and_intents_are_one_transaction(store):
    add(store, 'a', batch_id='bundle', batch_index=0)
    add(store, 'b', batch_id='bundle', batch_index=1)
    with patch.object(store, '_ensure_dispatch_in_session', side_effect=RuntimeError('disk full')):
        with pytest.raises(RuntimeError):
            store.try_mark_batch_paid('bundle')
    assert all(j.status == JobStatus.pending_payment for j in store.list_jobs())
    assert intents(store) == []
    assert store.try_mark_batch_paid('bundle')
    assert len(intents(store)) == 2
    assert not store.try_mark_batch_paid('bundle')


def test_batch_insertion_never_leaves_half_batch(store):
    a = Job(id='a', source_filename='a', trace_id='a', input_path='a', output_mode=OutputMode.simplified)
    b = Job(id='a', source_filename='b', trace_id='b', input_path='b', output_mode=OutputMode.simplified)
    with pytest.raises(Exception):
        store.add_many([a,b])
    assert store.list_jobs() == []
    assert intents(store) == []


def test_concurrent_dispatchers_publish_once_per_claim(store):
    add(store, status=JobStatus.pending)
    entered, release = threading.Event(), threading.Event()
    enqueue = Mock(side_effect=lambda *args: (entered.set(), release.wait(5)))
    worker = threading.Thread(target=dispatch_pending, args=(store, enqueue), kwargs={'now': time.time()+1})
    worker.start()
    assert entered.wait(5)
    dispatch_pending(PersistentJobStore(store._engine), enqueue, now=time.time()+1)
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert enqueue.call_count == 1






def test_customer_retry_preserves_failure_history_and_stops_repeat(store):
    add(store, enable_translation=True, status=JobStatus.failed, error_code='PARTIAL_TRANSLATION',
        translation_stats={'attempt_id':'old','upload_fingerprint':'source-key','qa_report':{'flags':['untranslated']}})
    job, reason = store.restart_translation_attempt('order', attempt_id='next',
        action_label='重试',max_free_retries=3,started_at=datetime.now(timezone.utc))
    assert reason == 'ok'
    assert job.translation_stats['attempt_history'][0]['error_code'] == 'PARTIAL_TRANSLATION'
    assert job.translation_stats['upload_fingerprint'] == 'source-key'
    store.update_status('order',JobStatus.failed,error_code='PARTIAL_TRANSLATION',
        translation_stats={'qa_report':{'flags':['untranslated']}})
    job,reason = store.restart_translation_attempt('order',attempt_id='again',action_label='重试',
        max_free_retries=3,started_at=datetime.now(timezone.utc))
    assert reason == 'manual_review'
    assert job.translation_stats.get('attempt_id', '') == 'next'
    assert blocked_reason({},error_code='EPUB_VALIDATION_FAILED',limit=3) == 'manual_review'
    assert blocked_reason({'free_retry_count':3},error_code='TRANSLATION_FAILED',limit=3) == 'retry_limit'
    assert blocked_reason({'free_retry_count':30},error_code='EPUB_VALIDATION_FAILED',limit=-1) is None


def test_restart_rejects_unpaid_cancelled_task(store,tmp_path):
    import app.main as main
    source=tmp_path/'book.epub'; source.write_bytes(b'book')
    job=add(store,status=JobStatus.cancelled,enable_translation=True,input_path=str(source))
    with patch.object(main,'job_store',store),patch.dict(os.environ,{'SKIP_PAYMENT_CHECK':'0'}),patch(
            'app.infra.alipay.query_verified_trade',return_value={'out_trade_no':'order','trade_status':'WAIT_BUYER_PAY','total_amount':'5.99'}):
        with pytest.raises(HTTPException) as exc:
            main._restart_translation_job(job,BackgroundTasks(),'重试')
    assert exc.value.status_code == 409
    assert store.get('order').status == JobStatus.cancelled
    assert intents(store) == []


def test_duplicate_uploads_are_scoped_and_concurrency_guarded(store,tmp_path):
    source=tmp_path/'book.epub';source.write_bytes(b'same-book')
    params=dict(user_id=None,client_session='session-a',paths=[source],options={'quality':'standard'})
    with upload_guard(store,**params) as (fingerprint,lease):
        with pytest.raises(HTTPException):
            with upload_guard(store,**params): pass
        add(store,translation_stats={'upload_fingerprint':fingerprint})
    with pytest.raises(HTTPException) as exc:
        with upload_guard(PersistentJobStore(store._engine),**params): pass
    assert exc.value.detail['code']=='duplicate_upload'
    assert exc.value.detail['job_id']=='order'
    with upload_guard(store,**{**params,'client_session':'session-b'}): pass
    with upload_guard(store,**{**params,'options':{'quality':'high'}}): pass


def test_upload_endpoint_reuses_order_before_second_gateway_call(store,tmp_path):
    import app.main as main
    from app.infra import alipay
    with patch.object(main,'job_store',store),patch.object(main,'UPLOAD_DIR',tmp_path),patch.dict(
            os.environ,{'SKIP_PAYMENT_CHECK':'0','ALIPAY_DISABLE_PRECREATE':'0'}),patch.object(
            main,'_validate_upload_format',return_value=[]),patch.object(
            alipay,'create_alipay_precreate',return_value='https://qr.alipay.com/offline') as gateway:
        client=TestClient(main.app)
        request=dict(files={'file':('book.epub',b'offline-book','application/epub+zip')},headers={'X-Client-Session':'browser-one'})
        first=client.post('/api/v2/jobs',**request)
        assert first.status_code==200, first.text
        duplicate=client.post('/api/v2/jobs',**request)
        assert duplicate.status_code==409, duplicate.text
        assert duplicate.json()['detail']['job_id']==first.json()['job_id']
        detail = duplicate.json()['detail']
        assert duplicate.headers['cache-control'] == 'no-store'
        assert detail['access_token'] == first.json()['access_token']
        # Simulate losing the first creation response and therefore its token.
        assert client.get('/api/v2/jobs/' + detail['job_id'], headers=request['headers']).status_code == 403
        restored = client.get('/api/v2/jobs/' + detail['job_id'],
                              headers={'X-Job-Token': detail['access_token']})
        assert restored.status_code == 200
        assert gateway.call_count==1
        assert len(list(tmp_path.glob('*book.epub')))==1
        assert len(store.list_jobs())==1


def test_batch_duplicate_order_reuses_existing_batch(store,tmp_path):
    import app.main as main
    with patch.object(main,'job_store',store),patch.object(main,'UPLOAD_DIR',tmp_path),patch.dict(
            os.environ,{'SKIP_PAYMENT_CHECK':'0','ALIPAY_DISABLE_PRECREATE':'0'}),patch.object(
            main,'_validate_upload_format',return_value=[]),patch(
            'app.infra.alipay.create_alipay_precreate',return_value='https://qr.alipay.com/offline') as gateway:
        client=TestClient(main.app)
        request=dict(files=[('files',('a.epub',b'one')),('files',('b.epub',b'two'))],headers={'X-Client-Session':'browser-two'})
        first=client.post('/api/v2/batches',**request)
        assert first.status_code==200,first.text
        duplicate=client.post('/api/v2/batches',**request)
        assert duplicate.status_code==409,duplicate.text
        assert duplicate.json()['detail']['batch_id']==first.json()['batch_id']
        detail = duplicate.json()['detail']
        assert detail['access_token'] == first.json()['access_token']
        headers = {'X-Job-Token': detail['access_token']}
        assert client.get('/api/v2/batches/' + detail['batch_id'], headers=headers).status_code == 200
        for job in store.list_jobs():
            assert client.get('/api/v2/jobs/' + job.id, headers=headers).status_code == 200
        assert gateway.call_count==1
        assert len(store.list_jobs())==2


@pytest.mark.parametrize('batch', [False, True])
def test_authenticated_duplicate_restores_owner_across_browsers_only(store, tmp_path, batch):
    import app.main as main
    import app.storage as storage
    from app.auth import jwt
    from app.models import User
    store.create_user(User(id='owner'))
    store.create_user(User(id='other'))
    with patch.object(main, 'job_store', store), patch.object(storage, 'job_store', store), patch.object(
            main, 'UPLOAD_DIR', tmp_path), patch.object(jwt, '_SECRET_KEY', 'isolated-test-signing-secret'), patch.dict(
            os.environ, {'SKIP_PAYMENT_CHECK': '0', 'ALIPAY_DISABLE_PRECREATE': '0'}), patch.object(
            main, '_validate_upload_format', return_value=[]), patch(
            'app.infra.alipay.create_alipay_precreate', return_value='https://qr.alipay.com/offline'):
        client = TestClient(main.app)
        endpoint = '/api/v2/batches' if batch else '/api/v2/jobs'
        files = [('files', ('a.epub', b'one')), ('files', ('b.epub', b'two'))] if batch else {'file': ('book.epub', b'book')}
        headers = {'Authorization': 'Bearer ' + jwt.create_access_token('owner'), 'X-Client-Session': 'first-browser'}
        first = client.post(endpoint, files=files, headers=headers)
        assert first.status_code == 200, first.text
        other = client.post(endpoint, files=files, headers={**headers, 'Authorization': 'Bearer ' + jwt.create_access_token('other')})
        assert other.status_code == 200, other.text
        assert other.json()['access_token'] != first.json()['access_token']
        invalid = client.post(endpoint, files=files, headers={**headers, 'Authorization': 'Bearer invalid'})
        assert invalid.status_code == 200, invalid.text
        assert invalid.json()['access_token'] != first.json()['access_token']
        duplicate = client.post(endpoint, files=files, headers={**headers, 'X-Client-Session': 'new-browser'})
        assert duplicate.status_code == 409
        detail = duplicate.json()['detail']
        assert detail['access_token'] == first.json()['access_token']
        key = 'batch_id' if batch else 'job_id'
        assert client.get(endpoint + '/' + detail[key], headers={'X-Job-Token': detail['access_token']}).status_code == 200
        store.update_user(User(id='owner', is_active=False))
        denied = client.post(endpoint, files=files, headers=headers)
        assert denied.status_code == 409
        assert 'access_token' not in denied.json()['detail']


@pytest.mark.parametrize('claimed,expired', [(True, False), (False, True)])
def test_anonymous_duplicate_never_recovers_claimed_or_expired_access(store, tmp_path, claimed, expired):
    source = tmp_path / 'book.epub'; source.write_bytes(b'book')
    params = dict(user_id=None, client_session='session-a', paths=[source], options={})
    expires = datetime.now(timezone.utc) + timedelta(days=-1 if expired else 1)
    with upload_guard(store, **params) as (fingerprint, _):
        add(store, access_token='original-secret', token_expires_at=expires,
            user_id='claimed-owner' if claimed else None, translation_stats={'upload_fingerprint': fingerprint})
    with pytest.raises(HTTPException) as exc:
        with upload_guard(store, **params): pass
    assert exc.value.detail['code'] == 'duplicate_upload'
    assert 'access_token' not in exc.value.detail
    assert store.get('order').access_token == 'original-secret'
    assert store.get('order').token_expires_at.replace(tzinfo=timezone.utc) == expires


def test_receipt_rejects_same_size_tampering_and_missing_artifact(tmp_path):
    path=tmp_path/'result.epub';path.write_bytes(b'good')
    manifest=receipt(path,'accepted-attempt')
    assert matches(path,manifest)
    path.write_bytes(b'evil')
    assert not matches(path,manifest)
    path.unlink()
    assert not matches(path,manifest)


def test_batch_download_cannot_bypass_integrity(store,tmp_path):
    import app.main as main
    path=tmp_path/'output.epub';path.write_bytes(b'good')
    add(store,status=JobStatus.success,output_path=str(path),batch_id='batch',batch_index=0,
        translation_stats={'artifact_manifest':receipt(path,'attempt')})
    path.write_bytes(b'evil')
    with patch.object(main,'job_store',store),patch.object(main,'_authorize_batch_access',return_value=True):
        response=TestClient(main.app).get('/api/v2/batches/batch/download')
    assert response.status_code==410


@pytest.fixture
def delivery_client(store, tmp_path):
    import app.main as main
    import app.storage as storage
    from app.auth import jwt
    from app.models import User
    for key in ('owner', 'other'):
        store.create_user(User(id=key))
    with patch.object(main, 'job_store', store), patch.object(storage, 'job_store', store), patch.object(
            main, 'OUTPUT_DIR', tmp_path), patch.object(jwt, '_SECRET_KEY', 'offline-delivery-account-key'), patch.object(
            main, 'DOWNLOAD_SIGN_SECRET', 'offline-delivery-download-key'):
        client = TestClient(main.app)  # No lifespan: no background workers or production startup.
        try:
            yield SimpleNamespace(client=client, owner={'Authorization': 'Bearer ' + jwt.create_access_token('owner')},
                                  other={'Authorization': 'Bearer ' + jwt.create_access_token('other')})
        finally:
            client.close()


@pytest.mark.parametrize('expired', [False, True])
def test_paid_delivery_owner_can_retrieve_without_original_token(store, tmp_path, delivery_client, expired):
    path = tmp_path / 'output.epub'; path.write_bytes(b'paid-result')
    job = add(store, status=JobStatus.success, output_path=str(path), user_id='owner',
              access_token='original-token', token_expires_at=datetime.now(timezone.utc) + timedelta(days=-1 if expired else 1),
              translation_stats={'artifact_manifest': receipt(path, 'attempt')})
    client = delivery_client.client
    assert client.get('/api/v2/jobs', headers=delivery_client.owner).json()['items'][0]['job_id'] == job.id
    detail = client.get('/api/v2/jobs/order', headers=delivery_client.owner)
    assert detail.status_code == 200
    # Browser navigation carries no Authorization header; follow the issued link.
    result = client.get(detail.json()['download_url'])
    assert result.status_code == 200
    assert result.content == b'paid-result'
    assert client.get('/api/v2/jobs/order/download', headers=delivery_client.owner).status_code == 200
    assert store.get(job.id).access_token == job.access_token
    assert store.get(job.id).token_expires_at == job.token_expires_at


def test_paid_delivery_owner_access_does_not_weaken_other_identities(store, tmp_path, delivery_client):
    from app.models import User
    path = tmp_path / 'output.epub'; path.write_bytes(b'paid-result')
    add(store, status=JobStatus.success, output_path=str(path), user_id='owner', access_token='original-token',
        token_expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    for headers in ({}, delivery_client.other, {'Authorization': 'Bearer invalid'}):
        for suffix in ('', '/download'):
            assert delivery_client.client.get('/api/v2/jobs/order' + suffix, headers=headers).status_code == 403
    # Anonymous capability access continues to work, independently of account login.
    assert delivery_client.client.get('/api/v2/jobs/order/download', headers={'X-Job-Token': 'original-token'}).status_code == 200
    store.update_user(User(id='owner', is_active=False))
    for token in ({}, {'X-Job-Token': 'original-token'}):
        for suffix in ('', '/download'):
            assert delivery_client.client.get('/api/v2/jobs/order' + suffix,
                headers={**delivery_client.owner, **token}).status_code == 403


def test_paid_delivery_expired_anonymous_token_is_still_rejected(store, tmp_path, delivery_client):
    path = tmp_path / 'output.epub'; path.write_bytes(b'paid-result')
    add(store, status=JobStatus.success, output_path=str(path), access_token='expired',
        token_expires_at=datetime.now(timezone.utc) - timedelta(days=1))
    for headers in ({'X-Job-Token': 'expired'}, delivery_client.owner):
        assert delivery_client.client.get('/api/v2/jobs/order/download', headers=headers).status_code == 403


def test_paid_delivery_owner_can_recover_payment_without_original_token(store, delivery_client):
    import app.main as main
    add(store, user_id='owner', access_token='expired', token_expires_at=datetime.now(timezone.utc) - timedelta(days=1))
    trade = {'out_trade_no': 'order', 'trade_status': 'TRADE_SUCCESS', 'total_amount': '5.99'}
    with patch('app.infra.alipay.query_verified_trade', return_value=trade) as query, patch.object(
            main, '_use_celery', return_value=True), patch('app.infra.job_dispatch_publisher.publish_conversion') as publish, patch.object(
            main, '_record_verified_payment'):
        assert delivery_client.client.post('/api/v2/jobs/order/recover', headers=delivery_client.other).status_code == 403
        query.assert_not_called()
        result = delivery_client.client.post('/api/v2/jobs/order/recover', headers=delivery_client.owner)
    assert result.status_code == 200
    assert result.json()['recovered']
    assert store.get('order').status == JobStatus.pending
    publish.assert_called_once()
    assert len(store.list_dispatches('order')) == 1


@pytest.mark.parametrize('paid', [False, True])
def test_paid_delivery_owner_retry_still_requires_payment(store, tmp_path, delivery_client, paid):
    import app.main as main
    from app.domain.payment_entitlement import quote_entitlement, grant_verified_entitlement
    source = tmp_path / 'source.epub'; source.write_bytes(b'offline-source-not-executed')
    job = Job(id='retry', source_filename='source.epub', input_path=str(source), trace_id='delivery-retry',
              status=JobStatus.failed, output_mode=OutputMode.simplified, enable_translation=True,
              user_id='owner', access_token='old', token_expires_at=datetime.now(timezone.utc) - timedelta(days=1),
              expected_amount='5.99', translation_model='deepseek-flash',
              translation_stats={'attempt_id': 'failed-attempt'})
    job.payment_entitlement = quote_entitlement(job)
    store.add(job)
    if paid:
        grant_verified_entitlement(store, job, '5.99', 'verified_query')
    with patch.object(main, '_use_celery', return_value=True), patch(
            'app.infra.job_dispatch_publisher.publish_conversion') as publish, patch.object(
            main, 'run_job', side_effect=AssertionError('No book execution')), patch.dict(
            os.environ, {'SKIP_PAYMENT_CHECK': '0', 'EPUB_TRANSLATION_MAX_FREE_RETRIES': '2'}):
        assert delivery_client.client.post('/api/v2/jobs/retry/restart-translation',
            headers=delivery_client.other).status_code == 403
        result = delivery_client.client.post('/api/v2/jobs/retry/restart-translation', headers=delivery_client.owner)
    assert result.status_code == (200 if paid else 402), result.text
    if paid:
        assert store.get(job.id).status == JobStatus.pending
        assert store.get(job.id).translation_stats['attempt_id'] != 'failed-attempt'
        assert store.get(job.id).payment_entitlement['state'] == 'paid'
        publish.assert_called_once()
    else:
        assert store.get(job.id).status == JobStatus.failed
        publish.assert_not_called()


@pytest.mark.parametrize('damage', ['missing', 'empty_path', 'tampered'])
def test_paid_delivery_batch_never_silently_omits_completed_artifact(store, tmp_path, delivery_client, damage):
    for index in range(2):
        path = tmp_path / f'{index}.epub'; path.write_bytes(b'good')
        add(store, str(index), status=JobStatus.success, output_path=str(path), user_id='owner',
            batch_id='batch', batch_index=index, translation_stats={'artifact_manifest': receipt(path, 'attempt')})
    if damage == 'missing':
        path.unlink()
    elif damage == 'empty_path':
        with store._Session() as session:
            session.execute(update(JobRecord).where(JobRecord.id == '1').values(output_path=None))
            session.commit()
    else:
        path.write_bytes(b'evil')
    result = delivery_client.client.get('/api/v2/batches/batch/download', headers=delivery_client.owner)
    assert result.status_code == 410
    assert '无需再次付款' in result.json()['detail']
    assert not list(tmp_path.glob('batch-*.zip'))


def test_paid_delivery_batch_names_survive_extraction_and_links_are_scoped(store, tmp_path, delivery_client):
    import io
    import zipfile
    import app.main as main
    from urllib.parse import urlsplit
    names = ['03-book.epub', 'book.epub', 'book.epub', '04-book.epub', 'book.epub']
    contents = []
    for index, name in enumerate(names):
        directory = tmp_path / str(index); directory.mkdir()
        path = directory / name
        content = f'paid-book-{index}'.encode(); contents.append(content); path.write_bytes(content)
        add(store, str(index), status=JobStatus.success, output_path=str(path), user_id='owner', access_token='old',
            token_expires_at=datetime.now(timezone.utc) - timedelta(days=1),
            batch_id='batch', batch_index=index, translation_stats={'artifact_manifest': receipt(path, 'attempt')})
    client = delivery_client.client
    detail = client.get('/api/v2/batches/batch', headers=delivery_client.owner)
    assert detail.status_code == 200
    url = detail.json()['download_url']
    result = client.get(url)  # A real anchor navigation has no account header.
    assert result.status_code == 200
    with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
        assert len(archive.namelist()) == len(set(archive.namelist())) == len(names)
        archive.extractall(tmp_path / 'extracted')
    assert sorted(p.read_bytes() for p in (tmp_path / 'extracted').iterdir()) == sorted(contents)
    assert not list(tmp_path.glob('batch-*.zip'))
    assert client.get('/api/v2/batches/batch/download').status_code == 403
    assert client.get('/api/v2/batches/batch/download', headers=delivery_client.other).status_code == 403
    # A single-child signature must never authorize downloading the whole batch.
    child_url = detail.json()['jobs'][0]['download_url']
    assert client.get('/api/v2/batches/batch/download?' + urlsplit(child_url).query).status_code == 403
    assert client.get('/api/v2/jobs/0/download?' + urlsplit(url).query).status_code == 403
    add(store, 'foreign', status=JobStatus.success, output_path=str(path), user_id='other', access_token='foreign',
        batch_id='other-batch', batch_index=0)
    assert client.get(url.replace('/batches/batch/', '/batches/other-batch/')).status_code == 403
    assert client.get(url.replace('sig=', 'sig=invalid')).status_code == 403
    with patch.object(main, 'DOWNLOAD_SIGN_TTL_SECONDS', -1):
        expired = client.get('/api/v2/batches/batch', headers=delivery_client.owner).json()['download_url']
    assert client.get(expired).status_code == 403


def test_repeated_batch_download_cleans_archives_but_preserves_epubs(store, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import io
    import zipfile
    import app.main as main
    path = tmp_path / 'output.epub'; path.write_bytes(b'accepted-output')
    add(store, status=JobStatus.success, output_path=str(path), batch_id='batch', batch_index=0,
        translation_stats={'artifact_manifest': receipt(path, 'attempt')})
    with patch.object(main, 'job_store', store), patch.object(main, 'OUTPUT_DIR', tmp_path), patch.object(
            main, '_authorize_batch_access', return_value=True):
        client = TestClient(main.app)
        # Two outstanding downloads must not overwrite or delete each other's ZIP.
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: client.get('/api/v2/batches/batch/download'), range(2)))
        for response in responses:
            assert response.status_code == 200
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                assert archive.read('output.epub') == b'accepted-output'
            assert not list(tmp_path.glob('batch-*.zip'))
        with patch.object(main.zipfile.ZipFile, 'write', side_effect=OSError('disk full')):
            with pytest.raises(OSError, match='disk full'):
                client.get('/api/v2/batches/batch/download')
        assert not list(tmp_path.glob('batch-*.zip'))
        assert path.read_bytes() == b'accepted-output'


@pytest.mark.parametrize('failure', ['disconnect', 'cancel', 'none'])
def test_temporary_download_cleans_up_on_interrupted_send(tmp_path, failure):
    import asyncio
    from app.infra.temporary_file_response import TemporaryFileResponse
    path = tmp_path / 'bundle.zip'; path.write_bytes(b'bundle-bytes')
    response = TemporaryFileResponse(path)
    bodies = []
    async def send(event):
        assert event['type'] != 'http.response.pathsend'
        if event['type'] == 'http.response.body':
            assert path.exists(), 'Do not delete the file before streaming it'
            if failure == 'disconnect': raise OSError('client disconnected')
            if failure == 'cancel': raise asyncio.CancelledError()
            bodies.append(event['body'])
    async def receive():
        return {'type': 'http.request', 'body': b''}
    scope = {'type': 'http', 'method': 'GET', 'headers': [], 'extensions': {'http.response.pathsend': {}}}
    if failure != 'none':
        with pytest.raises(OSError if failure == 'disconnect' else asyncio.CancelledError):
            asyncio.run(response(scope, receive, send))
    else:
        asyncio.run(response(scope, receive, send))
        assert b''.join(bodies) == b'bundle-bytes'
    assert not path.exists()


def test_download_qa_denial_overrides_success():
    import app.main as main
    job=Job(id='a',source_filename='a',trace_id='a',input_path='a',output_mode=OutputMode.simplified,
        status=JobStatus.success,output_path='output',translation_stats={'qa_report':{'can_deliver':False}})
    assert not main._job_can_download(job)


def test_epubcheck_diagnostics_keep_locations_without_source_snippets(tmp_path):
    from app.engine.epub_validation import validate_epub
    epub=tmp_path/'book.epub';epub.write_bytes(b'fixture')
    jar=tmp_path/'epubcheck.jar';jar.touch()
    def run(command,**kwargs):
        Path(command[-1]).write_text(json.dumps({'messages':[{'ID':'RSC-007','severity':'ERROR',
            'message':'private-book-quotation','locations':[{'path':'EPUB/cover.xhtml','line':8,'column':3}]}],
            'checker':{'nError':1,'nFatal':0,'nWarning':0}}))
        return SimpleNamespace(returncode=1,stdout='',stderr='')
    with patch('app.engine.epub_validation.subprocess.run',side_effect=run):
        result=validate_epub(epub,jar)
    assert not result.passed
    serialized=json.dumps(result.summary())
    assert 'RSC-007' in serialized and 'cover.xhtml' in serialized
    assert 'private-book-quotation' not in serialized


def test_paid_cancelled_upload_keeps_original_order(store,tmp_path):
    source=tmp_path/'book.epub';source.write_bytes(b'paid-book')
    params=dict(user_id=None,client_session='session-a',paths=[source],options={})
    with upload_guard(store,**params) as (fingerprint,lease):
        add(store,translation_stats={'upload_fingerprint':fingerprint})
    store.settle_verified_payment('order', amount='5.99', source='verified_query')
    store.update_status('order',JobStatus.cancelled)
    with pytest.raises(HTTPException) as exc:
        with upload_guard(store,**params): pass
    assert exc.value.detail['job_id']=='order'


def test_conversion_publishes_once_and_binds_manifest_to_attempt(store,tmp_path):
    import app.job_runner as runner
    from app.models import ConversionResult
    from test_epub_fixture import minimal_epub_bytes
    source=tmp_path/'book.epub';source.write_bytes(minimal_epub_bytes())
    job=add(store,status=JobStatus.pending,input_path=str(source))
    attempt=job.translation_stats.get('attempt_id', '')
    def convert(input_path,output_path,*args,**kwargs):
        output_path.write_bytes(minimal_epub_bytes())
        return ConversionResult(validation_passed=True,message='done',
                                translation_stats={'epub_validation':{'passed':True}})
    with patch.object(runner,'job_store',store),patch.object(runner,'OUTPUT_DIR',tmp_path),patch.object(
            runner.converter,'convert_file_to_horizontal',side_effect=convert) as convert_call,patch.object(
            runner,'notify_job_completed') as notify:
        runner.run_job(job.id,attempt)
        runner.run_job(job.id,attempt)
    completed=store.get(job.id)
    assert completed.status==JobStatus.success,completed.message
    assert convert_call.call_count==1
    assert notify.call_count==1
    assert matches(completed.output_path,completed.translation_stats['artifact_manifest'])
    assert completed.translation_stats['artifact_manifest']['attempt_id']==attempt
    assert completed.translation_stats['epub_validation']['passed'] is True


def test_success_without_validation_evidence_is_rejected():
    from app.models import ConversionResult
    from app.domain.status_resolver import resolve_after_conversion
    assert resolve_after_conversion(ConversionResult())[0]==JobStatus.failed
    assert resolve_after_conversion(ConversionResult(validation_passed=True,error_code='TRANSLATION_FAILED'))[0]==JobStatus.failed


def test_old_worker_write_is_serialized_with_restart(store):
    """Hold an old result after it loads; a restart must not slip past that write."""
    import app.storage_db as database
    add(store,status=JobStatus.failed,enable_translation=True,translation_stats={'attempt_id':'old'})
    entered,release,restarted=threading.Event(),threading.Event(),threading.Event()
    original_get=database.Session.get
    errors=[]
    def get(session,model,key,*args,**kwargs):
        value=original_get(session,model,key,*args,**kwargs)
        if model is JobRecord and threading.current_thread().name=='old-writer':
            entered.set()
            assert release.wait(5)
        return value
    def old():
        try: store.update_status('order',JobStatus.failed,'old result',expected_attempt_id='old')
        except Exception as exc: errors.append(exc)
    def restart():
        try:
            store.restart_translation_attempt('order',attempt_id='new',action_label='retry',
                max_free_retries=3,started_at=datetime.now(timezone.utc))
            restarted.set()
        except Exception as exc: errors.append(exc)
    with patch.object(database.Session,'get',get):
        writer=threading.Thread(target=old,name='old-writer');writer.start()
        assert entered.wait(5)
        restarter=threading.Thread(target=restart);restarter.start()
        assert not restarted.wait(.1)
        release.set();writer.join(5);restarter.join(5)
    assert not errors
    assert restarted.is_set()
    current=store.get('order')
    assert current.status==JobStatus.pending
    assert current.translation_stats['attempt_id']=='new'
    store.update_status('order',JobStatus.success,'stale',expected_attempt_id='old')
    assert store.get('order').status==JobStatus.pending


def test_paid_polish_cannot_succeed_from_conversion_alone(store,tmp_path):
    import app.job_runner as runner
    from app.models import ConversionResult
    from test_epub_fixture import minimal_epub_bytes
    source=tmp_path/'book.epub';source.write_bytes(minimal_epub_bytes())
    job=add(store,status=JobStatus.pending,input_path=str(source),enable_precision_polish=True)
    def convert(input_path,output_path,*args,**kwargs):
        output_path.write_bytes(minimal_epub_bytes())
        return ConversionResult(validation_passed=True,message='format passed',
            translation_stats={'epub_validation':{'passed':True}})
    with patch.object(runner,'job_store',store),patch.object(runner,'OUTPUT_DIR',tmp_path),patch.object(
            runner.converter,'convert_file_to_horizontal',side_effect=convert),patch.object(
            runner,'notify_job_completed'),patch.object(runner,'report_error'):
        runner.run_job(job.id,job.translation_stats.get('attempt_id', ''))
    done=store.get(job.id)
    assert done.status==JobStatus.failed
    assert done.error_code=='PRECISION_POLISH_FAILED'
    assert done.output_path is None
    assert not done.translation_stats.get('artifact_manifest')
    assert done.translation_stats['precision_polish']['status'] == 'failed'


@pytest.mark.parametrize("manifest", [{}, [], "invalid", {"bytes": "4", "sha256": "invalid"}])
def test_malformed_receipt_cannot_be_treated_as_a_legacy_artifact(tmp_path, manifest):
    path = tmp_path / "output.epub"
    path.write_bytes(b"good")
    assert not matches(path, manifest)
    assert matches(path, None)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
