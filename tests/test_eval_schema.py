import json

import pytest

from evals.schema import Dataset, DatasetError, load_dataset, parse_case

GOLDEN = "evals/datasets/nimbuscrm_golden_v1.jsonl"


def rag(**overrides):
    base = {
        "id": "c1",
        "kind": "rag_answer",
        "category": "factual",
        "messages": [{"role": "user", "content": "How much is Pro?"}],
        "expected_sources": ["NimbusCRM Pricing > Pro plan"],
        "required_facts": ["29"],
    }
    base.update(overrides)
    return base


def test_valid_case_parses_with_unreviewed_default():
    case = parse_case(rag())
    assert case.review_status == "unreviewed" and case.reviewer is None
    assert case.query == "How much is Pro?"


def test_reviewed_without_a_named_reviewer_is_rejected():
    with pytest.raises(DatasetError, match="requires a named 'reviewer'"):
        parse_case(rag(review_status="reviewed"))


def test_reviewed_with_reviewer_is_accepted_and_unreviewed_cannot_name_one():
    assert parse_case(rag(review_status="reviewed", reviewer="A. Person")).review_status == "reviewed"
    with pytest.raises(DatasetError, match="must not name a reviewer"):
        parse_case(rag(reviewer="A. Person"))


def test_unknown_field_fails_loudly():
    with pytest.raises(DatasetError, match="unknown field"):
        parse_case(rag(required_fact=["29"]))


def test_last_message_must_be_from_the_user_for_chat_cases():
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    with pytest.raises(DatasetError, match="last message must be from the user"):
        parse_case(rag(messages=messages))


def test_rag_case_needs_an_expectation():
    with pytest.raises(DatasetError, match="at least one expectation"):
        parse_case(rag(expected_sources=[], required_facts=[]))


def test_lead_gate_needs_proposal_and_expected_outcome():
    base = {
        "id": "g1", "kind": "lead_gate", "category": "lead_gate",
        "messages": [{"role": "user", "content": "yes"}],
    }
    with pytest.raises(DatasetError, match="proposed_tool_call"):
        parse_case(base)
    with pytest.raises(DatasetError, match="expected_gate"):
        parse_case({**base, "proposed_tool_call": {"arguments": {}}})


# ---- expected_lead_email: structural bookkeeping, never a consent-policy decision ------------
#
# These only check that the dataset's own fields are self-consistent (so evaluate_lead_outcome() always
# has an email to compare against when one is expected, and never a stray one when it is not). They do
# not change is_clear_yes(), consent_problem(), which phrases count as consent, or any expected_gate /
# expected_lead_saved label - see Decision A in docs/EVAL_POLICY_DECISIONS.md for the policy itself.

def lead_llm(**overrides):
    base = {
        "id": "l1", "kind": "lead_llm", "category": "lead_llm",
        "messages": [{"role": "user", "content": "yes"}],
        "expected_lead_saved": True,
        "expected_lead_email": "rahul@example.com",
    }
    base.update(overrides)
    return base


def test_lead_llm_expecting_a_save_needs_an_expected_email():
    with pytest.raises(DatasetError, match="needs 'expected_lead_email'"):
        parse_case(lead_llm(expected_lead_email=None))


def test_lead_llm_expecting_no_save_must_not_carry_an_expected_email():
    with pytest.raises(DatasetError, match="must not set 'expected_lead_email'"):
        parse_case(lead_llm(expected_lead_saved=False, expected_lead_email="rahul@example.com"))


def test_lead_llm_expecting_no_save_and_no_email_parses_cleanly():
    case = parse_case(lead_llm(expected_lead_saved=False, expected_lead_email=None))
    assert case.expected_lead_saved is False and case.expected_lead_email is None


def test_blank_expected_lead_email_is_rejected_like_a_missing_one():
    with pytest.raises(DatasetError, match="non-empty string"):
        parse_case(lead_llm(expected_lead_email="   "))


def test_expected_lead_email_on_a_non_lead_llm_case_is_rejected():
    with pytest.raises(DatasetError, match="only meaningful for kind 'lead_llm'"):
        parse_case(rag(expected_lead_email="rahul@example.com"))
    base = {
        "id": "g1", "kind": "lead_gate", "category": "lead_gate",
        "messages": [{"role": "user", "content": "yes"}],
        "proposed_tool_call": {"arguments": {}}, "expected_gate": "rejected",
        "expected_lead_email": "rahul@example.com",
    }
    with pytest.raises(DatasetError, match="only meaningful for kind 'lead_llm'"):
        parse_case(base)


def test_a_fully_consistent_lead_llm_case_parses_and_keeps_its_email():
    case = parse_case(lead_llm())
    assert case.expected_lead_saved is True and case.expected_lead_email == "rahul@example.com"


