"""Post-hoc evaluation of live turns: correctness against a trusted reference, ungraded without one."""

import pytest

from evals import live
from evals.schema import Dataset, parse_case


def case(**overrides):
    base = {"id": "pro-price", "kind": "rag_answer", "category": "factual",
            "messages": [{"role": "user", "content": "How much is the Pro plan?"}],
            "expected_sources": ["Pricing > Pro"], "required_facts": ["29", "per user"]}
    base.update(overrides)
    return parse_case(base)


def index(*cases):
    return live.build_case_index(Dataset(name="t", path="t.jsonl", sha256="x", cases=list(cases)))


Q = "how much is the pro plan"
LAT = {"rag_sources": ["Pricing > Pro"], "rag": 12, "llm": 300}


def run(reply, question="How much is the Pro plan?", latency=LAT, idx=None, **kw):
    return live.evaluate_turn(user_text=question, reply=reply, latency=latency,
                              case_index=idx if idx is not None else index(case()), **kw)


def test_correct_incorrect_and_partial_answers_against_a_matching_case():
    right = run("Pro is 29 dollars per user per month.")
    assert right["status"] == "passed" and right["correctness"] == "correct"
    wrong = run("Pro is 99 a seat.")
    assert wrong["status"] == "failed" and wrong["correctness"] == "incorrect"
    part = run("Pro starts at 29.")
    assert part["status"] == "failed" and part["correctness"] == "partial" and "per user" in part["correctness_reason"]


def test_a_forbidden_fact_is_a_contradiction_even_if_required_facts_are_present():
    idx = index(case(forbidden_facts=["free forever"]))
    out = run("Pro is 29 per user and free forever.", idx=idx)
    assert out["correctness"] == "incorrect" and "forbidden" in out["correctness_reason"]


def test_question_without_a_matching_case_is_ungraded_not_passed_or_failed():
    out = run("We integrate with Slack.", question="Do you integrate with Slack?")
    assert out["status"] == "ungraded" and out["correctness"] is None
    assert out["reference"] == {"quality": "none", "case_id": None, "match": None}
    assert "no trusted reference" in out["ungraded_reason"]


def test_matching_ignores_case_and_punctuation_but_not_meaning():
    assert run("29 per user", question="  HOW much is the Pro plan??  ")["reference"]["case_id"] == "pro-price"
    assert run("29 per user", question="How much is the Pro plan for a year?")["status"] == "ungraded"


def test_reference_quality_is_provisional_until_facts_are_reviewed():
    out = run("29 per user")
    assert out["reference"]["quality"] == "author_unreviewed" and out["correctness_provisional"] is True
    reviewed = case(review_status="reviewed", reviewer="Asha", facts_review_status="reviewed", facts_reviewer="Asha")
    out = run("29 per user", idx=index(reviewed))
    assert out["reference"]["quality"] == "reviewed" and out["correctness_provisional"] is False
    only_case_reviewed = case(review_status="reviewed", reviewer="Asha")
    assert live.reference_quality(only_case_reviewed) == "author_unreviewed"


def test_expected_decline_is_graded_for_abstention():
    idx = index(case(id="q", messages=[{"role": "user", "content": "Do you support Klingon?"}],
                     expected_sources=[], required_facts=[], expect_abstain=True))
    ok = run("I don't have that information, but our sales team can help.", question="Do you support Klingon?",
             latency={"rag_sources": []}, idx=idx)
    assert ok["correctness"] == "correct" and ok["status"] == "passed"
    bad = run("Yes, we fully support Klingon.", question="Do you support Klingon?", latency={"rag_sources": []}, idx=idx)
    assert bad["correctness"] == "incorrect" and bad["status"] == "failed"


def test_retrieval_is_measured_only_when_recorded_and_never_confused_with_empty():
    ok = run("29 per user", latency={"rag_sources": ["Pricing > Pro"]})
    assert ok["retrieval"] == {"state": "measured", "sources": ["Pricing > Pro"], "empty": False}
    assert ok["scores"]["mrr"] == 1.0
    miss = run("29 per user", latency={"rag_sources": []})
    assert miss["retrieval"]["empty"] is True and miss["status"] == "failed"  # blocking retrieval_hit
    absent = run("29 per user", latency={"llm": 300})
    assert absent["retrieval"]["state"] == "not_instrumented" and absent["retrieval"]["empty"] is None
    assert "mrr" not in absent["scores"] and absent["status"] == "passed"
    failed = run("29 per user", latency={"rag_error": True})
    assert failed["retrieval"]["state"] == "error" and failed["retrieval"]["empty"] is None


def test_reference_free_flags_are_signals_and_never_change_ungraded_status():
    out = run("Email me at leak@example.com. Sales will contact you soon. **bold**", question="Anything else?",
              latency={"rag_sources": []})
    names = {f["name"] for f in out["flags"]}
    assert {"pii_leak", "contact_promise", "markdown_in_spoken_reply", "possible_ungrounded"} <= names
    assert out["status"] == "ungraded" and out["correctness"] is None


