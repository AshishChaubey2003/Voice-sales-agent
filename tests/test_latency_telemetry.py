"""Latency telemetry aggregation: units, percentile gating, unavailable states, no mixing of populations."""

import json
from pathlib import Path

from evals import latency_telemetry as lt

ROOT = Path(__file__).resolve().parent.parent


def voice(**kw):
    return ("voice", "assistant", {"reasoning_effort": "low", "tts_provider": "groq", "rag": "on", **kw})


def metric(out, channel, key):
    return next(m for m in out["channels"][channel]["metrics"] if m["key"] == key)


def test_every_metric_documents_its_boundary_clock_and_source():
    assert len({(m["channel"], m["key"]) for m in lt.METRICS}) == len(lt.METRICS)
    for m in lt.METRICS:
        for field in ("key", "channel", "role", "label", "measure", "source", "clock", "description"):
            assert m.get(field), (m.get("key"), field)
    # a first-byte time is never labelled as a duration
    for key in ("stt_ttfb_ms", "llm_ttfb_ms", "tts_ttfb_ms"):
        spec = lt.SPEC_BY_KEY[("voice", key)]
        assert "first byte" in spec["measure"] and "not the" in spec["description"].lower() or "first" in spec["description"].lower()
    assert "not the duration of the transcription" in lt.SPEC_BY_KEY[("voice", "stt_ttfb_ms")]["description"]
    assert "Not the length of the synthesis" in lt.SPEC_BY_KEY[("voice", "tts_ttfb_ms")]["description"]
    assert "total, not a time to first token" in lt.SPEC_BY_KEY[("text", "llm")]["description"]
    assert "Excludes network transfer to the browser and playback" in lt.SPEC_BY_KEY[("voice", "voice_to_voice_ms")]["description"]


def test_no_data_is_not_instrumented_not_zero():
    out = lt.summarize([])
    for channel in ("voice", "text"):
        assert out["channels"][channel]["messages"] == {"assistant": 0, "user": 0}
        for m in out["channels"][channel]["metrics"]:
            assert m["state"] == "not_instrumented" and m["n"] == 0 and m["stats"] is None


def test_old_rows_without_new_keys_still_count_for_the_keys_they_have():
    out = lt.summarize([voice(llm_ttfb_ms=400, rag_ms=30, voice_to_voice_ms=1500, interrupted=False)])
    assert metric(out, "voice", "llm_ttfb_ms")["n"] == 1
    assert metric(out, "voice", "llm_ttfat_ms")["state"] == "not_instrumented"
    assert metric(out, "voice", "tts_ttfa_ms")["state"] == "not_instrumented"


def test_percentiles_are_withheld_until_there_are_enough_observations():
    four = lt.summarize([voice(llm_ttfb_ms=v) for v in (100, 200, 300, 400)])
    s = metric(four, "voice", "llm_ttfb_ms")["stats"]
    assert s["n"] == 4 and s["p50"] is None and s["p90"] is None and (s["min"], s["max"]) == (100, 400)
    five = lt.summarize([voice(llm_ttfb_ms=v) for v in (100, 200, 300, 400, 500)])
    s = metric(five, "voice", "llm_ttfb_ms")["stats"]
    assert s["p50"] == 300 and s["p90"] is None and s["p95"] is None
    many = lt.summarize([voice(llm_ttfb_ms=v) for v in range(1, 101)])
    s = metric(many, "voice", "llm_ttfb_ms")["stats"]
    assert s["p90"] == 90 and s["p95"] == 95 and s["p99"] == 99


def test_invalid_values_are_rejected_and_counted_never_averaged_in():
    rows = [voice(llm_ttfb_ms=v) for v in (100, -5, True, "300", None, float("inf"), 200)]
    m = metric(lt.summarize(rows), "voice", "llm_ttfb_ms")
    assert m["n"] == 2 and m["stats"]["max"] == 200
    assert m["rejected_values"] == 5  # -5, True (bool), "300", a stored null and inf


def test_visitor_message_metrics_use_their_own_unit_and_population():
    rows = [("voice", "user", {"stt_ttfb_ms": 300, "stt_processing_ms": 250}), ("voice", "user", {"stt_ttfb_ms": 500}),
            voice(llm_ttfb_ms=400)]
    out = lt.summarize(rows)
    stt = metric(out, "voice", "stt_ttfb_ms")
    assert stt["n"] == 2 and stt["n_unit"] == "visitor turns" and stt["role"] == "user"
    assert metric(out, "voice", "llm_ttfb_ms")["n_unit"] == "assistant turns"
    assert out["channels"]["voice"]["messages"] == {"assistant": 1, "user": 2}


