"""Gemini model-id validation, judge error counting, and dry-run case accounting. All offline."""

import pytest

from evals import aggregate, run as cli
from evals.gemini import GeminiJudgeLLM, SETUP_HINT, build_gemini_judge_llm, model_id_problem
from evals.schema import load_dataset

GOLDEN = "evals/datasets/nimbuscrm_golden_v1.jsonl"


# --- model configuration ----------------------------------------------------------------

@pytest.mark.parametrize("bad", [None, "", "   ", "<model-id-from-AI-Studio>", "<gemini-x>", "your-model-id",
                                 "model-id", "gemini flash", "gemini/flash", '"gemini-x"', "x", " gemini-x",
                                 "changeme", "gemini-example-1"])
def test_missing_or_placeholder_model_ids_are_rejected(bad):
    assert model_id_problem(bad)


@pytest.mark.parametrize("good", ["gemini-abc-1.5", "model_v2", "Some-Model.3-lite"])
def test_plausible_ids_are_accepted(good):
    assert model_id_problem(good) is None  # shape only: existence is checked by the API, not here


def test_constructor_rejects_placeholder_before_any_request_and_explains_setup():
    with pytest.raises(ValueError) as error:
        GeminiJudgeLLM(api_key="k", model="<model-id-from-AI-Studio>")
    message = str(error.value)
    assert "GEMINI_JUDGE_MODEL" in message and SETUP_HINT in message
    assert "api_key=" not in message


def test_valid_model_id_is_passed_through_unchanged():
    llm = GeminiJudgeLLM(api_key="secret-key-value", model="gemini-abc-1.5")
    assert llm.model == "gemini-abc-1.5"
    assert "/models/gemini-abc-1.5:generateContent" in llm._url
    assert "secret-key-value" not in llm._url


def test_build_from_settings_uses_override_unchanged_and_never_substitutes_a_default():
    class Secret:
        def get_secret_value(self):
            return "k"

    class Settings:
        gemini_api_key = Secret()
        gemini_judge_model = None
        llm_timeout_seconds = 5

    with pytest.raises(ValueError):
        build_gemini_judge_llm(Settings())  # nothing configured -> no guessed default
    assert build_gemini_judge_llm(Settings(), "gemini-abc-1.5").model == "gemini-abc-1.5"
    Settings.gemini_judge_model = "gemini-env-2"
    assert build_gemini_judge_llm(Settings()).model == "gemini-env-2"


def test_cli_refuses_a_placeholder_model_with_no_call(monkeypatch, capsys, tmp_path):
    import json

    replay = tmp_path / "cases.jsonl"
    replay.write_text(json.dumps({"case_id": "fact-pro-price", "status": "ok",
                                  "output": {"reply": "It is $29.", "retrieved_sources": [], "context_chunks": 0}}) + "\n")

    def forbidden(*a, **k):
        raise AssertionError("a provider client must not be built")

    monkeypatch.setattr(cli, "_provider_llm", forbidden)
    monkeypatch.setenv("GEMINI_API_KEY", "secret-key-value")
    monkeypatch.setenv("GROQ_API_KEY", "unused-test-key")
    monkeypatch.setenv("GROQ_MODEL", "unused-test-model")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:1/none")
    monkeypatch.setenv("GEMINI_JUDGE_MODEL", "<model-id-from-AI-Studio>")
    from api.config import get_settings

    get_settings.cache_clear()
    try:
        code = cli.main(["--mode", "replay", "--replay", str(replay), "--results-dir", str(tmp_path / "r"),
                         "--allow-llm", "--judge-suite", "--judge-provider", "gemini"])
    finally:
        get_settings.cache_clear()
    assert code == 2
    err = capsys.readouterr().err
    assert "placeholder" in err or "not a valid model id" in err
    assert "GEMINI_JUDGE_MODEL" in err and "No call was made" in err
    assert "secret-key-value" not in err
    assert not (tmp_path / "r").exists()


# --- judge error counting ---------------------------------------------------------------

def _ev(name, status):
    return {"evaluator": name, "status": status, "verdict": None, "score": None}


def test_suite_errors_are_counted_and_skips_are_not():
    results = [
        {"status": "ok", "passed": True, "evaluators": [_ev("relevance", "error"), _ev("tone_language", "error")]},
        {"status": "ok", "passed": True, "evaluators": [_ev("faithfulness", "skipped_missing_input")]},
        {"status": "ok", "passed": True, "evaluators": [_ev("relevance", "ok")]},
        {"status": "ok", "passed": True, "judge": {"ok": False, "error": "x"}},
        {"status": "ok", "passed": True, "judge": {"ok": True, "scores": {"faithfulness": 1.0}}},
        {"status": "skipped", "evaluators": [_ev("relevance", "error")]},
    ]
    counts = aggregate.build_counts(results)
    assert counts["judge_errors"] == 2          # case 1 (suite) and case 4 (legacy)
    assert counts["evaluator_errors"] == 2      # two failed evaluator calls on case 1
    assert counts["skipped"] == 1 and counts["passed"] == 5