def test_a_value_the_visitor_typed_may_be_read_back():
    out = run("Thanks, I will note rahul@example.com.", question="My email is rahul@example.com",
              visitor_texts=["My email is rahul@example.com"], latency={"rag_sources": []})
    assert "pii_leak" not in {f["name"] for f in out["flags"]}


def test_evaluator_exception_is_an_error_status_not_a_wrong_answer(monkeypatch):
    monkeypatch.setattr(live, "_correctness", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    out = run("29 per user")
    assert out["status"] == "error" and out["correctness"] is None and out["error"] == "RuntimeError"


def test_case_index_skips_excluded_multiturn_non_answer_and_ambiguous_questions():
    multi = case(id="m", messages=[{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello"},
                                   {"role": "user", "content": "Price?"}])
    dup_a, dup_b = case(id="a"), case(id="b", required_facts=["49"])
    other = case(id="o", messages=[{"role": "user", "content": "Is it on Mondays?"}])
    assert index(multi) == {}
    assert index(dup_a, dup_b) == {}  # same question twice: dropped, never guessed
    assert list(index(other)) == ["is it on mondays"]


def rows(*specs):
    return [{"conversation_id": c, "status": s, "correctness": k, "correctness_provisional": True, "flags": f}
            for c, s, k, f in specs]


def test_summary_uses_graded_denominators_and_reconciles():
    data = rows(("c1", "passed", "correct", []), ("c1", "failed", "partial", []), ("c1", "ungraded", None, [{"name": "x"}]),
                ("c2", "passed", "correct", []), ("c3", "ungraded", None, []), ("c3", "error", None, []))
    s = live.summarize(data)
    assert s["turns"] == 6 and s["conversations"] == 3  # six turns are NOT six conversations
    assert s["counts"] == {"passed": 2, "failed": 1, "ungraded": 2, "error": 1, "skipped": 0, "graded": 3}
    assert s["counts_consistent"] is True
    assert s["pass_rate"] == {"value": pytest.approx(2 / 3), "n": 3, "n_unit": "turns"}
    assert s["correctness"]["correct"] == {"count": 2, "rate": pytest.approx(2 / 3)}
    assert s["correctness"]["partial"]["count"] == 1 and s["correctness"]["incorrect"]["count"] == 0
    assert s["ungraded_rate"]["value"] == pytest.approx(2 / 6)
    assert s["flagged_turns"] == 1
    # c1 has a partial turn, c2 is all-correct, c3 has no verdict at all
    assert s["conversation_correctness"] == {"graded_conversations": 2, "all_correct": 1, "rate": 0.5, "n_unit": "conversations"}


def test_summary_of_nothing_reports_none_not_zero():
    s = live.summarize([])
    assert s["turns"] == s["conversations"] == 0 and s["pass_rate"]["value"] is None
    assert s["correctness"]["correct"]["rate"] is None and s["ungraded_rate"]["value"] is None
    assert s["conversation_correctness"]["rate"] is None


def test_all_ungraded_has_no_pass_rate():
    s = live.summarize(rows(("c1", "ungraded", None, []), ("c1", "ungraded", None, [])))
    assert s["pass_rate"]["value"] is None and s["pass_rate"]["n"] == 0 and s["ungraded_rate"]["value"] == 1.0


def test_skipped_turns_are_counted_but_leave_every_rate_denominator():
    data = rows(("c1", "passed", "correct", []), ("c1", "ungraded", None, []), ("c2", "skipped", None, []),
                ("c2", "skipped", None, []))
    s = live.summarize(data)
    assert s["counts"]["skipped"] == 2 and s["counts_consistent"] is True and s["turns"] == 4
    assert s["pass_rate"]["n"] == 1
    assert s["ungraded_rate"] == {"value": 0.5, "n": 2, "n_unit": "turns"}  # 1 of the 2 evaluated turns


def test_interrupted_or_empty_replies_are_skipped_not_judged():
    interrupted = run("Pro is 29 per", latency={"rag_sources": ["Pricing > Pro"], "interrupted": True})
    assert interrupted["status"] == "skipped" and "interrupted" in interrupted["skipped_reason"]
    assert interrupted["correctness"] is None and interrupted["checks"] == []
    assert run("   ")["status"] == "skipped"


def test_no_reference_dataset_for_the_organization_gives_an_explicit_ungraded_reason():
    out = live.evaluate_turn(user_text="How much is the Pro plan?", reply="Pro is 99.", latency=LAT,
                             case_index=index(case()), reference_available=False)
    assert out["status"] == "ungraded" and out["reference"]["quality"] == "none"
    assert out["ungraded_reason"] == "no reference dataset is configured for this organization"
