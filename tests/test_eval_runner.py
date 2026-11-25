import asyncio
import json

import pytest

from evals import aggregate, compare, runner, store
from evals.judge import Judge, parse_verdict
from evals.report import render_summary
from evals.schema import Dataset, parse_case
from evals.targets import CountingLLM, ReplayTarget


def case(**overrides):
    base = {
        "id": "c1", "kind": "rag_answer", "category": "factual",
        "messages": [{"role": "user", "content": "How much is Pro?"}],
        "expected_sources": ["P"], "required_facts": ["29"],
    }
    base.update(overrides)
    return parse_case(base)


def dataset(*cases):
    return Dataset(name="t", path="t.jsonl", sha256="abc", cases=list(cases))


class FakeTarget:
    def __init__(self, outputs=None, raises=None):
        self.outputs, self.raises, self.seen = outputs or {}, raises or {}, []

    async def run_case(self, c):
        self.seen.append(c.id)
        if c.id in self.raises:
            raise self.raises[c.id]
        return dict(self.outputs[c.id])


def run(ds, target, **kw):
    return asyncio.run(runner.run_suite(ds, target, mode=kw.pop("mode", "full"), **kw))


GOOD = {"reply": "The Pro plan is $29 per user per month.", "retrieved_sources": ["P"],
        "retrieval_ms": 10, "llm_ms": 200, "wall_ms": 300}


def test_passing_and_failing_cases_are_scored_and_explained():
    ds = dataset(case(id="ok"), case(id="bad"))
    target = FakeTarget({"ok": GOOD, "bad": {**GOOD, "reply": "It is $39.", "retrieved_sources": ["X"]}})
    results, status = run(ds, target)
    assert status == "completed"
    by_id = {r["case_id"]: r for r in results}
    assert by_id["ok"]["passed"] is True and by_id["ok"]["failure_reasons"] == []
    assert by_id["bad"]["passed"] is False
    reasons = " ".join(by_id["bad"]["failure_reasons"])
    assert "retrieval_hit" in reasons and "required_facts" in reasons


def test_target_exception_becomes_an_error_with_only_the_type_name():
    ds = dataset(case(id="boom"))
    target = FakeTarget(raises={"boom": RuntimeError("postgresql://user:secret@host/db failed")})
    results, status = run(ds, target)
    assert results[0]["status"] == "error" and results[0]["error"] == "RuntimeError"
    assert "secret" not in json.dumps(results[0])
    assert status == "failed"  # every case that ran errored


def test_consent_gate_cases_run_without_a_target_and_in_every_mode():
    gate = parse_case({
        "id": "g", "kind": "lead_gate", "category": "lead_gate",
        "messages": [{"role": "user", "content": "I'm Rahul, rahul@example.com"}],
        "proposed_tool_call": {"name": "save_lead", "arguments": {"name": "Rahul", "email": "rahul@example.com"}},
        "expected_gate": "rejected",
    })
    results, status = run(dataset(gate), None, mode="retrieval")
    assert results[0]["passed"] is True and status == "completed"


def test_skip_rules_per_mode():
    follow = case(id="f", category="follow_up",
                  messages=[{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}])
    lead = parse_case({"id": "l", "kind": "lead_llm", "category": "lead_llm",
                       "messages": [{"role": "user", "content": "yes"}], "expected_lead_saved": True,
                       "expected_lead_email": "lead@example.com"})
    assert "LLM" in runner.skip_reason(follow, "retrieval")
    assert "LLM" in runner.skip_reason(lead, "retrieval")
    assert runner.skip_reason(follow, "full") is None
    assert runner.skip_reason(case(), "replay", replay_ids=set()) is not None
    assert runner.skip_reason(case(), "replay", replay_ids={"c1"}) is None


def test_retrieval_mode_scores_retrieval_only_and_skips_llm_cases():
    ds = dataset(case(id="r"))
    target = FakeTarget({"r": {"retrieved_sources": ["P"], "retrieval_ms": 5}})
    results, _ = run(ds, target, mode="retrieval")
    assert results[0]["passed"] is True
    assert "recall_at_4" in results[0]["scores"] and "required_facts" not in results[0]["scores"]


