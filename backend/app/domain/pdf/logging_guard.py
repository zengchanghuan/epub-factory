"""Suppress HTTP SDK debug records in OCR calls, including presigned query strings.

ContextVar keeps unrelated concurrent requests' logging unchanged. Public errors
are separately replaced by fixed messages in the provider adapters.
"""
import logging
from contextlib import contextmanager
from contextvars import ContextVar

_private_request = ContextVar('pdf_private_http', default=False)


class PrivateHttpFilter(logging.Filter):
    def filter(self, record):
        return not _private_request.get()


_filter = PrivateHttpFilter()


@contextmanager
def private_http():
    names = {'httpx', 'httpcore', 'httpcore.connection', 'httpcore.http11', 'httpcore.http2',
             'httpcore.proxy', 'httpcore.socks', 'openai', 'openai._base_client', 'openai._client',
             'openai._legacy_response', 'openai._response'}
    names.update(name for name in list(logging.Logger.manager.loggerDict)
                 if name.startswith(('httpx.', 'httpcore.', 'openai.')))
    for name in names:
        logger = logging.getLogger(name)
        if _filter not in logger.filters:
            logger.addFilter(_filter)
    token = _private_request.set(True)
    try:
        yield
    finally:
        _private_request.reset(token)