def test_a_case_with_legacy_and_suite_errors_is_counted_once():
    both = {"status": "ok", "passed": True, "judge": {"ok": False}, "evaluators": [_ev("relevance", "error")]}
    assert aggregate.build_counts([both])["judge_errors"] == 1


def test_errors_do_not_enter_scores():
    results = [{"status": "ok", "passed": True, "scores": {},
                "evaluators": [{"evaluator": "relevance", "version": "1", "model": "m", "status": "error",
                                "verdict": None, "score": None, "error": "provider: LLMError: gemini_http_400"},
                               {"evaluator": "pii_leak", "version": "1", "model": "deterministic", "status": "ok",
                                "verdict": "pass", "score": 1.0}]}]
    summary = aggregate.build_evaluator_summary(results)
    assert summary["relevance"]["n_error"] == 1 and summary["relevance"]["n_scored"] == 0
    assert summary["pii_leak"]["n_scored"] == 1


# --- dry-run accounting -----------------------------------------------------------------

def test_retrieval_dry_run_splits_model_gate_and_skipped_cases():
    ds = load_dataset(GOLDEN)
    plan = cli.estimate_llm_calls(ds, "retrieval", judge=False, limit=None)
    assert plan["model_cases"] + plan["free_gate_cases"] + plan["skipped"] == len(ds.cases)
    assert plan["free_gate_cases"] == sum(1 for c in ds.cases if c.kind == "lead_gate")
    assert plan["cases_to_run"] == plan["model_cases"] + plan["free_gate_cases"]
    assert plan["skipped"] == sum(plan["skipped_reasons"].values()) > 0
    assert plan["total"] == 0


def test_dry_run_with_ks_prints_breakdown_and_writes_nothing(tmp_path, capsys):
    out_dir = tmp_path / "results"
    code = cli.main(["--mode", "retrieval", "--ks", "1,3,5", "--dry-run", "--results-dir", str(out_dir)])
    assert code == 0
    out = capsys.readouterr().out
    assert "free consent-gate case(s)" in out and "Skipped:" in out and "Dry run: nothing executed." in out
    assert not out_dir.exists() or not any(out_dir.iterdir())


def test_bad_ks_is_a_usage_error(capsys):
    assert cli.main(["--mode", "retrieval", "--ks", "0,x", "--dry-run"]) == cli.EXIT_USAGE
    assert "--ks" in capsys.readouterr().err


def test_deterministic_and_llm_judge_scores_are_reported_separately():
    def ev(model, status, score=None):
        return {"evaluator": "x", "model": model, "status": status, "score": score, "verdict": None}

    results = [{"status": "ok", "passed": True, "evaluators": [
        ev("deterministic", "ok", 1.0), ev("deterministic", "ok", 0.0),
        ev("gemini/m", "ok", 0.8), ev("gemini/m", "error"), ev("gemini/m", "skipped_missing_input"),
        ev("gemini/m", "ok", None),  # insufficient evidence: not a score
    ]}]
    counts = aggregate.build_counts(results)
    assert counts["evaluator_scores_deterministic"] == 2
    assert counts["evaluator_scores_llm_judge"] == 1
    assert counts["evaluator_errors"] == 1 and counts["evaluator_skipped"] == 1


def test_no_audio_or_live_api_fields_in_gemini_judge_request():
    import inspect

    from evals import gemini

    source = inspect.getsource(gemini.GeminiJudgeLLM.generate)
    assert "responseMimeType" in source and "application/json" in source
    for forbidden in ("responseModalities", "speechConfig", "AUDIO", "bidi", "live"):
        assert forbidden not in source


# --- judge timeout and retries --------------------------------------------------------------

import asyncio

import httpx

from api.config import Settings
from api.llm import LLMError
from evals import gemini as gm
from evals.judges import run_judge


def _client(handler, **kw):
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    llm = GeminiJudgeLLM("k", "gemini-abc-1.5", transport=httpx.MockTransport(handler), sleep=sleep, **kw)
    return llm, sleeps


def _ok_response():
    return httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"text": '{"verdict":"pass","score":1.0,"reason":"r","evidence":[]}'}]}}],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2, "totalTokenCount": 3}})


