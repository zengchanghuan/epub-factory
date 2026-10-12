#!/usr/bin/env python3
"""Run delivery regressions with dotenv disabled and Python network access blocked.

Use the backend Python environment with pytest installed. Optional arguments are
passed to pytest; the default suite covers delivery faults and affected callers.
"""
import os
from pathlib import Path
import socket
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SUITES = [
    'test_d43_delivery_reliability.py', 'test_d10_status_resolver.py',
    'test_d3_api_v2_skeleton.py', 'test_d4_celery_job_pipeline.py',
    'test_d19_order_admin.py', 'test_d20_checkout_webhook.py',
    'test_d20_translation_confirmation.py', 'test_d21_real_order_recovery.py',
    'test_d24_delivery_policy.py', 'test_d31_epub_validation.py',
    'test_d31_llm_usage_ledger.py', 'test_d35_payment_reconcile.py',
    'test_d35_payment_hooks.py', 'test_d16_translation_qa_service.py',
    'test_d37_pdf_api.py',
    'test_d12_translation_enhancement.py', 'test_d18_paid_translation_regression.py',
    'test_d25_navigation_numeric.py',
    'test_d47_chinese_title_qa.py', 'test_d48_continuation_store.py',
    'test_d49_translation_slicing.py', 'test_d50_timeout_continuation.py',
    'test_d51_timeout_propagation.py',
]


def main():
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT / 'backend'))
    with tempfile.TemporaryDirectory(prefix='fixepub-delivery-tests-') as directory:
        os.environ.update(DATABASE_URL=f'sqlite:///{directory}/jobs.db',
                          OPENAI_API_KEY='offline-test', REDIS_URL='',
                          CELERY_BROKER_URL='', SKIP_PAYMENT_CHECK='1')
        import dotenv
        dotenv.load_dotenv = lambda *args, **kwargs: False

        def offline(*args, **kwargs):
            raise AssertionError('Network forbidden in delivery regression tests')
        socket.socket.connect = offline
        socket.create_connection = offline
        import pytest
        return pytest.main(sys.argv[1:] or ['-q', *[f'backend/{name}' for name in SUITES]])


if __name__ == '__main__':
    raise SystemExit(main())
