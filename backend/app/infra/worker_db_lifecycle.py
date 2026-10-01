"""Keep SQLAlchemy connections out of Celery prefork children.

SQLite WAL connections inherited across fork can fail even on the first read.
Close idle parent connections before pool startup and before subsequent async
forks, then replace the child's pool without touching parent connections.
The blocking prefork pool does not emit worker_before_create_process, so its
initial parent cleanup must use worker_init (after imports, before pool setup).
Engine.dispose(close=False) is supported
by SQLAlchemy for this child initializer use case; it alone is insufficient for
the inherited SQLite WAL handles reproduced by the process regression tests.
"""
import sys

from celery.signals import worker_init, worker_before_create_process, worker_process_init


def _known_engines():
    """Inspect already-loaded components only; no DB initialization or imports."""
    found = []
    store_module = sys.modules.get("app.storage")
    store = getattr(store_module, "job_store", None)
    found.append(getattr(store, "_engine", None))
    # Normally the ledger shares the store engine. Include previously warmed
    # separate ledger engines, without initializing its lazy default connection.
    ledger_module = sys.modules.get("app.infra.llm_usage_ledger")
    if ledger_module is not None:
        found.append(getattr(ledger_module, "_default_engine", None))
        found.extend(list(getattr(ledger_module, "_ledgers", {})))
    seen = set()
    for engine in found:
        if engine is not None and id(engine) not in seen:
            seen.add(id(engine))
            yield engine


def before_worker_fork(**_kwargs):
    for engine in _known_engines():
        # The worker parent owns only setup/idle connections. Checked-out
        # connections are not closed by dispose; no application data is changed.
        engine.dispose(close=True)


def after_worker_fork(**_kwargs):
    for engine in _known_engines():
        engine.dispose(close=False)


def register_worker_db_lifecycle():
    worker_init.connect(before_worker_fork, weak=False, dispatch_uid="epub.db.before_worker_pool")
    worker_before_create_process.connect(before_worker_fork, weak=False, dispatch_uid="epub.db.before_worker_fork")
    worker_process_init.connect(after_worker_fork, weak=False, dispatch_uid="epub.db.after_worker_fork")