def test_run_stops_as_partial_when_the_call_budget_is_exhausted():
    class Budgeted(FakeTarget):
        budget_exhausted = False

        async def run_case(self, c):
            out = await super().run_case(c)
            if len(self.seen) == 1:
                self.budget_exhausted = True
            return out

    ds = dataset(case(id="a"), case(id="b"))
    results, status = run(ds, Budgeted({"a": GOOD, "b": GOOD}))
    assert status == "partial" and [r["case_id"] for r in results] == ["a"]


def test_counting_llm_enforces_a_hard_cap():
    from api.llm import LLMError, LLMReply

    class Inner:
        async def generate(self, *a, **k):
            return LLMReply(text="x", latency_ms=1)

    llm = CountingLLM(Inner(), max_calls=2)

    async def go():
        await llm.generate("s", [])
        await llm.generate("s", [])
        assert llm.budget_exhausted
        with pytest.raises(LLMError):
            await llm.generate("s", [])

    asyncio.run(go())
    assert llm.calls == 2


def test_lead_row_is_reduced_to_booleans_and_never_persisted():
    lead = parse_case({"id": "l", "kind": "lead_llm", "category": "lead_llm",
                       "messages": [{"role": "user", "content": "Yes"}],
                       "expected_lead_saved": True, "expected_lead_email": "rahul@example.com"})
    target = FakeTarget({"l": {"reply": "Passed to the sales team.", "lead_status": "saved",
                               "lead_row": {"email": "rahul@example.com"}}})
    results, _ = run(dataset(lead), target)
    assert results[0]["passed"] is True
    assert results[0]["output"]["lead_saved"] is True and results[0]["output"]["lead_email_ok"] is True
    assert "rahul@example.com" not in json.dumps(results[0]["output"])


# ---- judge ----

GOOD_VERDICT = json.dumps({"faithfulness": {"score": 5, "unsupported_claims": []},
                           "relevance": {"score": 4}, "correctness": {"score": 3}, "explanation": "ok"})


def test_judge_verdict_parsing_and_normalisation():
    verdict = parse_verdict("Here you go: " + GOOD_VERDICT)
    assert verdict.ok and verdict.scores == {"faithfulness": 1.0, "relevance": 0.75, "correctness": 0.5}


@pytest.mark.parametrize("text", ["", "no json", "{bad", '{"faithfulness": {"score": 9}, "relevance": {"score": 1}, "correctness": {"score": 1}}',
                                  '{"faithfulness": {"score": 3}}', "[1, 2]"])
def test_invalid_judge_output_is_an_error_never_a_score(text):
    verdict = parse_verdict(text)
    assert verdict.ok is False and verdict.scores == {} and verdict.error


def test_judge_provider_failure_does_not_abort_the_run():
    class Broken:
        async def generate(self, *a, **k):
            raise RuntimeError("boom")

    judge = Judge(Broken(), "x")
    ds = dataset(case(id="a"))
    results, status = run(ds, FakeTarget({"a": {**GOOD, "context": ["ctx"]}}), judge=judge)
    assert status == "completed"
    assert results[0]["judge"]["ok"] is False and results[0]["judge"]["error"] == "judge call failed: RuntimeError"
    metrics = aggregate.build_metrics(results)
    assert "judge.faithfulness" not in metrics
    assert aggregate.build_counts(results)["judge_errors"] == 1


def test_judge_scores_reach_the_aggregates():
    from api.llm import LLMReply

    class Good:
        async def generate(self, *a, **k):
            return LLMReply(text=GOOD_VERDICT, latency_ms=1)

    results, _ = run(dataset(case(id="a")), FakeTarget({"a": {**GOOD, "context": ["c"]}}), judge=Judge(Good(), "x"))
    metrics = aggregate.build_metrics(results)
    assert metrics["judge.faithfulness"]["value"] == 1.0 and metrics["judge.relevance"]["value"] == 0.75