def test_default_judge_timeout_is_60_and_does_not_inherit_the_chat_timeout():
    class Secret:
        def get_secret_value(self):
            return "k"

    class S:
        gemini_api_key = Secret()
        gemini_judge_model = "gemini-abc-1.5"
        gemini_judge_timeout_seconds = None
        llm_timeout_seconds = 20.0

    assert gm.build_gemini_judge_llm(S()).timeout_seconds == gm.DEFAULT_TIMEOUT_SECONDS == 60.0
    S.gemini_judge_timeout_seconds = 90.0
    assert gm.build_gemini_judge_llm(S()).timeout_seconds == 90.0
    assert S.llm_timeout_seconds == 20.0


def _settings(**env):
    base = {"GROQ_API_KEY": "x", "GROQ_MODEL": "m", "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:1/n",
            "REDIS_ENABLED": "false"}
    return Settings(_env_file=None, **{k.lower(): v for k, v in {**base, **env}.items()})


def test_settings_read_the_dedicated_timeout_and_leave_chat_timeout_alone():
    unset = _settings()
    assert unset.gemini_judge_timeout_seconds is None and unset.llm_timeout_seconds == 20.0
    custom = _settings(GEMINI_JUDGE_TIMEOUT_SECONDS="45", LLM_TIMEOUT_SECONDS="7")
    assert custom.gemini_judge_timeout_seconds == 45.0 and custom.llm_timeout_seconds == 7.0


@pytest.mark.parametrize("bad", ["0", "-5", "601", "abc"])
def test_invalid_judge_timeout_is_rejected(bad):
    with pytest.raises(Exception):
        _settings(GEMINI_JUDGE_TIMEOUT_SECONDS=bad)


def test_read_timeout_is_not_retried_and_reports_duration_and_attempts():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=request)

    llm, sleeps = _client(handler, timeout_seconds=45)
    with pytest.raises(LLMError) as error:
        asyncio.run(llm.generate("sys", [{"role": "user", "content": "SECRET QUESTION TEXT"}]))
    assert len(calls) == 1 and sleeps == []
    assert "45s" in str(error.value) and "no_retry" in str(error.value)
    assert "SECRET" not in str(error.value) and "k" != str(error.value)


def test_connection_errors_are_retried_then_reported_as_exhausted():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("nope", request=request)

    llm, sleeps = _client(handler, max_retries=2)
    with pytest.raises(LLMError) as error:
        asyncio.run(llm.generate("s", [{"role": "user", "content": "q"}]))
    assert len(calls) == 3 and sleeps == [1, 2]
    assert "ConnectError_after_3_attempts" in str(error.value)


def test_retryable_status_then_success_makes_two_requests():
    statuses = iter([503, 200])
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503) if next(statuses) == 503 else _ok_response()

    llm, _ = _client(handler)
    reply = asyncio.run(llm.generate("s", [{"role": "user", "content": "q"}]))
    assert len(calls) == 2 and reply.usage["total_tokens"] == 3


def test_status_error_after_retries_names_the_attempts_and_non_retryable_status_does_not_retry():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429)

    llm, _ = _client(handler, max_retries=1)
    with pytest.raises(LLMError, match="gemini_http_429_after_2_attempts"):
        asyncio.run(llm.generate("s", [{"role": "user", "content": "q"}]))
    assert len(calls) == 2
    calls.clear()
    llm403, _ = _client(lambda r: (calls.append(1), httpx.Response(403))[1])
    with pytest.raises(LLMError, match="^gemini_http_403$"):
        asyncio.run(llm403.generate("s", [{"role": "user", "content": "q"}]))
    assert len(calls) == 1


def test_successful_judge_response_is_a_scored_outcome():
    llm, _ = _client(lambda r: _ok_response())
    out = asyncio.run(run_judge(llm, "relevance", {"question": "q", "answer": "a"}, model_label="gemini/gemini-abc-1.5"))
    assert out.status == "ok" and out.score == 1.0


def test_timeout_becomes_an_evaluator_error_with_a_log_line_and_no_content(caplog):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    llm, _ = _client(handler, timeout_seconds=60)
    with caplog.at_level("WARNING", logger="evals.judges"):
        out = asyncio.run(run_judge(llm, "tone_language", {"question": "my secret q", "answer": "my secret a"},
                                    model_label="gemini/gemini-abc-1.5"))
    assert out.status == "error" and out.score is None
    assert "gemini_timeout_60s" in out.error
    text = caplog.text
    assert "tone_language" in text and "gemini/gemini-abc-1.5" in text and "gemini_timeout_60s" in text
    assert "secret" not in text


# --- judge call latency ---------------------------------------------------------------------

from api.llm import LLMReply
from evals.report import render_summary


class _Provider:
    def __init__(self, reply=None, error=None):
        self._reply, self._error = reply, error

    async def generate(self, system_prompt, history, tools=None, tool_choice=None):
        if self._error:
            raise self._error
        return self._reply


_GOOD = '{"verdict":"pass","score":1.0,"reason":"r","evidence":[]}'


