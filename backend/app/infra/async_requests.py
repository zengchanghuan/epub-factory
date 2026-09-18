"""Absolute request deadlines and prompt cancellation (not idle socket timeouts)."""
import asyncio
import time

from app.cancellation import raise_if_cancelled


async def gather_cancel_on_error(*operations):
    tasks = [asyncio.create_task(operation) for operation in operations]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def bounded_request(operation, *, timeout: float, cancel_check=None):
    task = asyncio.ensure_future(operation)
    deadline = time.monotonic() + max(0.01, timeout)
    try:
        while True:
            raise_if_cancelled(cancel_check)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError("LLM request wall timeout exceeded")
            done, _ = await asyncio.wait({task}, timeout=min(remaining, 0.5))
            if done:
                raise_if_cancelled(cancel_check)
                return task.result()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
