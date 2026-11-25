"""Stage 3 timing capture: what the recorder, the Pipecat observer and the retrieval step store.

Everything here is MOCKED at the edges (fake frames, fake search, fake clock-free values): no microphone, STT,
TTS or LangSmith is involved. These tests prove the plumbing and units; they do not prove that a real Groq
call reports these metrics.
"""

import asyncio
import types
import uuid

import pytest
from pipecat.frames.frames import MetricsFrame
from pipecat.metrics.metrics import (
    ProcessingMetricsData,
    TTFAMetricsData,
    TTFATMetricsData,
    TTFBMetricsData,
)
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frame_processor import FrameDirection

from api.tracing import TurnTrace
from voice.metrics_observer import ServiceTTFBObserver
from voice.rag import KnowledgeInjector
from voice.turn_recorder import VoiceTurnRecorder


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Store:
    def __init__(self):
        self.saved = []

    async def save(self, role, content, latency):
        self.saved.append((role, content, latency))


def pushed(frame):
    return FramePushed(source=None, destination=None, frame=frame, direction=FrameDirection.DOWNSTREAM, timestamp=0)


# ---- recorder: units, first-wins vs summed, missing values ----------------------------------------

@pytest.mark.anyio
async def test_new_timings_are_stored_in_milliseconds_with_the_right_aggregation():
    store = Store()
    recorder = VoiceTurnRecorder(store.save)
    recorder.record_service_metric("processing", "GroqSTTService#0", 0.31)
    await recorder.user_turn_finished("hello")

    recorder.record_service_metric("ttfat", "GroqLLMService#0", 0.9)
    recorder.record_service_metric("ttfat", "GroqLLMService#0", 2.0)        # second inference: first one wins
    recorder.record_service_metric("processing", "GroqLLMService#0", 0.5)
    recorder.record_service_metric("processing", "GroqLLMService#0", 0.25)  # tool call, then answer: summed
    recorder.record_service_metric("ttfa", "GroqTTSService#0", 0.4)
    recorder.record_service_metric("ttfa", "GroqTTSService#0", 0.9)
    recorder.record_service_metric("processing", "GroqTTSService#0", 0.2)
    recorder.record_service_metric("processing", "GroqTTSService#0", 0.3)   # one request per sentence: summed
    await recorder.assistant_turn_finished("Hi there.", interrupted=False)

    assert store.saved[0] == ("user", "hello", {"stt_processing_ms": 310})
    assistant = store.saved[1][2]
    assert assistant["llm_ttfat_ms"] == 900 and assistant["llm_processing_ms"] == 750
    assert assistant["tts_ttfa_ms"] == 400 and assistant["tts_processing_ms"] == 500


@pytest.mark.anyio
async def test_unreported_malformed_or_misrouted_metrics_stay_absent_and_never_raise():
    store = Store()
    recorder = VoiceTurnRecorder(store.save)
    for args in [("processing", "GroqLLMService#0", None), ("processing", "GroqLLMService#0", 0),
                 ("processing", "GroqLLMService#0", -1), ("processing", "GroqLLMService#0", True),
                 ("processing", "GroqLLMService#0", "x"), ("processing", "GroqLLMService#0", float("nan")),
                 ("ttfa", "GroqLLMService#0", 0.5),        # TTFA belongs to TTS only
                 ("ttfat", "GroqTTSService#0", 0.5),       # TTFAT belongs to the LLM only
                 ("processing", "SomethingElse#0", 0.5), ("bogus", "GroqLLMService#0", 0.5),
                 ("processing", None, 0.5)]:
        recorder.record_service_metric(*args)
    await recorder.assistant_turn_finished("Reply.", interrupted=False)
    assert store.saved[0][2] == {"interrupted": False}


@pytest.mark.anyio
async def test_values_never_leak_between_consecutive_turns_or_between_calls():
    store_a, store_b = Store(), Store()
    call_a, call_b = VoiceTurnRecorder(store_a.save), VoiceTurnRecorder(store_b.save)
    call_a.record_service_metric("processing", "GroqLLMService#0", 1.0)
    call_b.record_service_metric("processing", "GroqLLMService#0", 2.0)
    call_a.record_retrieval_detail(search_ms=5, rewrite_ms=7, error=True, trace_run_id="run-a")
    await asyncio.gather(call_a.assistant_turn_finished("A1", False), call_b.assistant_turn_finished("B1", False))
    await call_a.assistant_turn_finished("A2", False)

    assert store_a.saved[0][2]["llm_processing_ms"] == 1000 and store_b.saved[0][2]["llm_processing_ms"] == 2000
    assert "trace_run_id" not in store_b.saved[0][2] and "rag_error" not in store_b.saved[0][2]
    assert store_a.saved[1][2] == {"interrupted": False}  # the next turn starts clean


