import csv
import re
from pathlib import Path

import pytest

from evals.evaluators import _contains
from evals.schema import load_dataset

CANDIDATES = "evals/datasets/nimbuscrm_candidates_v2.jsonl"
GOLDEN = "evals/datasets/nimbuscrm_golden_v1.jsonl"
SHEET = "evals/review/candidate_review_sheet_blank.csv"
ANSWERABLE = {"factual", "multi_fact"}
UNANSWERABLE = {"unanswerable", "out_of_scope"}
EDGE = {"edge", "hallucination_trap", "prompt_injection"}


@pytest.fixture(scope="module")
def candidates():
    return load_dataset(CANDIDATES)


@pytest.fixture(scope="module")
def knowledge():
    from api.chunking import split_markdown

    sections = {}
    for path in Path("knowledge/nimbuscrm").glob("*.md"):
        text = path.read_text(encoding="utf-8")
        title = next(l[2:].strip() for l in text.splitlines() if l.startswith("# "))
        for chunk in split_markdown(title, text):
            sections[chunk.heading] = chunk.content
    return sections


def norm(text):
    return re.sub(r"\s+", " ", text.lower()).strip()


def test_size_and_mix(candidates):
    cats = [c.category for c in candidates.cases]
    assert len(cats) == 60
    assert sum(c in ANSWERABLE for c in cats) == 30
    assert sum(c in UNANSWERABLE for c in cats) == 20
    assert sum(c in EDGE for c in cats) == 10


def test_every_case_is_unreviewed_with_no_reviewer(candidates):
    assert candidates.reviewed_count == 0
    assert all(c.review_status == "unreviewed" and c.reviewer is None for c in candidates.cases)


def test_ids_unique_prefixed_and_disjoint_from_golden(candidates):
    ids = [c.id for c in candidates.cases]
    assert len(ids) == len(set(ids)) and all(i.startswith("cand-") for i in ids)
    assert not set(ids) & {c.id for c in load_dataset(GOLDEN).cases}


def test_no_duplicate_questions_within_or_against_golden(candidates):
    golden = {norm(c.query) for c in load_dataset(GOLDEN).cases}
    seen = set()
    for case in candidates.cases:
        q = norm(case.query)
        assert q not in golden, f"{case.id} repeats a golden question"
        assert q not in seen, f"{case.id} repeats another candidate"
        seen.add(q)


def test_expected_sources_exist_in_the_knowledge_files(candidates, knowledge):
    for case in candidates.cases:
        for source in case.expected_sources:
            assert source in knowledge, f"{case.id}: unknown heading {source!r}"


def test_required_facts_are_supported_by_the_expected_sections(candidates, knowledge):
    for case in candidates.cases:
        if not case.required_facts:
            continue
        derived = (case.notes or "").startswith("Derived:")
        text = norm(" ".join(f"{s} {knowledge[s]}" for s in case.expected_sources))  # heading names the plan
        for fact in case.required_facts:
            alternatives = [a for a in fact.split("|") if a.strip()]
            supported = any(_contains(text, a) for a in alternatives)
            # Negative statements ("does not include") are phrased by the reply, not quoted.
            negative = all(a in {"not include", "does not", "doesn't", "no marketing", "isn't", "is not", "cannot", "can't"} for a in alternatives)
            assert supported or derived or negative, f"{case.id}: fact {fact!r} not found in its expected sections"


def test_category_contract(candidates):
    for case in candidates.cases:
        if case.category in ANSWERABLE:
            assert case.expected_sources and case.required_facts, case.id
            assert not case.expect_abstain, case.id
        if case.category in UNANSWERABLE:
            assert not case.expected_sources and case.expect_abstain, case.id
        if case.category == "prompt_injection":
            # An injection attempt is never labelled answerable just because it names a product.
            assert not case.expected_sources and not case.required_facts, case.id
            assert case.forbidden_regex, case.id


def test_forbidden_regexes_compile(candidates):
    for case in candidates.cases:
        for pattern in case.forbidden_regex:
            re.compile(pattern, re.I)


def test_blank_review_sheet_matches_and_is_blind(candidates):
    with open(SHEET, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [r["case_id"] for r in rows] == [c.id for c in candidates.cases]
    header = " ".join(rows[0].keys()).lower()
    for leaked in ("expected_sources", "required_facts", "category", "notes"):
        assert leaked not in header
    assert all(not any(v for k, v in r.items() if k not in ("case_id", "question")) for r in rows)
    assert [r["question"] for r in rows] == [c.query for c in candidates.cases]


def test_candidates_not_the_default_run_dataset():
    from evals import run

    assert "candidates" not in str(getattr(run, "DEFAULT_DATASET", ""))
