"""RAG and voice-timing aggregation of live turns, and the dashboard wiring. No database."""

from pathlib import Path

from api import live_eval

ROOT = Path(__file__).resolve().parent.parent


def row(scores=None, state="measured", empty=False, channel="text", latency=None):
    return {"scores": scores or {}, "retrieval": {"state": state, "empty": empty if state == "measured" else None},
            "channel": channel, "latency": latency or {}}


def test_rag_metrics_use_their_own_denominators_and_never_pad_with_zero():
    rows = [row({"recall_at_4": 1.0, "mrr": 1.0}), row({"recall_at_4": 0.5, "mrr": 0.5}),
            row(empty=True), row(state="not_instrumented"), row(state="error")]
    out = live_eval.rag_metrics(rows)
    assert out["recall_at_4"] == {"value": 0.75, "n": 2, "n_unit": "graded turns with expected sources"}
    assert out["empty_retrieval_rate"] == {"value": 1 / 3, "n": 3, "n_unit": "turns with recorded retrieval"}
    assert out["retrieval_not_recorded"]["not_instrumented"] == 1 and out["retrieval_not_recorded"]["error"] == 1
    none = live_eval.rag_metrics([row(state="not_instrumented")])
    assert none["recall_at_4"]["value"] is None and none["empty_retrieval_rate"]["value"] is None


def test_voice_timings_ignore_chat_turns_booleans_and_unrecorded_stages():
    rows = [row(channel="voice", latency={"llm_ttfb_ms": 400, "rag_ms": 30}),
            row(channel="voice", latency={"llm_ttfb_ms": 600, "interrupted": True, "tts_ttfb_ms": True}),
            row(channel="text", latency={"llm_ttfb_ms": 9999})]
    out = live_eval.voice_timings(rows)
    assert out["voice_turns"] == 2 and out["stages"]["llm_ttfb_ms"]["n"] == 2
    assert out["stages"]["tts_ttfb_ms"] is None  # a boolean is not a duration
    assert out["stages"]["voice_to_voice_ms"] is None and out["stages"]["first_speech_ms"] is None
    assert "p90" in out["stages"]["llm_ttfb_ms"] and out["stages"]["llm_ttfb_ms"]["p90"] is None  # n=2: percentile withheld


def test_dashboard_has_a_separate_live_section_with_provisional_labelling():
    html = (ROOT / "api" / "static" / "eval_dashboard.html").read_text(encoding="utf-8")
    for needle in ("live-card", "/v1/evals/live/summary", "/v1/evals/live/turns", "/v1/evals/live/refresh",
                   "provisional", "assistant turns", "Not an offline evaluation run"):
        assert needle in html, needle
    for forbidden in ("innerHTML", "eval(", "http://", "cdn."):
        assert forbidden not in html.replace("http:" + "//www", ""), forbidden
