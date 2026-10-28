import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from api.llm import LLMError
from evals import judges as j
from evals.gemini import GeminiJudgeLLM


class Provider:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []

    async def generate(self, system, history, tools=None, tool_choice=None):
        self.calls.append((system, history))
        if self.error:
            raise self.error
        return SimpleNamespace(text=self.reply if isinstance(self.reply, str) else json.dumps(self.reply))


def run(coro):
    return asyncio.run(coro)


GOOD = {"verdict": "pass", "score": 0.9, "reason": "Matches.", "evidence": ["$29 per user"]}
INPUTS = {"question": "Price?", "answer": "It is $29 per user.", "reference": "Pro costs $29 per user per month."}


def test_missing_reference_is_never_sent_to_the_model_or_scored():
    provider = Provider(GOOD)
    out = run(j.run_judge(provider, "correctness", {"question": "Price?", "answer": "$29"}))
    assert out.status == "skipped_missing_input" and out.missing == ["reference"]
    assert out.score is None and provider.calls == []
    assert j.passed(out) is None


def test_valid_result_is_recorded_with_version_and_model():
    out = run(j.run_judge(Provider(GOOD), "correctness", INPUTS, model_label="gemini/test-model"))
    assert (out.status, out.verdict, out.score) == ("ok", "pass", 0.9)
    assert (out.evaluator, out.version, out.model) == ("correctness", "1", "gemini/test-model")
    assert out.evidence_unverified == 0 and j.passed(out) is True


def test_unverifiable_evidence_is_counted_not_trusted():
    bad = {**GOOD, "evidence": ["$29 per user", "an invented quote"]}
    out = run(j.run_judge(Provider(bad), "correctness", INPUTS))
    assert out.evidence_unverified == 1


def test_model_can_say_insufficient_evidence_and_it_is_not_a_pass():
    reply = {"verdict": "insufficient_evidence", "score": None, "reason": "Context is empty.", "evidence": []}
    out = run(j.run_judge(Provider(reply), "faithfulness", {"answer": "x", "context": ["some context"]}))
    assert out.verdict == "insufficient_evidence" and out.score is None and j.passed(out) is None


@pytest.mark.parametrize("reply", [
    "not json at all",
    {"verdict": "pass", "score": 1.5, "reason": "x", "evidence": []},          # out of range
    {"verdict": "pass", "reason": "x", "evidence": []},                         # score missing
    {"verdict": "insufficient_evidence", "score": 0.5, "reason": "x"},          # score with insufficient
    {"verdict": "maybe", "score": 0.5, "reason": "x"},                          # unknown verdict
    {**GOOD, "extra_field": 1},                                                 # extra field forbidden
])
def test_invalid_judge_output_is_an_error_not_a_score(reply):
    out = run(j.run_judge(Provider(reply), "correctness", INPUTS))
    assert out.status == "error" and out.score is None and out.error.startswith("invalid judge output")


def test_provider_failure_is_visible_and_scrubbed_of_detail():
    out = run(j.run_judge(Provider(error=LLMError("gemini_http_429")), "relevance", {"question": "q", "answer": "a"}))
    assert out.status == "error" and "gemini_http_429" in out.error and out.score is None


def test_sensitive_values_never_reach_the_judge_prompt():
    provider = Provider(GOOD)
    inputs = {**INPUTS, "answer": "Mail jane.doe@example.com or call +353 87 123 4567; key sk-abcdefghijklmnop1234"}
    run(j.run_judge(provider, "correctness", inputs))
    sent = provider.calls[0][1][0]["content"]
    assert "jane.doe" not in sent and "123 4567" not in sent and "abcdefghijklmnop" not in sent
    assert "[EMAIL]" in sent and "[SECRET]" in sent


def test_deterministic_checks():
    assert j.pii_leak("Contact me at a@b.com")["kinds"] == ["EMAIL"]
    assert j.pii_leak("Your email a@b.com is saved", allowed=("a@b.com",))["leaked"] is False
    assert j.tool_selection(["save_lead"], ["save_lead"])["correct"]
    assert j.tool_selection([], ["save_lead"])["unexpected"] == ["save_lead"]
    assert j.conciseness_check("word " * 61)["within_limit"] is False


def test_every_spec_declares_inputs_and_a_version():
    assert all(s.needs and s.version and s.rubric for s in j.SPECS.values())
    assert {"correctness", "faithfulness", "relevance", "hallucination", "task_completion",
            "tone_language", "tool_result_correctness", "safety"} == set(j.SPECS)


# ---- Gemini REST client (mock transport; no network, no key) ---------------


def gemini(handler, **kw):
    return GeminiJudgeLLM("test-key-not-real", "test-model", transport=httpx.MockTransport(handler),
                          sleep=lambda s: asyncio.sleep(0), **kw)


def ok_body(text='{"verdict":"pass","score":1,"reason":"ok","evidence":[]}'):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15}}


def test_gemini_request_shape_key_in_header_only_and_usage_returned():
    seen = {}

    def handler(request):
        seen["url"], seen["key"], seen["body"] = str(request.url), request.headers.get("x-goog-api-key"), json.loads(request.content)
        return httpx.Response(200, json=ok_body())

    reply = run(gemini(handler).generate("SYSTEM", [{"role": "user", "content": "hi"}]))
    assert seen["key"] == "test-key-not-real" and "test-key-not-real" not in seen["url"]
    assert seen["url"].endswith("/models/test-model:generateContent")
    assert seen["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert seen["body"]["systemInstruction"]["parts"][0]["text"] == "SYSTEM"
    assert reply.usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


def test_gemini_retries_rate_limits_then_succeeds_and_gives_up_on_auth_errors():
    attempts = []

    def flaky(request):
        attempts.append(1)
        return httpx.Response(429 if len(attempts) < 3 else 200, json=ok_body())

    assert run(gemini(flaky).generate("s", [{"role": "user", "content": "x"}])).text
    assert len(attempts) == 3

    with pytest.raises(LLMError, match="gemini_http_403") as info:
        run(gemini(lambda r: httpx.Response(403, json={"error": "secret detail"})).generate("s", []))
    assert "secret detail" not in str(info.value)

    always = []

    def down(request):
        always.append(1)
        return httpx.Response(503)

    with pytest.raises(LLMError, match="gemini_http_503"):
        run(gemini(down).generate("s", []))
    assert len(always) == 3  # first try + 2 retries


def test_gemini_blocked_and_empty_responses_are_errors():
    with pytest.raises(LLMError, match="blocked"):
        run(gemini(lambda r: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})).generate("s", []))
    with pytest.raises(LLMError, match="empty"):
        run(gemini(lambda r: httpx.Response(200, json={"candidates": []})).generate("s", []))


def test_gemini_requires_key_and_model_without_echoing_anything():
    with pytest.raises(ValueError, match="API key"):
        GeminiJudgeLLM("", "m")
    with pytest.raises(ValueError, match="GEMINI_JUDGE_MODEL"):
        GeminiJudgeLLM("k", "")


def test_gemini_judge_plugs_into_the_existing_judge_class():
    from evals.judge import Judge

    body = {"faithfulness": {"score": 5, "unsupported_claims": []}, "relevance": {"score": 4}, "correctness": {"score": 5}, "explanation": "ok"}
    llm = gemini(lambda r: httpx.Response(200, json=ok_body(json.dumps(body))))
    verdict = run(Judge(llm, "gemini/test-model").judge("q", ["ctx"], ["29"], False, "It is $29."))
    assert verdict.ok and verdict.scores["faithfulness"] == 1.0
