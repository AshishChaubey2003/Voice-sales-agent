"""Latency telemetry of persisted conversations (chat and voice).

This is a dataset of its own. It is not an evaluation run and not a live-turn evaluation: nothing here is
scored, its denominators are messages that carry a timing, and it never mixes with pass/fail counts.

What every metric means is written down once, in METRICS, and travels with the numbers to the API and the
dashboard: which event starts and ends it, which clock, which side (all of it is measured on the server),
and what it does or does not include. Nothing is measured on the visitor's device, so there is no client
playback duration here.

Pure functions, no I/O. Rows come from Message.latency_ms, written by voice/turn_recorder.py (voice) and
api/chat.py (chat). Older rows simply lack the newer keys; a key that was never recorded is "not
instrumented", never zero.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable

from evals.metrics import MIN_N_FOR_P90, MIN_N_FOR_P95, MIN_N_FOR_P99
from voice.stats import percentile

# Below this many observations even a median is not reported: min and max are shown instead.
MIN_N_FOR_P50 = 5

PIPECAT_CLOCK = "wall clock (time.time) read inside Pipecat"
MONOTONIC = "monotonic (time.perf_counter)"

# key, role, channel, label, measure, source, clock, description, includes
METRICS: list[dict] = [
    # ---- voice: visitor message -------------------------------------------------------------
    dict(key="stt_ttfb_ms", channel="voice", role="user", label="STT time to first transcript",
         measure="time to first byte (TTFB)", source="Pipecat service metric", clock=PIPECAT_CLOCK,
         description="Pipecat's TTFB for the speech-to-text service: from the start of its measurement to the "
                     "service's first output. It is not the duration of the transcription."),
    dict(key="stt_processing_ms", channel="voice", role="user", label="STT processing time",
         measure="processing time (summed)", source="Pipecat service metric", clock=PIPECAT_CLOCK,
         description="Processing time the STT service reports to Pipecat, added up over the visitor's turn."),
    # ---- voice: assistant message ---------------------------------------------------------------
    dict(key="rag_ms", channel="voice", role="assistant", label="Retrieval step (search + optional rewrite)",
         measure="elapsed time", source="voice/rag.py", clock=MONOTONIC,
         description="Whole knowledge step before the model answers: search, and when the first search found "
                     "nothing, the query rewrite and the second search."),
    dict(key="rag_rewrite_ms", channel="voice", role="assistant", label="Query rewrite",
         measure="elapsed time", source="voice/rag.py", clock=MONOTONIC,
         description="The rewrite model call only. Recorded only for turns where a rewrite was attempted."),
    dict(key="rag_search_ms", channel="voice", role="assistant", label="Knowledge search",
         measure="elapsed time", source="voice/rag.py", clock=MONOTONIC,
         description="Embedding plus vector and keyword search, added up when a second search followed a rewrite."),
    dict(key="llm_ttfb_ms", channel="voice", role="assistant", label="LLM time to first output",
         measure="time to first byte (TTFB)", source="Pipecat service metric", clock=PIPECAT_CLOCK,
         description="From the LLM request to the first output of any kind, including reasoning the model streams "
                     "before its answer. First call of the turn only."),
    dict(key="llm_ttfat_ms", channel="voice", role="assistant", label="LLM time to first answer token",
         measure="time to first answer token (TTFAT)", source="Pipecat service metric", clock=PIPECAT_CLOCK,
         description="From the LLM request to the first token of the answer itself (after any reasoning). "
                     "First call of the turn only."),
    dict(key="llm_processing_ms", channel="voice", role="assistant", label="LLM generation",
         measure="processing time (summed)", source="Pipecat service metric", clock=PIPECAT_CLOCK,
         description="Processing time the LLM service reports, added up when a turn needs two calls (tool call, "
                     "then answer)."),
    dict(key="tts_ttfb_ms", channel="voice", role="assistant", label="TTS time to first audio chunk",
         measure="time to first byte (TTFB)", source="Pipecat service metric", clock=PIPECAT_CLOCK,
         description="From the first text sent to the TTS service to its first audio chunk. First request only. "
                     "Not the length of the synthesis."),
    dict(key="tts_ttfa_ms", channel="voice", role="assistant", label="TTS time to first audible audio",
         measure="time to first audible audio (TTFA)", source="Pipecat service metric", clock=PIPECAT_CLOCK,
         description="TTFB plus any leading silence the service puts before the first audible sample."),
    dict(key="tts_processing_ms", channel="voice", role="assistant", label="TTS synthesis",
         measure="processing time (summed)", source="Pipecat service metric", clock=PIPECAT_CLOCK,
         description="Processing time the TTS service reports, added up over the sentences of the reply."),
    dict(key="text_aggregation_ms", channel="voice", role="assistant", label="Sentence aggregation before TTS",
         measure="interval", source="Pipecat latency breakdown", clock=PIPECAT_CLOCK,
         description="From the first LLM token to the first complete sentence handed to TTS."),
    dict(key="voice_to_voice_ms", channel="voice", role="assistant", label="Visitor stopped speaking to bot started speaking",
         measure="end-to-end interval (server side)", source="Pipecat UserBotLatencyObserver", clock=PIPECAT_CLOCK,
         description="From the server detecting that the visitor went silent to the server starting to send the "
                     "bot's speech. Includes VAD wait, STT, retrieval, LLM and TTS. Excludes network transfer to "
                     "the browser and playback."),
    dict(key="first_speech_ms", channel="voice", role="assistant", label="Client connected to first bot speech",
         measure="interval (greeting)", source="Pipecat UserBotLatencyObserver", clock=PIPECAT_CLOCK,
         description="The greeting only: from the visitor connecting to the bot's first speech. A different "
                     "population from a normal turn, so it is reported on its own."),
    # ---- text chat -----------------------------------------------------------------------------
    dict(key="rag", channel="text", role="assistant", label="Knowledge search",
         measure="elapsed time", source="api.knowledge.search_knowledge", clock=MONOTONIC,
         description="Embedding plus vector and keyword search of the search whose results were used. Excludes "
                     "any query rewrite and an earlier search that found nothing."),
    dict(key="llm", channel="text", role="assistant", label="LLM request",
         measure="request latency (not streamed)", source="api/llm.py", clock=MONOTONIC,
         description="Wall time of the model request including network, added up when a tool call needs a second "
                     "request. The reply is not streamed, so this is the total, not a time to first token."),
]
SPEC_BY_KEY = {(m["channel"], m["key"]): m for m in METRICS}

# Keys that can sit next to a numeric value but are not durations.
_RAG_KEYS = {"rag_ms", "rag_search_ms", "rag_rewrite_ms", "rag"}

NOT_RECORDED = [
    "Failed requests are not stored as messages: a chat request that returns 503 and a voice turn that errors "
    "before any reply is spoken leave no row, so provider errors and timeouts cannot be counted here.",
    "Playback of the reply in the visitor's browser is not reported to the server: no client-side duration exists.",
    "Chat has no first-token, rewrite-only or end-to-end timing: those are not recorded on that path.",
]


def stats(values: list[float]) -> dict:
    """Percentiles only when there are enough observations; min and max always."""
    clean = sorted(float(v) for v in values if v is not None and not math.isnan(float(v)))
    n = len(clean)
    out: dict = {"n": n, "min": None, "max": None, "p50": None, "p90": None, "p95": None, "p99": None}
    if n == 0:
        return out
    out["min"], out["max"] = clean[0], clean[-1]
    if n >= MIN_N_FOR_P50:
        out["p50"] = percentile(clean, 50)
    if n >= MIN_N_FOR_P90:
        out["p90"] = percentile(clean, 90)
    if n >= MIN_N_FOR_P95:
        out["p95"] = percentile(clean, 95)
    if n >= MIN_N_FOR_P99:
        out["p99"] = percentile(clean, 99)
    return out


def _valid(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def _config_label(latency: dict) -> str:
    return (f"reasoning={latency.get('reasoning_effort', 'unknown')} | "
            f"tts={latency.get('tts_provider', 'unknown')}")


def summarize(rows: Iterable[tuple[str, str, dict | None]]) -> dict:
    """rows: (channel, role, latency_ms dict). Returns the per-channel telemetry dataset."""
    values: dict[tuple[str, str], list[float]] = defaultdict(list)
    rejected: dict[tuple[str, str], int] = defaultdict(int)
    messages: dict[tuple[str, str], int] = defaultdict(int)
    interrupted = defaultdict(int)
    retrieval_errors = defaultdict(int)
    no_timing = defaultdict(int)
    trace_ids = defaultdict(int)
    configs: dict[str, set] = defaultdict(set)
    breakdown_parts: dict[str, list[float]] = defaultdict(list)
    breakdown_turns = 0
    breakdown_other_anchor = 0

    for channel, role, latency in rows:
        if role not in ("user", "assistant"):
            continue
        messages[(channel, role)] += 1
        data = latency if isinstance(latency, dict) else {}
        specs = [m for m in METRICS if m["channel"] == channel and m["role"] == role]
        errored = role == "assistant" and data.get("rag_error") is True
        got_any = False
        for spec in specs:
            key = spec["key"]
            if key not in data:
                continue
            value = data[key]
            if key in _RAG_KEYS and errored:
                continue  # a failed search has no meaningful duration; it is counted as an error below
            if _valid(value):
                values[(channel, key)].append(value)
                got_any = True
            else:
                rejected[(channel, key)] += 1
        if role != "assistant":
            continue
        if errored:
            retrieval_errors[channel] += 1
        if not got_any:
            no_timing[channel] += 1
        if channel == "voice":
            if data.get("interrupted") is True:
                interrupted[channel] += 1
            if data.get("trace_run_id"):
                trace_ids[channel] += 1
            configs[channel].add(_config_label(data))
            parts = data.get("latency_breakdown")
            if isinstance(parts, list) and parts:
                if data.get("breakdown_from") == "user_silence":
                    breakdown_turns += 1
                    for part in parts:
                        if isinstance(part, dict) and isinstance(part.get("key"), str) and _valid(part.get("ms")):
                            breakdown_parts[part["key"]].append(part["ms"])
                else:
                    breakdown_other_anchor += 1

    channels = {}
    for channel in ("voice", "text"):
        metrics = []
        for spec in METRICS:
            if spec["channel"] != channel:
                continue
            observed = values.get((channel, spec["key"]), [])
            metrics.append({
                **spec, "unit": "ms",
                "n": len(observed),
                "n_unit": "visitor turns" if spec["role"] == "user" else "assistant turns",
                "state": "measured" if observed else "not_instrumented",
                "rejected_values": rejected.get((channel, spec["key"]), 0),
                "stats": stats(observed) if observed else None,
            })
        assistant_turns = messages.get((channel, "assistant"), 0)
        channels[channel] = {
            "messages": {"assistant": assistant_turns, "user": messages.get((channel, "user"), 0)},
            "metrics": metrics,
            "outcomes": {
                "assistant_turns": assistant_turns,
                "interrupted": interrupted.get(channel, 0) if channel == "voice" else None,
                "retrieval_errors": retrieval_errors.get(channel, 0),
                "turns_without_any_timing": no_timing.get(channel, 0),
                "with_langsmith_run_id": trace_ids.get(channel, 0) if channel == "voice" else None,
                "n_unit": "assistant turns",
            },
            "configurations_observed": sorted(configs.get(channel, [])) if channel == "voice" else [],
        }
    voice = channels["voice"]
    voice["pooled_across_configurations"] = len(voice["configurations_observed"]) > 1
    voice["breakdown"] = {
        "n_turns": breakdown_turns, "n_unit": "voice turns with a breakdown measured from user silence",
        "excluded_other_anchor": breakdown_other_anchor,
        "parts": sorted(
            ({"key": key, "n": len(vals), "stats": stats(vals)} for key, vals in breakdown_parts.items()),
            key=lambda part: -(part["stats"]["p50"] or part["stats"]["max"] or 0)),
        "note": "Named parts of one turn, as Pipecat reports them. Parts of a turn add up to its "
                "visitor-stopped-to-bot-started time; a part is listed only when it happened.",
    }
    return {
        "kind": "latency_telemetry",
        "unit_note": "Each number is a duration in milliseconds of one message. Voice and text are separate "
                     "populations; none of this is added to evaluation pass/fail counts.",
        "percentile_policy": {"p50_min_n": MIN_N_FOR_P50, "p90_min_n": MIN_N_FOR_P90, "p95_min_n": MIN_N_FOR_P95,
                              "p99_min_n": MIN_N_FOR_P99},
        "channels": channels,
        "not_recorded": NOT_RECORDED,
    }
