#!/usr/bin/env python3
"""Local-only real-book preview smoke fixture. Isolated DB, GET only, no LLM/payments."""
import argparse
import os
from pathlib import Path
import sys
import tempfile
import sqlite3

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('book', type=Path)
    args = parser.parse_args()
    book = args.book.resolve()
    if not book.is_file() or book.suffix.lower() != '.epub': parser.error('Existing EPUB required')
    with tempfile.TemporaryDirectory(prefix='epub-preview-smoke-') as runtime:
        os.environ.update(DATABASE_URL='sqlite:///' + runtime + '/jobs.db',
                          OPENAI_API_KEY='offline-smoke-only', ALIPAY_APP_ID='',
                          CELERY_BROKER_URL='', REDIS_URL='',
                          DOWNLOAD_SIGN_SECRET='local-smoke-not-production')
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root/'backend'))
        from app import main
        from app.models import Job, JobStatus, OutputMode
        from fastapi.responses import HTMLResponse, JSONResponse
        import uvicorn
        main.BASE_DIR = Path(runtime)
        main.job_store.add(Job(id='local-preview-smoke', trace_id='isolated-local-fixture',
                               source_filename=book.name, input_path=str(book), output_path=str(book),
                               output_mode=OutputMode.simplified, status=JobStatus.success,
                               creator_ip='127.0.0.1'))
        fixture_owner = {}
        @main.app.middleware('http')
        async def fixture_only(request, call_next):
            browser_session = request.headers.get('x-client-session')
            # Bind only this ephemeral fixture to the first normal browser
            # session. Never insert tokens or change production authorization.
            if request.url.path == '/api/v2/jobs' and browser_session and not fixture_owner:
                fixture_owner['session'] = browser_session
                with sqlite3.connect(Path(runtime)/'jobs.db') as connection:
                    connection.execute('UPDATE epub_jobs SET creator_session=? WHERE id=?', (fixture_owner['session'], 'local-preview-smoke'))
            if request.method != 'GET':
                return JSONResponse({'detail':'隔离预览测试不允许提交或付款'}, status_code=405)
            if request.url.path in {'/', '/index.html'}:
                page = (root/'frontend/index.html').read_text()
                page = page.replace('<head>', '<head><script>window.EPUB_FACTORY_API=location.origin;</script>', 1)
                return HTMLResponse(page, headers={'Cache-Control':'no-store'})
            return await call_next(request)
        print('Isolated local real-book preview: http://127.0.0.1:18880/ (GET only)')
        uvicorn.run(main.app, host='127.0.0.1', port=18880, log_level='warning')
