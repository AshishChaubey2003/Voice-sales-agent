import asyncio

import pytest

from voice.limits import CallLimiter, call_duration_watchdog


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_limiter_allows_up_to_the_maximum():
    limiter = CallLimiter(max_concurrent=2)
    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is True
    assert limiter.active == 2
    # Third call over the limit is refused.
    assert limiter.try_acquire() is False
    assert limiter.active == 2


def test_releasing_frees_a_slot():
    limiter = CallLimiter(max_concurrent=1)
    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is False
    limiter.release()
    assert limiter.active == 0
    assert limiter.try_acquire() is True


def test_release_never_goes_negative():
    limiter = CallLimiter(max_concurrent=1)
    limiter.release()
    limiter.release()
    assert limiter.active == 0


def test_zero_or_negative_maximum_is_treated_as_one():
    assert CallLimiter(max_concurrent=0).max_concurrent == 1
    assert CallLimiter(max_concurrent=-5).max_concurrent == 1


@pytest.mark.anyio
async def test_watchdog_does_nothing_without_a_limit():
    calls = []

    async def on_expired():
        calls.append(True)

    # Returns immediately; nothing fires.
    await call_duration_watchdog(None, on_expired)
    await call_duration_watchdog(0, on_expired)
    assert calls == []


@pytest.mark.anyio
async def test_watchdog_fires_after_the_limit():
    calls = []

    async def on_expired():
        calls.append(True)

    # Patch sleep so the test does not actually wait.
    from voice import limits

    async def instant_sleep(_seconds):
        return None

    original = limits.asyncio.sleep
    limits.asyncio.sleep = instant_sleep
    try:
        await call_duration_watchdog(5, on_expired)
    finally:
        limits.asyncio.sleep = original

    assert calls == [True]


@pytest.mark.anyio
async def test_watchdog_can_be_cancelled_before_it_fires():
    calls = []

    async def on_expired():
        calls.append(True)

    task = asyncio.create_task(call_duration_watchdog(3600, on_expired))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == []
