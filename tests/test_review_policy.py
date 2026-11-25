"""Regression tests for evals/review_policy.py: approvals are recorded by a person, never decided here."""

from __future__ import annotations

import csv
from pathlib import Path

from evals import review_policy as rp
from tests.test_review_workflow import both_sheets, flagged, one, write_dataset


def _write(review_dir: Path, rows: list[tuple]) -> None:
    with (review_dir / rp.APPROVALS_FILE).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(rp.COLUMNS)
        writer.writerows(rows)


def test_no_file_means_every_decision_is_open(tmp_path):
    report = rp.status(tmp_path)
    assert report["file_present"] is False
    assert set(report["open"]) == set(rp.DECISIONS)


def test_a_recorded_approval_is_reported_as_approved(tmp_path):
    _write(tmp_path, [("C3", "owner", "2026-11-01", "doc-link")])
    report = rp.status(tmp_path)
    c3 = next(d for d in report["decisions"] if d["id"] == "C3")
    assert c3["status"] == "approved"
    assert "C3" not in report["open"]


def test_approving_a_decision_that_still_needs_a_code_change_says_so(tmp_path):
    _write(tmp_path, [("A", "owner", "2026-11-01", "doc-link")])
    report = rp.status(tmp_path)
    a = next(d for d in report["decisions"] if d["id"] == "A")
    assert a["status"] == "approved"
    assert a["needs_code_change"] is True


def test_unknown_decision_id_is_rejected_and_stays_open(tmp_path):
    _write(tmp_path, [("Z9", "owner", "2026-11-01", "doc-link")])
    report = rp.status(tmp_path)
    assert "Z9" not in {d["id"] for d in report["decisions"]}
    assert report["problems"]
    assert set(report["open"]) == set(rp.DECISIONS)


def test_a6_and_the_sub_question_ids_are_not_recognised_decision_ids(tmp_path):
    """Phase 4K/4M: docs/EVAL_POLICY_DECISIONS.md carries one card per sub-question
    (A-Q1..A-Q5, A-6) alongside the real decisions (A, B, C1, C2, C3), but the code
    recognises only the five lumped ids - never 'A-6' or any 'A-Qn'. This locks that fact
    in: a row using one of the document's card labels is rejected exactly like any other
    unknown id, never silently accepted as a sixth/extra decision and never treated as
    approving (or contradicting) the real 'A' decision."""
    doc_only_ids = ["A-6", "A-Q1", "A-Q2", "A-Q3", "A-Q4", "A-Q5"]
    for fake_id in doc_only_ids:
        assert fake_id not in rp.DECISIONS, f"{fake_id} must not become a real decision id"

    _write(tmp_path, [(doc_only_ids[0], "owner", "2026-11-01", "doc-link")])
    report = rp.status(tmp_path)
    assert "A-6" not in {d["id"] for d in report["decisions"]}
    assert report["problems"]
    # The real "A" decision is untouched by an "A-6" row - it is not silently approved,
    # and the attempted row does not count as a conflicting approval of it either.
    assert "A" in report["open"]


def test_missing_field_leaves_the_decision_open(tmp_path):
    _write(tmp_path, [("C3", "", "2026-11-01", "doc-link")])
    report = rp.status(tmp_path)
    assert "C3" in report["open"]
    assert any("needs approved_by" in p for p in report["problems"])


def test_malformed_date_leaves_the_decision_open(tmp_path):
    _write(tmp_path, [("C3", "owner", "11/01/2026", "doc-link")])
    report = rp.status(tmp_path)
    assert "C3" in report["open"]


def test_conflicting_duplicate_rows_for_one_decision_are_not_merged(tmp_path):
    _write(tmp_path, [("C3", "owner-a", "2026-11-01", "doc-a"), ("C3", "owner-b", "2026-11-02", "doc-b")])
    report = rp.status(tmp_path)
    assert "C3" in report["open"]
    assert any("already recorded" in p for p in report["problems"])


def test_approved_helper_matches_status(tmp_path):
    _write(tmp_path, [("C1", "owner", "2026-11-01", "doc-link")])
    assert rp.approved(tmp_path, "C1") is True
    assert rp.approved(tmp_path, "C2") is False


def test_missing_header_column_leaves_every_decision_open(tmp_path):
    with (tmp_path / rp.APPROVALS_FILE).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["decision_id", "approved_by", "approved_on"])  # missing reference
        writer.writerow(["C3", "owner", "2026-11-01"])
    report = rp.status(tmp_path)
    assert set(report["open"]) == set(rp.DECISIONS)
    assert report["problems"]


# ---- Cross-module: approving every decision must not move readiness -------------------------
#
# review_status.py's docstring and review_policy.py's module docstring both claim that this module is
# never imported by review_status.py and that approving a decision changes nothing in
# ready_for_validated_metrics. That claim is a grep-able fact about imports, but the behavioural half
# of it - that a fully-approved policy_approvals.csv does not flip readiness - was not itself under
# test anywhere. These two tests close that gap directly, at both ends: a dataset that is otherwise
# ready stays ready-for-the-same-reasons, and a dataset that is otherwise blocked stays blocked with
# the identical blockers, whether or not every decision (A, B, C1, C2, C3) is approved.

ALL_APPROVED_ROWS = [(decision_id, "owner", "2026-11-01", "doc-link") for decision_id in rp.DECISIONS]


def _write_approvals(review_dir: Path, rows=ALL_APPROVED_ROWS) -> None:
    with (review_dir / rp.APPROVALS_FILE).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(rp.COLUMNS)
        writer.writerows(rows)


def test_approving_every_decision_does_not_make_an_unready_dataset_ready(tmp_path):
    ds = tmp_path / "d.jsonl"
    rd = tmp_path / "review"
    rd.mkdir()
    # No sheets at all: nothing reviewed, nothing assigned - this must stay blocked regardless of policy.
    write_dataset(ds, [flagged("a", "unreviewed", reviewer="")])
    before = one(ds, rd)
    assert before["ready_for_validated_metrics"] is False

    _write_approvals(rd)  # approve every decision (A, B, C1, C2, C3)
    report = rp.status(rd)
    assert set(report["open"]) == set()  # every decision is now approved
    for decision in report["decisions"]:
        assert decision["status"] == "approved"

    after = one(ds, rd)
    assert after["ready_for_validated_metrics"] is False
    assert after["review_state"] == before["review_state"]
    assert after["blockers"] == before["blockers"]
    assert after["readiness_counts"] == before["readiness_counts"]


def test_approving_every_decision_does_not_change_an_already_ready_dataset(tmp_path):
    ds = tmp_path / "d.jsonl"
    rd = tmp_path / "review"
    rd.mkdir()
    write_dataset(ds, [flagged("a", "reviewed"), flagged("b", "reviewed")])
    both_sheets(rd, [["a", "q", "yes", "S1", "answer", "", "keep", ""],
                      ["b", "q", "yes", "S1", "answer", "", "keep", ""]])
    before = one(ds, rd)
    assert before["ready_for_validated_metrics"] is True and before["blockers"] == []

    assert not (rd / rp.APPROVALS_FILE).exists()
    for decision_id in rp.DECISIONS:
        assert rp.approved(rd, decision_id) is False  # no file yet: every decision reads as open

    _write_approvals(rd)  # now approve every decision (A, B, C1, C2, C3)
    report = rp.status(rd)
    assert set(report["open"]) == set()
    for decision in report["decisions"]:
        assert decision["status"] == "approved"

    after = one(ds, rd)
    assert after["ready_for_validated_metrics"] is True
    assert after["review_state"] == before["review_state"] == "ready"
    assert after["blockers"] == before["blockers"] == []
    assert after["readiness_counts"] == before["readiness_counts"]
