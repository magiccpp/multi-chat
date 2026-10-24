"""Run coroutines from Qt slots on the qasync loop, reporting failures instead of losing them."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

log = logging.getLogger("multichat.desktop")
_error_handler: Callable[[BaseException], None] | None = None


def set_error_handler(handler: Callable[[BaseException], None]) -> None:
    global _error_handler
    _error_handler = handler


def spawn(coro: Coroutine[Any, Any, Any], on_error: Callable[[BaseException], None] | None = None) -> asyncio.Task:
    task = asyncio.ensure_future(coro)

    def done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            handler = on_error or _error_handler
            if handler:
                handler(exc)
            else:
                log.error("background task failed", exc_info=exc)

    task.add_done_callback(done)
    return task
