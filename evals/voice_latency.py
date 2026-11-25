"""Aggregates the voice latency telemetry that voice/turn_recorder.py already stores.

This is live-call telemetry, not an evaluation run: nothing here is scored, and it
only has data for calls that actually happened. Same metric keys and config label
as scripts/latency_report.py; percentiles follow evals.metrics.latency_summary.
"""

from __future__ import annotations

from collections import defaultdict

from evals.metrics import latency_summary

ROLE_METRICS = {
    "user": ["stt_ttfb_ms"],
    "assistant": ["rag_ms", "llm_ttfb_ms", "tts_ttfb_ms", "voice_to_voice_ms", "first_speech_ms"],
}
REPORT_ORDER = [
    "stt_ttfb_ms",
    "rag_ms",
    "llm_ttfb_ms",
    "tts_ttfb_ms",
    "voice_to_voice_ms",
    "first_speech_ms",
]


def config_label(latency: dict) -> str:
    return (
        f"reasoning={latency.get('reasoning_effort', 'unknown')} | "
        f"tts={latency.get('tts_provider', 'groq')} | rag={latency.get('rag', 'off')}"
    )


def summarize_voice_latency(rows) -> dict:
    """rows: iterable of (role, latency_dict). Returns {label: {metric: summary}}."""
    groups: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    interrupted = 0
    assistant_turns = 0
    for role, latency in rows:
        if not isinstance(latency, dict):
            continue
        label = config_label(latency)
        for key in ROLE_METRICS.get(role, []):
            value = latency.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                groups[label][key].append(value)
        if role == "assistant":
            assistant_turns += 1
            if latency.get("interrupted") is True:
                interrupted += 1

    return {
        "configs": {
            label: {key: latency_summary(groups[label][key]) for key in REPORT_ORDER if groups[label][key]}
            for label in sorted(groups)
        },
        "assistant_turns": assistant_turns,
        "interrupted_turns": interrupted,
        "interruption_rate": (interrupted / assistant_turns) if assistant_turns else None,
    }