def test_measured_latency_is_propagated_unchanged():
    out = asyncio.run(run_judge(_Provider(LLMReply(text=_GOOD, latency_ms=1234)), "relevance",
                                {"question": "q", "answer": "a"}, model_label="g/m"))
    assert out.status == "ok" and out.latency_ms == 1234 and out.to_dict()["latency_ms"] == 1234


def test_zero_is_a_real_measurement_and_is_kept():
    out = asyncio.run(run_judge(_Provider(LLMReply(text=_GOOD, latency_ms=0)), "relevance",
                                {"question": "q", "answer": "a"}))
    assert out.latency_ms == 0


@pytest.mark.parametrize("bad", [None, True, -5, "12", 1.5])
def test_unusable_latency_is_na_not_a_default(bad):
    reply = LLMReply(text=_GOOD, latency_ms=0)
    reply.latency_ms = bad
    out = asyncio.run(run_judge(_Provider(reply), "relevance", {"question": "q", "answer": "a"}))
    assert out.status == "ok" and out.latency_ms is None


def test_timeout_has_no_score_no_latency_and_is_counted_as_an_error():
    out = asyncio.run(run_judge(_Provider(error=LLMError("gemini_timeout_60s_1_attempt_no_retry")), "tone_language",
                                {"question": "q", "answer": "a"}, model_label="g/m"))
    assert out.status == "error" and out.score is None and out.latency_ms is None
    results = [{"status": "ok", "passed": True, "evaluators": [out.to_dict()]}]
    counts = aggregate.build_counts(results)
    assert counts["judge_errors"] == 1 and counts["evaluator_scores_llm_judge"] == 0
    assert aggregate.build_evaluator_summary(results)["tone_language"]["n_scored"] == 0


def test_invalid_judge_output_still_records_the_measured_latency():
    out = asyncio.run(run_judge(_Provider(LLMReply(text="not json", latency_ms=777)), "relevance",
                                {"question": "q", "answer": "a"}))
    assert out.status == "error" and out.latency_ms == 777 and out.score is None


def test_skipped_and_deterministic_outcomes_have_no_latency():
    skipped = asyncio.run(run_judge(_Provider(), "faithfulness", {"question": "q", "answer": "a"}))
    assert skipped.status == "skipped_missing_input" and skipped.latency_ms is None
    from evals import judge_suite
    from evals.schema import parse_case

    case = parse_case({"id": "c", "kind": "rag_answer", "category": "factual",
                       "messages": [{"role": "user", "content": "q"}], "expected_sources": ["B"]})
    for item in judge_suite.deterministic_outcomes(case, {"reply": "ok"}):
        assert item["latency_ms"] is None


def _run_meta():
    return {"run_id": "20260101T000000Z-aaaaaaaa", "status": "completed", "mode": "replay", "dataset": {"name": "d", "reviewed_cases": 0},
            "counts": {"ran": 1, "total": 1}, "metrics": {}, "latency": {}, "by_category": {}}


def _case(evaluators):
    return {"case_id": "c1", "category": "factual", "question": "q", "status": "ok", "passed": True,
            "evaluators": evaluators}


def test_summary_lists_each_judge_call_with_latency_or_na():
    ev = [
        {"evaluator": "relevance", "model": "g/m", "status": "ok", "score": 1.0, "verdict": "pass", "latency_ms": 1234},
        {"evaluator": "tone_language", "model": "g/m", "status": "error", "score": None,
         "error": "provider: LLMError: gemini_timeout_60s_1_attempt_no_retry", "latency_ms": None},
        {"evaluator": "pii_leak", "model": "deterministic", "status": "ok", "score": 1.0, "verdict": "pass", "latency_ms": None},
        {"evaluator": "faithfulness", "model": "g/m", "status": "skipped_missing_input", "score": None},
    ]
    text = render_summary(_run_meta(), [_case(ev)], None)
    assert "## LLM judge calls (2)" in text
    assert "c1 / relevance (g/m): score=1.0, latency_ms=1234" in text
    assert "c1 / tone_language (g/m): error=provider: LLMError: gemini_timeout_60s_1_attempt_no_retry, latency_ms=N/A" in text
    assert "pii_leak" not in text.split("## LLM judge calls")[1].split("##")[0]
    assert "faithfulness" not in text.split("## LLM judge calls")[1].split("##")[0]


def test_old_saved_results_without_the_field_do_not_break_the_summary():
    old = [{"evaluator": "relevance", "model": "g/m", "status": "ok", "score": 0.9, "verdict": "pass"}]
    text = render_summary(_run_meta(), [_case(old)], None)
    assert "latency_ms=N/A" in text
    assert "## LLM judge calls" not in render_summary(_run_meta(), [_case([])], None)
