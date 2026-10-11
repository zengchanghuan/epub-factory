"""Explicit real-book quote/refresh/retry/execution-boundary regression.

Payment and translation boundaries are stubs. No production orders, model
requests, restored databases or generated translated books are involved.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from test_d83_quote_execution import QuoteExecutionTests


def fingerprint(path):
    return path.stat().st_size, path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--book', nargs=2, action='append', required=True, metavar=('PATH', 'SHA256'))
    args = parser.parse_args()
    reports = []
    for configured, expected in args.book:
        path = Path(configured).resolve(strict=True)
        before = fingerprint(path)
        assert before[2] == expected
        case = QuoteExecutionTests()
        try:
            case.setUp()
            prices = []
            for policy in ('fresh', 'reuse', 'verified'):
                job = case.job_from(case.create(payload=path.read_bytes(), profile_confirmation='false',
                    cache_policy=policy, translation_model='deepseek-flash', translation_quality='standard'))
                quote = copy.deepcopy(job.translation_stats['translation_pricing'])
                assert quote['execution_basis']['source_sha256'] == expected
                from app.domain.translation_quote import normalized_content_digest
                assert quote['execution_basis']['normalized_content_sha256'] == normalized_content_digest(path)
                assert quote['cached_chars'] == 0 and quote['billable_chars'] == quote['total_chars']
                assert quote['hit_ratio'] is None
                prices.append(job.expected_amount)
                with patch.object(case.main, 'job_store', case.PersistentJobStore(case.engine)):
                    response = case.client.get('/jobs/' + job.id, headers={'X-Job-Token': job.access_token})
                assert response.status_code == 200
                assert response.json()['pricing'] == quote
                job = case.authorize(job)
                def stop_before_translation(**kwargs):
                    actual = Path(kwargs['input_path'])
                    assert hashlib.sha256(actual.read_bytes()).hexdigest() == expected
                    assert kwargs['job'].translation_stats['translation_pricing'] == quote
                    raise RuntimeError('history-execution-boundary-captured')
                finished, executor, _ = case.execute(job, stop_before_translation)
                executor.assert_called_once()
                assert finished.message == 'history-execution-boundary-captured'
                response = case.client.post('/api/v2/jobs/' + job.id + '/restart-translation?cache_policy=reuse',
                    headers={'X-Job-Token': job.access_token})
                assert response.status_code == 200, response.text
                retry = case.store.get(job.id)
                assert retry.expected_amount == job.expected_amount
                assert retry.translation_stats['translation_pricing'] == quote
                finished, executor, _ = case.execute(retry, stop_before_translation)
                executor.assert_called_once()
                assert finished.message == 'history-execution-boundary-captured'
                assert finished.translation_stats['translation_pricing'] == quote
            assert len(set(prices)) == 1
            # A real book with an accidentally changed product cannot consume
            # requests under its unchanged original quote.
            job = case.authorize(case.job_from(case.create(payload=path.read_bytes(), profile_confirmation='false',
                translation_model='deepseek-flash', translation_quality='standard')))
            changed = case.alter_job(job, translation_quality='literary')
            finished, executor, _ = case.execute(changed)
            executor.assert_not_called()
            assert '与原报价不一致' in finished.message
            case.tearDown()
            reports.append({'book_sha256': expected, 'policies_tested': 3, 'refreshes': 3,
                            'execution_boundary_checks': 6, 'retries': 3, 'product_drift_blocked': True})
        finally:
            case.doCleanups()
        assert fingerprint(path) == before
    print(json.dumps({'books': reports, 'real_model_calls': 0, 'originals_unchanged': True}))


if __name__ == '__main__':
    main()