def test_legacy_judge_never_scores_correctness_against_unreviewed_facts():
    from api.llm import LLMReply

    class Good:
        async def generate(self, *a, **k):
            return LLMReply(text=GOOD_VERDICT, latency_ms=1)

    out = {"a": {**GOOD, "context": ["c"]}}
    # author-written facts, nobody has reviewed them: correctness is dropped, the other scores stay
    results, _ = run(dataset(case(id="a")), FakeTarget(out), judge=Judge(Good(), "x"))
    assert "correctness" not in results[0]["judge"]["scores"] and "correctness" not in results[0]["judge"]["raw_scores"]
    assert "no human-reviewed reference" in results[0]["judge"]["correctness_skipped"]
    metrics = aggregate.build_metrics(results)
    assert "judge.correctness" not in metrics and metrics["judge.relevance"]["n"] == 1
    # a passed case review alone (sources/behaviour) does not review the keyword facts either
    results, _ = run(dataset(case(id="a", review_status="reviewed", reviewer="R")), FakeTarget(out), judge=Judge(Good(), "x"))
    assert "judge.correctness" not in aggregate.build_metrics(results)
    # only reviewed facts on a reviewed case make correctness scoreable
    reviewed = case(id="a", review_status="reviewed", reviewer="R", facts_review_status="reviewed", facts_reviewer="R2")
    results, _ = run(dataset(reviewed), FakeTarget(out), judge=Judge(Good(), "x"))
    assert aggregate.build_metrics(results)["judge.correctness"]["value"] == 0.5
    assert "correctness_skipped" not in results[0]["judge"]


# ---- aggregation, comparison, report, storage ----

def finished(results, **meta):
    base = {"run_id": "20260101T000000Z-aaaaaaaa", "status": "completed", "mode": "full",
            "started_at": "2026-01-01T00:00:00+00:00", "organization_id": "org-1",
            "dataset": {"name": "t", "sha256": "abc", "n_cases": len(results), "reviewed_cases": 0},
            "model": {"provider": "groq", "name": "m", "reasoning_effort": "low"}}
    base.update(meta)
    return runner.finalize_run(base, results)


def results_with(pass_count, fail_count):
    ds = dataset(*[case(id=f"c{i}") for i in range(pass_count + fail_count)])
    outputs = {f"c{i}": (GOOD if i < pass_count else {**GOOD, "reply": "nope"}) for i in range(pass_count + fail_count)}
    return run(ds, FakeTarget(outputs))[0]


def test_aggregates_report_n_and_never_turn_missing_data_into_zero():
    run_doc = finished(results_with(3, 1))
    assert run_doc["metrics"]["run.pass_rate"]["value"] == 0.75
    assert run_doc["metrics"]["retrieval.recall_at_4"]["n"] == 4
    assert "judge.faithfulness" not in run_doc["metrics"]
    assert run_doc["latency"]["wall_ms"]["n"] == 4 and run_doc["latency"]["wall_ms"]["p95"] is None
    assert run_doc["counts"]["failed"] == 1


def test_compare_flags_regressions_improvements_and_thin_data():
    base = finished(results_with(9, 1), run_id="20260101T000000Z-bbbbbbbb")
    worse = finished(results_with(5, 5))
    result = compare.compare_runs(worse, base)
    assert "run.pass_rate" in result["regressions"]
    assert result["tolerances"]["calibrated"] is False

    better = compare.compare_runs(base, worse)
    assert "run.pass_rate" in better["improvements"]

    thin = compare.compare_runs(finished(results_with(2, 0)), finished(results_with(1, 1)))
    assert all(r["status"] == "insufficient_data" for r in thin["metrics"] if r["name"] == "run.pass_rate")


def test_compare_warns_when_runs_are_not_like_for_like():
    a = finished(results_with(5, 0))
    b = finished(results_with(5, 0), mode="retrieval", dataset={"name": "t", "sha256": "different", "n_cases": 5, "reviewed_cases": 0})
    warnings = " ".join(compare.compare_runs(a, b)["warnings"])
    assert "dataset differs" in warnings and "mode differs" in warnings


def test_latency_regression_uses_relative_tolerance():
    def with_p50(value):
        return {"metrics": {}, "latency": {"wall_ms": {"n": 30, "p50": value, "p90": None, "p95": None, "p99": None, "max": value}}}

    result = compare.compare_runs(with_p50(500), with_p50(300))
    assert "latency.wall_ms.p50" in result["regressions"]
    assert compare.compare_runs(with_p50(310), with_p50(300))["regressions"] == []


