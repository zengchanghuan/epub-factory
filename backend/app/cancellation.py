from __future__ import annotations

from typing import Callable


class JobCancelled(RuntimeError):
    """Raised when a running job has been cancelled by the user."""


CancelCheck = Callable[[], bool]


def raise_if_cancelled(cancel_check: CancelCheck | None, message: str = "用户已停止翻译") -> None:
    if cancel_check and cancel_check():
        raise JobCancelled(message)
