import json

from evals import evaluators as ev
from evals.schema import load_dataset, parse_case

GOLDEN = "evals/datasets/nimbuscrm_golden_v1.jsonl"


def make(**overrides):
    base = {
        "id": "c", "kind": "rag_answer", "category": "factual",
        "messages": [{"role": "user", "content": "q"}],
        "required_facts": ["29"], "expected_sources": ["H1"],
    }
    base.update(overrides)
    return parse_case(base)


def test_fact_matching_is_case_insensitive_and_supports_alternatives():
    assert ev.fact_present("The Pro plan is $29 per user.", "29")
    assert ev.fact_present("Includes WhatsApp", "whatsapp")
    assert ev.fact_present("There is no user limit.", "no user limit|unlimited")
    assert not ev.fact_present("It costs $290.", "29")  # not inside a longer number
    assert not ev.fact_present("It costs $2.9", "29")
    assert ev.fact_present("20% off", "20%")


def test_missing_and_forbidden_helpers():
    assert ev.missing_facts("pro and business", ["pro", "business", "starter"]) == ["starter"]
    hits = ev.forbidden_hits("The Pro plan is now free", [], [r"pro plan is (now )?free"])
    assert hits == ["/pro plan is (now )?free/"]
    assert ev.forbidden_hits("fine", ["$99"], []) == []


def test_abstention_detection():
    assert ev.looks_like_abstention("I don't have that information, but I can pass it to the sales team.")
    assert ev.looks_like_abstention("I'm not sure about that.")
    assert ev.looks_like_abstention("I can check with our sales team on that.")
    assert not ev.looks_like_abstention("The Pro plan is $29 per user per month.")


def test_contact_promise_detection():
    assert ev.promises_contact("The sales team will contact you tomorrow.")
    assert ev.promises_contact("Someone will call you soon.")
    assert not ev.promises_contact("I've passed your details to the sales team.")


def test_plain_text_and_sentence_count():
    assert ev.is_plain_text("The Pro plan is $29. It has no user limit.")
    assert not ev.is_plain_text("**Pro** plan")
    assert not ev.is_plain_text("- item one\n- item two")
    assert ev.sentence_count("One. Two. Three.") == 3
    assert ev.sentence_count("It costs $29.50 per user. Done.") == 2


def test_retrieval_check_passes_only_when_every_expected_source_is_found():
    case = make(expected_sources=["H1", "H2"])
    scores, checks = ev.evaluate_retrieval(case, ["H2", "X"])
    assert scores["recall_at_1"] == 0.5 and scores["mrr"] == 1.0
    assert checks[0].passed is False and "H1" in checks[0].detail
    scores, checks = ev.evaluate_retrieval(case, ["H1", "H2"])
    assert checks[0].passed is True and scores["recall_at_3"] == 1.0


def test_unanswerable_case_records_empty_retrieval_without_blocking():
    case = make(expected_sources=[], required_facts=[], expect_abstain=True)
    scores, checks = ev.evaluate_retrieval(case, [])
    assert scores["empty_when_unanswerable"] == 1.0 and checks[0].blocking is False
    scores, checks = ev.evaluate_retrieval(case, ["Some chunk"])
    assert scores["empty_when_unanswerable"] == 0.0 and checks[0].passed is False


def test_reply_checks():
    case = make(forbidden_facts=["$39"], expect_abstain=False)
    scores, checks = ev.evaluate_reply(case, "The Pro plan is $29 per user per month.")
    by_name = {c.name: c for c in checks}
    assert by_name["required_facts"].passed and by_name["forbidden_clear"].passed
    assert scores["required_facts"] == 1.0

    scores, checks = ev.evaluate_reply(case, "It is $39 a month.")
    by_name = {c.name: c for c in checks}
    assert not by_name["required_facts"].passed and not by_name["forbidden_clear"].passed


def test_abstain_case_requires_an_abstaining_reply():
    case = make(expected_sources=[], required_facts=[], expect_abstain=True)
    _, checks = ev.evaluate_reply(case, "Yes, we integrate with Salesforce.")
    assert {c.name: c.passed for c in checks}["abstains_when_unknown"] is False
    _, checks = ev.evaluate_reply(case, "I don't have that information. I can pass it to our sales team.")
    assert {c.name: c.passed for c in checks}["abstains_when_unknown"] is True


def test_lead_outcome_scoring():
    case = parse_case({
        "id": "l", "kind": "lead_llm", "category": "lead_llm",
        "messages": [{"role": "user", "content": "Yes"}],
        "expected_lead_saved": True, "expected_lead_email": "a@example.com",
    })
    scores, checks = ev.evaluate_lead_outcome(case, "saved", True, True, "Passed to the sales team.")
    assert scores["lead_outcome_correct"] == 1.0 and scores["lead_email_correct"] == 1.0
    scores, _ = ev.evaluate_lead_outcome(case, None, False, None, "Sure.")
    assert scores["lead_outcome_correct"] == 0.0

    negative = parse_case({
        "id": "l2", "kind": "lead_llm", "category": "lead_llm",
        "messages": [{"role": "user", "content": "No"}], "expected_lead_saved": False,
    })
    scores, _ = ev.evaluate_lead_outcome(negative, "rejected", False, None, "OK.")
    assert scores["lead_outcome_correct"] == 1.0 and scores["premature_tool_call"] == 1.0
    scores, _ = ev.evaluate_lead_outcome(negative, "saved", True, None, "The sales team will contact you.")
    assert scores["lead_outcome_correct"] == 0.0 and scores["no_contact_promise"] == 0.0


def test_every_golden_gate_case_agrees_with_the_real_consent_gate():
    """Regression guard for production behaviour: the dataset states the intended outcome."""
    gate_cases = [c for c in load_dataset(GOLDEN).cases if c.kind == "lead_gate"]
    assert len(gate_cases) >= 15
    for case in gate_cases:
        scores, checks, output = ev.evaluate_lead_gate(case)
        assert checks[0].passed, f"{case.id}: {checks[0].detail}"
        assert scores["gate_false_save"] == 0.0


def test_gate_false_save_is_flagged_when_expectation_is_reject():
    case = parse_case({
        "id": "g", "kind": "lead_gate", "category": "lead_gate",
        "messages": [
            {"role": "user", "content": "call me rahul@example.com"},
            {"role": "assistant", "content": "Confirming rahul@example.com, may sales contact you?"},
            {"role": "user", "content": "yes"},
        ],
        "proposed_tool_call": {"name": "save_lead", "arguments": {"name": "R", "email": "rahul@example.com"}},
        "expected_gate": "rejected",
    })
    scores, checks, output = ev.evaluate_lead_gate(case)
    assert output["gate_status"] == "saved"
    assert scores["gate_false_save"] == 1.0 and checks[0].passed is False
