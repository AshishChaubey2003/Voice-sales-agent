"""In-process guards that bound how much a public demo can spend.

One Pipecat runner process handles every incoming call, so a single shared
``CallLimiter`` instance caps how many pipelines run at once, and a per-call
duration watchdog stops a call that runs too long (for example a browser tab
left open). Both exist to protect paid Groq, Deepgram and TURN quotas on a
public URL. Neither is authentication — see DEPLOYMENT.md for that.
"""

import asyncio
from collections.abc import Awaitable, Callable

from loguru import logger


class CallLimiter:
    """Bounds concurrent calls. try_acquire() is non-blocking and thread-free."""

    def __init__(self, max_concurrent: int) -> None:
        # 0 or negative would reject every call; treat it as "no limit off".
        self._max = max(1, int(max_concurrent))
        self._active = 0

    @property
    def active(self) -> int:
        return self._active

    @property
    def max_concurrent(self) -> int:
        return self._max

    def try_acquire(self) -> bool:
        if self._active >= self._max:
            return False
        self._active += 1
        return True

    def release(self) -> None:
        # Never drop below zero even if release is called twice on an error path.
        self._active = max(0, self._active - 1)


async def call_duration_watchdog(
    max_seconds: int | None,
    on_expired: Callable[[], Awaitable[None]],
) -> None:
    """Awaits max_seconds, then runs on_expired once. Cancelled when the call ends.

    A None or non-positive limit means no cap, so the watchdog returns at once
    and never fires.
    """
    if not max_seconds or max_seconds <= 0:
        return
    # A CancelledError (the call ended first) propagates and on_expired never runs.
    await asyncio.sleep(max_seconds)
    logger.info(f"Call reached the {max_seconds}s limit; ending it")
    await on_expired()