@pytest.mark.anyio
async def test_retrieval_detail_and_trace_id_are_stored_only_when_present():
    store = Store()
    recorder = VoiceTurnRecorder(store.save)
    recorder.record_retrieval(120, ["A > B"], "standalone question")
    recorder.record_retrieval_detail(search_ms=80, rewrite_ms=40, error=False, trace_run_id=None)
    await recorder.assistant_turn_finished("Reply.", interrupted=False)
    latency = store.saved[0][2]
    assert latency["rag_ms"] == 120 and latency["rag_search_ms"] == 80 and latency["rag_rewrite_ms"] == 40
    assert "trace_run_id" not in latency and "rag_error" not in latency  # not invented, not False

    recorder.record_retrieval_detail(search_ms=0, rewrite_ms=None, error=True, trace_run_id="11111111-2222")
    await recorder.assistant_turn_finished("Again.", interrupted=False)
    again = store.saved[1][2]
    assert again["rag_error"] is True and again["trace_run_id"] == "11111111-2222" and "rag_rewrite_ms" not in again


def _breakdown(parts, measured_from="user_silence", aggregation_secs=None):
    return types.SimpleNamespace(
        contributions=[types.SimpleNamespace(key=k, duration_secs=v) for k, v in parts],
        measured_from=types.SimpleNamespace(value=measured_from),
        text_aggregation=None if aggregation_secs is None else types.SimpleNamespace(duration_secs=aggregation_secs),
    )


@pytest.mark.anyio
async def test_breakdown_is_stored_with_its_anchor_and_bad_input_is_dropped():
    store = Store()
    recorder = VoiceTurnRecorder(store.save)
    recorder.record_breakdown(_breakdown([("silence", 0.6), ("llm", 0.9), ("zero", 0.0)], aggregation_secs=0.12))
    await recorder.assistant_turn_finished("Reply.", interrupted=False)
    latency = store.saved[0][2]
    assert latency["latency_breakdown"] == [{"key": "silence", "ms": 600}, {"key": "llm", "ms": 900}]
    assert latency["breakdown_from"] == "user_silence" and latency["text_aggregation_ms"] == 120

    recorder.record_breakdown(object())              # not a breakdown at all
    recorder.record_breakdown(_breakdown([]))        # nothing measured
    await recorder.assistant_turn_finished("Next.", interrupted=False)
    assert "latency_breakdown" not in store.saved[1][2]


@pytest.mark.anyio
async def test_a_failing_save_does_not_leak_state_into_the_next_turn():
    calls = []

    async def failing_save(role, content, latency):
        calls.append(latency)
        raise RuntimeError("db down")

    recorder = VoiceTurnRecorder(failing_save)
    recorder.record_service_metric("processing", "GroqLLMService#0", 1.0)
    with pytest.raises(RuntimeError):
        await recorder.assistant_turn_finished("Reply.", interrupted=False)
    # the metrics were cleared before saving, so a retry of the next turn cannot inherit them
    recorder.record_service_metric("processing", "GroqLLMService#0", 2.0)
    with pytest.raises(RuntimeError):
        await recorder.assistant_turn_finished("Next.", interrupted=False)
    assert calls[1]["llm_processing_ms"] == 2000


# ---- Pipecat observer ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_observer_forwards_each_metric_kind_once_in_seconds():
    ttfb, extra = [], []
    observer = ServiceTTFBObserver(lambda p, v: ttfb.append((p, v)), on_metric=lambda k, p, v: extra.append((k, p, v)))
    frame = MetricsFrame(data=[
        TTFBMetricsData(processor="GroqLLMService#0", value=0.4),
        TTFATMetricsData(processor="GroqLLMService#0", ttfat=0.9, ttfb=0.4, thinking_time=0.5),
        TTFAMetricsData(processor="GroqTTSService#0", ttfa=0.7, ttfb=0.5, leading_silence=0.2),
        ProcessingMetricsData(processor="GroqLLMService#0", value=1.2),
    ])
    await observer.on_push_frame(pushed(frame))
    await observer.on_push_frame(pushed(frame))  # the same frame passes many hops: counted once
    assert ttfb == [("GroqLLMService#0", 0.4)]
    assert extra == [("ttfat", "GroqLLMService#0", 0.9), ("ttfa", "GroqTTSService#0", 0.7),
                     ("processing", "GroqLLMService#0", 1.2)]


@pytest.mark.anyio
async def test_observer_without_on_metric_behaves_as_before_and_callback_errors_do_not_escape():
    ttfb = []
    plain = ServiceTTFBObserver(lambda p, v: ttfb.append((p, v)))
    await plain.on_push_frame(pushed(MetricsFrame(data=[
        TTFBMetricsData(processor="GroqSTTService#0", value=0.5),
        ProcessingMetricsData(processor="GroqSTTService#0", value=0.2)])))
    assert ttfb == [("GroqSTTService#0", 0.5)]

    def boom(*args):
        raise RuntimeError("telemetry bug")

    noisy = ServiceTTFBObserver(boom, on_metric=boom)
    await noisy.on_push_frame(pushed(MetricsFrame(data=[
        TTFBMetricsData(processor="x", value=0.1), ProcessingMetricsData(processor="x", value=0.1)])))


