"""The remote text-only product supersedes legacy OCR/translation HTTP routes."""
import os
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
import app.main as main
from app.storage import JobStore


@pytest.mark.parametrize('translation', ['true', 'false'])
@pytest.mark.parametrize('service', ['ocr', 'translate', ''])
def test_legacy_pdf_flags_cannot_reopen_general_upload(tmp_path, translation, service):
    from test_d37_pdf_pipeline import fixture
    source = fixture(tmp_path / 'source.pdf')
    store = JobStore()
    with patch.dict(os.environ, {'PDF_TRANSLATION_ENABLED': '1', 'PDF_OCR_ENABLED': '1'}), \
         patch.object(main, 'job_store', store), patch.object(main, 'UPLOAD_DIR', tmp_path), \
         patch.object(main, 'create_alipay_page_pay') as payment:
        result = TestClient(main.app).post('/api/v2/jobs',
            files={'file': ('source.pdf', source.read_bytes(), 'application/pdf')},
            data={'enable_translation': translation, 'pdf_service': service})
    assert result.status_code == 400
    payment.assert_not_called()
    assert store.list_jobs() == []


def test_old_capability_and_image_preview_routes_are_not_exposed():
    paths = {route.path for route in main.app.routes}
    assert '/api/v2/pdf-capabilities' not in paths
    assert '/api/v2/jobs/{job_id}/pdf-preview/{page}' not in paths
    assert '/api/v2/pdf-jobs' in paths


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