def test_duplicate_ids_and_bad_json_are_reported_with_line_numbers(tmp_path):
    path = tmp_path / "d.jsonl"
    line = json.dumps(rag())
    path.write_text(line + "\n" + line + "\n", encoding="utf-8")
    with pytest.raises(DatasetError, match="line 2.*duplicate"):
        load_dataset(path)

    path.write_text(line + "\n{oops\n", encoding="utf-8")
    with pytest.raises(DatasetError, match="line 2.*invalid JSON"):
        load_dataset(path)


def test_digest_is_the_same_for_lf_and_crlf_files(tmp_path):
    line = json.dumps(rag())
    lf, crlf = tmp_path / "lf.jsonl", tmp_path / "crlf.jsonl"
    lf.write_bytes((line + "\n").encode())
    crlf.write_bytes((line + "\r\n").encode())
    assert load_dataset(lf).sha256 == load_dataset(crlf).sha256


def test_golden_dataset_is_valid_and_makes_no_false_review_claims():
    dataset = load_dataset(GOLDEN)
    assert len(dataset.cases) >= 50
    kinds = {c.kind for c in dataset.cases}
    assert kinds == {"rag_answer", "lead_gate", "lead_llm"}
    # Nothing may claim human review unless a reviewer is named (enforced by the
    # parser). Whatever the count is, it must be reported honestly.
    assert dataset.describe()["reviewed_cases"] == dataset.reviewed_count
    for case in dataset.cases:
        if case.review_status == "reviewed":
            assert case.reviewer


def test_golden_dataset_covers_the_required_categories():
    categories = {c.category for c in load_dataset(GOLDEN).cases}
    assert {"factual", "multi_fact", "follow_up", "unanswerable", "hallucination_trap",
            "prompt_injection", "lead_gate", "lead_llm", "edge"} <= categories


def test_golden_expected_sources_match_the_real_knowledge_files():
    """Ground truth headings must exist in knowledge/nimbuscrm, or retrieval metrics are meaningless."""
    from pathlib import Path

    from api.chunking import split_markdown

    headings = set()
    for path in Path("knowledge/nimbuscrm").glob("*.md"):
        text = path.read_text(encoding="utf-8")
        title = next(l[2:].strip() for l in text.splitlines() if l.startswith("# "))
        headings.update(chunk.heading for chunk in split_markdown(title, text))

    for case in load_dataset(GOLDEN).cases:
        for source in case.expected_sources:
            assert source in headings, f"{case.id}: unknown heading {source!r}"


def test_excluded_status_needs_a_named_reviewer_and_is_counted_separately():
    with pytest.raises(DatasetError):
        parse_case(rag(review_status="excluded"))
    case = parse_case(rag(review_status="excluded", reviewer="A; B"))
    assert case.review_status == "excluded" and case.reviewer == "A; B"
    with pytest.raises(DatasetError):
        parse_case(rag(review_status="approved"))


def test_legacy_records_without_review_fields_still_load_as_unreviewed():
    case = parse_case(rag())
    assert case.review_status == "unreviewed" and case.reviewer is None
    dataset = load_dataset(GOLDEN)
    described = dataset.describe()
    assert described["excluded_cases"] == dataset.excluded_count
    assert described["reviewed_cases"] + described["excluded_cases"] + described["unreviewed_cases"] == described["n_cases"]


def test_facts_review_fields_default_to_unreviewed_and_are_validated():
    base = {"id": "c", "kind": "rag_answer", "category": "factual", "messages": [{"role": "user", "content": "q"}],
            "required_facts": ["29"]}
    case = parse_case(base)
    assert case.facts_review_status == "unreviewed" and case.facts_reviewer is None
    ok = parse_case({**base, "facts_review_status": "reviewed", "facts_reviewer": "Asha; Ravi"})
    assert ok.facts_review_status == "reviewed" and ok.facts_reviewer == "Asha; Ravi"
    with pytest.raises(DatasetError, match="requires a named 'facts_reviewer'"):
        parse_case({**base, "facts_review_status": "reviewed"})
    with pytest.raises(DatasetError, match="must not name a facts_reviewer"):
        parse_case({**base, "facts_reviewer": "Asha"})
    with pytest.raises(DatasetError, match="facts_review_status"):
        parse_case({**base, "facts_review_status": "approved"})


def test_dataset_description_counts_cases_with_facts_and_how_many_have_reviewed_facts():
    def mk(i, **kw):
        return parse_case({"id": i, "kind": "rag_answer", "category": "factual",
                           "messages": [{"role": "user", "content": "q"}], "expected_sources": ["S"], **kw})
    cases = [mk("a", required_facts=["1"]), mk("b", forbidden_facts=["x"], facts_review_status="reviewed", facts_reviewer="R"),
             mk("c"), mk("d", required_facts=["2"], review_status="excluded", reviewer="R")]
    info = Dataset(name="t", path="t", sha256="x", cases=cases).describe()
    assert info["facts_cases"] == 2 and info["facts_reviewed_cases"] == 1  # the excluded case is not counted