def test_store_round_trip_filters_by_organization_and_rejects_bad_ids(tmp_path):
    results = results_with(2, 1)
    doc_a = finished(results, run_id="20260102T000000Z-aaaaaaaa", organization_id="org-A")
    doc_b = finished(results, run_id="20260103T000000Z-bbbbbbbb", organization_id="org-B")
    store.write_run(tmp_path, doc_a, results, "# a")
    store.write_run(tmp_path, doc_b, results, "# b")

    assert [r["run_id"] for r in store.list_runs(tmp_path, "org-A")] == ["20260102T000000Z-aaaaaaaa"]
    assert len(store.list_runs(tmp_path)) == 2
    assert store.load_run(tmp_path, "../../etc/passwd") is None
    assert store.load_run(tmp_path, "not-a-run-id") is None
    assert len(store.load_cases(tmp_path, "20260102T000000Z-aaaaaaaa")) == 3
    with pytest.raises(ValueError):
        store.write_run(tmp_path, {**doc_a, "run_id": "../x"}, results, "")


def test_unfinished_run_directory_without_run_json_is_ignored(tmp_path):
    (tmp_path / "20260101T000000Z-cccccccc").mkdir()
    assert store.list_runs(tmp_path) == []


def test_secrets_are_scrubbed_from_written_results(tmp_path):
    secret_groq = "gsk_" + "a" * 30
    secret_vsa = "vsa_" + "b" * 40
    results = results_with(1, 0)
    results[0]["output"]["reply"] = f"key {secret_groq} and {secret_vsa} and Bearer abcdefghijklmnop12345"
    doc = finished(results, run_id="20260104T000000Z-dddddddd")
    directory = store.write_run(tmp_path, doc, results, f"summary {secret_groq}")
    text = "".join(p.read_text() for p in directory.iterdir())
    assert secret_groq not in text and secret_vsa not in text and "abcdefghijklmnop12345" not in text
    assert "postgresql+asyncpg://***@" in store.scrub("postgresql+asyncpg://user:pw@host/db")


def test_baseline_round_trip_keeps_metrics_but_not_case_text(tmp_path):
    results = results_with(2, 0)
    doc = finished(results)
    store.save_baseline(tmp_path, "main", doc)
    saved = store.load_baseline(tmp_path, "main")
    assert saved["metrics"] == doc["metrics"] and "by_category" not in saved
    assert store.load_baseline(tmp_path, "missing") is None
    with pytest.raises(ValueError):
        store.save_baseline(tmp_path, "../evil", doc)


def test_report_renders_failures_unreviewed_warning_and_comparison():
    results = results_with(1, 1)
    doc = finished(results)
    comparison = compare.compare_runs(doc, finished(results_with(2, 0), run_id="20260101T000000Z-bbbbbbbb"))
    text = render_summary(doc, results, comparison)
    assert "No case in this dataset has been reviewed" in text
    assert "Failed or errored cases (1)" in text and "Regressions:" in text
    assert "uncalibrated" in text


def test_replay_target_rescoring_matches_the_original(tmp_path):
    results = results_with(2, 1)
    doc = finished(results, run_id="20260105T000000Z-eeeeeeee")
    directory = store.write_run(tmp_path, doc, results, "")
    target = ReplayTarget(directory / "cases.jsonl")
    ds = dataset(*[case(id=f"c{i}") for i in range(3)])
    again, status = run(ds, target, mode="replay", replay_ids=target.case_ids)
    assert status == "completed"
    assert [r["passed"] for r in again] == [r["passed"] for r in results]


def test_report_states_whether_tracing_was_active():
    base = {"run_id": "r", "started_at": "s", "mode": "retrieval", "dataset": {"name": "d", "cases": 1, "reviewed_cases": 0, "sha256": "x" * 12},
            "counts": {}, "metrics": {}, "model": {}, "judge": {}, "langsmith": {"status": "off"}}
    from evals import report

    assert "Application tracing: not recorded" in report._tracing_line(base)
    assert "Application tracing: off" in report._tracing_line({**base, "tracing": {"enabled": False}})
    on = report._tracing_line({**base, "tracing": {"enabled": True, "project": "p", "content": False}})
    assert "on (project p, metadata only)" in on