# ---- retrieval step: rewrite vs search split, error flag, trace id ------------------------------------

class _Settings:
    rag_top_k = 4
    rag_max_distance = 0.3
    chat_history_limit = 10


class _Context:
    def __init__(self, messages):
        self._messages = list(messages)

    def get_messages(self):
        return list(self._messages)

    def set_messages(self, messages):
        self._messages = list(messages)


def _chunk(heading):
    return types.SimpleNamespace(heading=heading, title="t", content="c", vector_distance=0.1, keyword_rank=None)


def _injector(detail, *, rewriter=object()):
    return KnowledgeInjector(
        session_factory=lambda: None, organization_id=uuid.uuid4(), settings=_Settings(), rewriter=rewriter,
        on_retrieval=lambda *a: None, on_retrieval_detail=detail, conversation_id=uuid.uuid4())


MULTI_TURN = [{"role": "user", "content": "Pricing?"}, {"role": "assistant", "content": "Which plan?"},
              {"role": "user", "content": "and the Business one?"}]


@pytest.mark.anyio
async def test_retrieval_splits_search_and_rewrite_time_and_reports_both_searches():
    seen = {}
    injector = _injector(lambda **kw: seen.update(kw))
    results = [types.SimpleNamespace(chunks=[], elapsed_ms=0, error=False),
               types.SimpleNamespace(chunks=[_chunk("Pricing > Business")], elapsed_ms=0, error=False)]

    async def fake_search(query):
        await asyncio.sleep(0.02)
        return results.pop(0)

    async def fake_rewrite(messages):
        await asyncio.sleep(0.03)
        return "What does the Business plan cost?"

    injector._search, injector._rewrite = fake_search, fake_rewrite
    await injector._inject(_Context(MULTI_TURN))
    assert seen["rewrite_ms"] >= 25 and seen["search_ms"] >= 35 and seen["error"] is False
    assert seen["trace_run_id"] is None  # tracing is off here: no id is made up


@pytest.mark.anyio
async def test_no_rewrite_means_rewrite_ms_is_none_and_a_failed_search_is_flagged():
    seen = {}
    injector = _injector(lambda **kw: seen.update(kw))

    async def failing_search(query):
        return types.SimpleNamespace(chunks=[], elapsed_ms=0, error=True)

    injector._search = failing_search
    await injector._inject(_Context(MULTI_TURN))
    assert seen["rewrite_ms"] is None and seen["error"] is True


@pytest.mark.anyio
async def test_a_failing_timing_callback_never_breaks_retrieval_or_the_old_callback():
    old = []

    def boom(**kw):
        raise RuntimeError("telemetry bug")

    injector = KnowledgeInjector(
        session_factory=lambda: None, organization_id=uuid.uuid4(), settings=_Settings(), rewriter=None,
        on_retrieval=lambda *a: old.append(a), on_retrieval_detail=boom)

    async def search(query):
        return types.SimpleNamespace(chunks=[_chunk("A > B")], elapsed_ms=3, error=False)

    injector._search = search
    context = _Context([{"role": "user", "content": "How much is Pro?"}])
    await injector._inject(context)
    assert old and old[0][1] == ["A > B"]
    assert any("A > B" in str(m.get("content")) or "Knowledge base" in str(m.get("content")) for m in context.get_messages())


@pytest.mark.anyio
async def test_the_injector_still_works_without_the_new_callback():
    injector = KnowledgeInjector(session_factory=lambda: None, organization_id=uuid.uuid4(), settings=_Settings(),
                                 rewriter=None, on_retrieval=lambda *a: None)

    async def search(query):
        return types.SimpleNamespace(chunks=[], elapsed_ms=1, error=False)

    injector._search = search
    await injector._inject(_Context([{"role": "user", "content": "Hello?"}]))


# ---- trace id: read from the run object, never invented ------------------------------------------

def test_trace_run_id_is_none_without_a_run_and_read_from_the_run_when_there_is_one():
    turn = TurnTrace()
    assert turn.run_id is None and turn.active is False
    run_id = uuid.uuid4()
    turn._run = types.SimpleNamespace(id=run_id)
    assert turn.run_id == str(run_id)
    turn._run = types.SimpleNamespace()              # a run object without an id
    assert turn.run_id is None


# ---- the wiring in bot.py is the only place these hooks are connected --------------------------------

def test_bot_wires_exactly_the_new_hooks_and_nothing_else_changed_shape():
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent / "voice" / "bot.py").read_text(encoding="utf-8")
    assert "on_retrieval_detail=recorder.record_retrieval_detail" in source
    assert "on_metric=recorder.record_service_metric" in source
    assert '"on_latency_breakdown"' in source and "recorder.record_breakdown(breakdown)" in source
