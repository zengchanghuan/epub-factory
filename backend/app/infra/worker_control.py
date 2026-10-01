"""Keep fixed worker roles intact without disabling read-only inspection."""
from functools import wraps

from celery.worker.control import Panel


_FIXED_ROLE_COMMANDS = (
    "add_consumer", "cancel_consumer", "pool_grow", "pool_shrink", "autoscale",
)


def register_worker_control_guards() -> None:
    """Wrap Celery's existing handlers once, checking the receiving app per call.

    Panel is a process-global registry. Do not disable commands globally: other
    Celery apps in the same interpreter must retain their original behavior.
    The isolation flag also covers explicit ``-Q`` workers without a role label.
    """
    for name in _FIXED_ROLE_COMMANDS:
        original = Panel.data[name]
        if getattr(original, "_epub_fixed_role_guard", False):
            continue

        def wrap_handler(handler, command):
            @wraps(handler)
            def guarded(state, *args, **kwargs):
                app = getattr(state, "app", None)
                if app is not None and app.conf.get("epub_queue_isolation", False):
                    return {"error": f"FixEpub fixed worker roles prohibit {command}; restart with the configured role instead"}
                return handler(state, *args, **kwargs)

            guarded._epub_fixed_role_guard = True
            return guarded

        # Preserve parsing, aliases, help and command type in Celery's own
        # registry; inspect/ping and unrelated management handlers are untouched.
        Panel.register(wrap_handler(original, name), name=name, **Panel.meta[name]._asdict())