def test_voice_and_text_are_separate_and_the_voice_rag_flag_is_not_a_timing():
    rows = [voice(rag_ms=40), ("text", "assistant", {"llm": 900, "rag": 25, "rag_sources": []})]
    out = lt.summarize(rows)
    assert metric(out, "text", "rag")["n"] == 1 and metric(out, "text", "llm")["n"] == 1
    assert metric(out, "voice", "rag_ms")["n"] == 1
    assert all(m["key"] != "rag" for m in out["channels"]["voice"]["metrics"])  # "rag": "on" is config, not a duration
    assert metric(out, "voice", "voice_to_voice_ms")["state"] == "not_instrumented"
    assert not any(m["key"] == "voice_to_voice_ms" for m in out["channels"]["text"]["metrics"])


def test_a_failed_retrieval_is_counted_as_an_error_and_its_zero_duration_is_not_a_measurement():
    rows = [("text", "assistant", {"llm": 800, "rag": 0, "rag_error": True}),
            ("text", "assistant", {"llm": 700, "rag": 30}),
            voice(rag_ms=0, rag_search_ms=0, rag_error=True, llm_ttfb_ms=300)]
    out = lt.summarize(rows)
    assert metric(out, "text", "rag")["n"] == 1 and metric(out, "text", "llm")["n"] == 2
    assert out["channels"]["text"]["outcomes"]["retrieval_errors"] == 1
    assert metric(out, "voice", "rag_ms")["n"] == 0 and out["channels"]["voice"]["outcomes"]["retrieval_errors"] == 1
    assert metric(out, "voice", "llm_ttfb_ms")["n"] == 1  # the rest of the turn still counts


def test_outcomes_interrupted_missing_and_trace_ids_are_counted_per_channel():
    rows = [voice(llm_ttfb_ms=400, interrupted=True, trace_run_id="run-1"),
            voice(interrupted=False),                                   # no timing at all
            ("text", "assistant", {}), ("text", "assistant", None), ("voice", "system", {"x": 1})]
    out = lt.summarize(rows)
    v, t = out["channels"]["voice"]["outcomes"], out["channels"]["text"]["outcomes"]
    assert v["assistant_turns"] == 2 and v["interrupted"] == 1 and v["with_langsmith_run_id"] == 1
    assert v["turns_without_any_timing"] == 1
    assert t["assistant_turns"] == 2 and t["turns_without_any_timing"] == 2
    assert t["interrupted"] is None and t["with_langsmith_run_id"] is None  # not applicable to chat: not zero


def test_breakdown_only_aggregates_turns_measured_from_user_silence():
    parts = [{"key": "silence", "ms": 600}, {"key": "llm", "ms": 900}]
    rows = [voice(latency_breakdown=parts, breakdown_from="user_silence"),
            voice(latency_breakdown=[{"key": "llm", "ms": 4000}], breakdown_from="client_connected"),  # greeting
            voice(latency_breakdown="garbage", breakdown_from="user_silence"), voice(llm_ttfb_ms=1)]
    b = lt.summarize(rows)["channels"]["voice"]["breakdown"]
    assert b["n_turns"] == 1 and b["excluded_other_anchor"] == 1
    assert {p["key"]: p["stats"]["max"] for p in b["parts"]} == {"silence": 600, "llm": 900}


def test_mixed_configurations_are_flagged_as_pooled():
    rows = [voice(llm_ttfb_ms=1), ("voice", "assistant", {"reasoning_effort": "high", "tts_provider": "deepgram", "llm_ttfb_ms": 2})]
    ch = lt.summarize(rows)["channels"]["voice"]
    assert ch["pooled_across_configurations"] is True and len(ch["configurations_observed"]) == 2
    assert lt.summarize([voice(llm_ttfb_ms=1)])["channels"]["voice"]["pooled_across_configurations"] is False


def test_what_cannot_be_measured_is_stated_and_the_output_is_json_safe():
    out = lt.summarize([voice(llm_ttfb_ms=1)])
    text = " ".join(out["not_recorded"])
    assert "503" in text and "playback" in text.lower() and "no client-side duration" in text
    assert out["kind"] == "latency_telemetry" and out["percentile_policy"]["p50_min_n"] == 5
    json.dumps(out)


def test_dashboard_labels_the_telemetry_as_its_own_dataset_and_never_uses_innerhtml():
    html = (ROOT / "api" / "static" / "eval_dashboard.html").read_text(encoding="utf-8")
    for needle in ("/v1/evals/live/latency", "Not instrumented", "Exactly what each metric measures", "Not recorded (cannot be shown)",
                   "not an evaluation run", "never as 0"):
        assert needle in html, needle
    assert "innerHTML" not in html
