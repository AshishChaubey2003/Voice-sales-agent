import asyncio

import pytest

from voice.limits import CallLimiter, call_duration_watchdog, run_with_call_guards


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeRunner:
    """Stands in for pipecat's WorkerRunner: records run/cancel without a pipeline."""

    def __init__(self, *, run_blocks_until_cancelled: bool = False):
        self.ran = False
        self.cancelled = False
        self._cancel_reason = None
        self._run_blocks = run_blocks_until_cancelled
        self._cancel_event = asyncio.Event()

    async def run(self):
        self.ran = True
        if self._run_blocks:
            await self._cancel_event.wait()

    async def cancel(self, reason=None):
        self.cancelled = True
        self._cancel_reason = reason
        self._cancel_event.set()


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


async def _noop_cleanup(bucket):
    bucket.append("cleaned")


@pytest.mark.anyio
async def test_guards_run_the_call_and_release_the_slot():
    limiter = CallLimiter(max_concurrent=1)
    runner = FakeRunner()
    cleaned = []

    result = await run_with_call_guards(
        runner=runner,
        limiter=limiter,
        max_call_seconds=None,
        cleanup=lambda: _noop_cleanup(cleaned),
    )

    assert result == "ran"
    assert runner.ran is True
    assert runner.cancelled is False
    assert limiter.active == 0            # slot released in the finally
    assert cleaned == ["cleaned"]         # cleanup ran exactly once


@pytest.mark.anyio
async def test_guards_refuse_a_call_over_the_limit_without_running_it():
    limiter = CallLimiter(max_concurrent=1)
    assert limiter.try_acquire() is True  # the one allowed call is already active

    runner = FakeRunner()
    cleaned = []

    result = await run_with_call_guards(
        runner=runner,
        limiter=limiter,
        max_call_seconds=None,
        cleanup=lambda: _noop_cleanup(cleaned),
    )

    assert result == "refused"
    assert runner.ran is False            # the refused call never runs the pipeline
    assert runner.cancelled is True       # it is cancelled instead
    assert limiter.active == 1            # the pre-existing call's slot is untouched
    assert cleaned == ["cleaned"]         # cleanup still ran


@pytest.mark.anyio
async def test_guards_release_the_slot_even_when_run_raises():
    limiter = CallLimiter(max_concurrent=1)

    class ExplodingRunner(FakeRunner):
        async def run(self):
            self.ran = True
            raise RuntimeError("pipeline blew up")

    cleaned = []
    with pytest.raises(RuntimeError):
        await run_with_call_guards(
            runner=ExplodingRunner(),
            limiter=limiter,
            max_call_seconds=None,
            cleanup=lambda: _noop_cleanup(cleaned),
        )

    assert limiter.active == 0            # slot released despite the crash
    assert cleaned == ["cleaned"]


@pytest.mark.anyio
async def test_duration_watchdog_ends_a_long_running_call():
    limiter = CallLimiter(max_concurrent=1)
    runner = FakeRunner(run_blocks_until_cancelled=True)
    cleaned = []

    # A tiny limit stands in for the configured one; the watchdog should fire,
    # call runner.cancel(), which unblocks run(), and the guard then cleans up.
    result = await run_with_call_guards(
        runner=runner,
        limiter=limiter,
        max_call_seconds=1,
        cleanup=lambda: _noop_cleanup(cleaned),
    )

    assert result == "ran"
    assert runner.cancelled is True
    assert limiter.active == 0
    assert cleaned == ["cleaned"]
