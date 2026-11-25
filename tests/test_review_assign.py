"""Regression tests for evals/review_assign.py: assignment preparation, never a completed review."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from evals import review_assign as ra
from evals.review_identity import load_registry

GOLDEN_KB = [{"id": f"g{i}", "kind": "rag_answer", "messages": [{"role": "user", "content": "hi"}],
             "required_facts": ["x"]} for i in range(1, 4)]
GOLDEN_LEAD = [{"id": "g-lead-1", "kind": "lead_gate", "messages": [{"role": "user", "content": "hi"}],
               "proposed_tool_call": {"name": "save_lead", "arguments": {}}, "expected_gate": "saved"},
              {"id": "g-lead-2", "kind": "lead_llm", "messages": [{"role": "user", "content": "hi"}],
               "expected_lead_saved": True}]
CANDIDATES = [{"id": f"c{i}", "kind": "rag_answer", "messages": [{"role": "user", "content": "hi"}],
              "required_facts": ["x"]} for i in range(1, 3)]


def _write_jsonl(path: Path, cases: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(c) for c in cases) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def world(tmp_path):
    golden = _write_jsonl(tmp_path / "nimbuscrm_golden_v1.jsonl", GOLDEN_KB + GOLDEN_LEAD)
    candidates = _write_jsonl(tmp_path / "nimbuscrm_candidates_v2.jsonl", CANDIDATES)
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    return {"golden": golden, "candidates": candidates, "review_dir": review_dir}


def _registry(review_dir: Path, rows: list[tuple]) -> None:
    with (review_dir / "reviewers.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["reviewer_id", "display_name", "role", "sheets", "attested_by"])
        writer.writerows(rows)


def test_in_scope_excludes_lead_and_consent_cases(world):
    assert ra.in_scope_case_ids(world["golden"]) == ["g1", "g2", "g3"]
    assert ra.in_scope_case_ids(world["candidates"]) == ["c1", "c2"]


def test_missing_registry_blocks_and_writes_nothing(world):
    report = ra.write_new_assignments(world["review_dir"], {"golden": world["golden"]})
    assert report["wrote"] is False
    assert any("reviewers.csv does not exist" in m for m in report["registry_problems"])
    assert not (world["review_dir"] / "assignments.csv").exists()


def test_too_few_registered_reviewers_blocks(world):
    _registry(world["review_dir"], [("asha", "Asha Verma", "reviewer", "", "lead")])
    report = ra.write_new_assignments(world["review_dir"], {"golden": world["golden"]})
    assert report["wrote"] is False
    assert any("only 1 registered reviewer" in m for m in report["plan_problems"])


def test_valid_registry_assigns_two_distinct_reviewers_per_case(world):
    _registry(world["review_dir"], [("asha", "Asha Verma", "reviewer", "", "lead"),
                                     ("ravi", "Ravi Menon", "reviewer", "", "lead")])
    report = ra.write_new_assignments(world["review_dir"], {"golden": world["golden"], "candidates": world["candidates"]})
    assert report["wrote"] is True
    assert report["new_rows"] == 5 * 2  # 3 golden kb + 2 candidate cases, 2 reviewers each
    rows = ra.load_assignments(world["review_dir"] / "assignments.csv")
    for case_id in ["g1", "g2", "g3"]:
        assert set(rows["by_case"][("golden", case_id)]) == {"asha", "ravi"}
    for case_id in ["c1", "c2"]:
        assert set(rows["by_case"][("candidates", case_id)]) == {"asha", "ravi"}
    assert ("golden", "g-lead-1") not in rows["by_case"]
    assert ("golden", "g-lead-2") not in rows["by_case"]


def test_resolver_role_is_never_used_as_a_reviewer(world):
    _registry(world["review_dir"], [("asha", "Asha Verma", "reviewer", "", "lead"),
                                     ("kiran", "Kiran Das", "resolver", "", "lead")])
    report = ra.write_new_assignments(world["review_dir"], {"golden": world["golden"]})
    assert report["wrote"] is False
    assert any("only 1 registered reviewer" in m for m in report["plan_problems"])


def test_rerun_is_safe_and_never_duplicates_or_rewrites_existing_rows(world):
    _registry(world["review_dir"], [("asha", "Asha Verma", "reviewer", "", "lead"),
                                     ("ravi", "Ravi Menon", "reviewer", "", "lead")])
    ra.write_new_assignments(world["review_dir"], {"golden": world["golden"]})
    before = (world["review_dir"] / "assignments.csv").read_text(encoding="utf-8")
    report = ra.write_new_assignments(world["review_dir"], {"golden": world["golden"]})
    after = (world["review_dir"] / "assignments.csv").read_text(encoding="utf-8")
    assert report["new_rows"] == 0
    assert report["wrote"] is False
    assert before == after


def test_new_case_added_later_gets_assigned_without_touching_existing_cases(world):
    _registry(world["review_dir"], [("asha", "Asha Verma", "reviewer", "", "lead"),
                                     ("ravi", "Ravi Menon", "reviewer", "", "lead")])
    ra.write_new_assignments(world["review_dir"], {"golden": world["golden"]})
    before = ra.load_assignments(world["review_dir"] / "assignments.csv")["by_case"][("golden", "g1")]
    _write_jsonl(world["golden"], GOLDEN_KB + [{"id": "g4", "kind": "rag_answer",
                 "messages": [{"role": "user", "content": "hi"}], "required_facts": ["x"]}] + GOLDEN_LEAD)
    report = ra.write_new_assignments(world["review_dir"], {"golden": world["golden"]})
    rows = ra.load_assignments(world["review_dir"] / "assignments.csv")
    assert report["new_rows"] == 2
    assert rows["by_case"][("golden", "g1")] == before
    assert set(rows["by_case"][("golden", "g4")]) == {"asha", "ravi"}


def test_a_case_already_assigned_to_only_one_reviewer_is_left_alone_not_topped_up(world):
    review_dir = world["review_dir"]
    _registry(review_dir, [("asha", "Asha Verma", "reviewer", "", "lead"),
                           ("ravi", "Ravi Menon", "reviewer", "", "lead")])
    with (review_dir / "assignments.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(ra.COLUMNS)
        writer.writerow(["golden", "g1", "asha", "lead", "2026-10-01"])
    report = ra.write_new_assignments(review_dir, {"golden": world["golden"]})
    rows = ra.load_assignments(review_dir / "assignments.csv")
    assert rows["by_case"][("golden", "g1")] == ["asha"]
    assert report["new_rows"] == 2 * 2  # g2 and g3 only


def test_duplicate_row_in_assignments_file_is_flagged_and_ignored(world):
    review_dir = world["review_dir"]
    with (review_dir / "assignments.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(ra.COLUMNS)
        writer.writerow(["golden", "g1", "asha", "lead", "2026-10-01"])
        writer.writerow(["golden", "g1", "asha", "lead", "2026-10-01"])
    rows = ra.load_assignments(review_dir / "assignments.csv")
    assert rows["by_case"][("golden", "g1")] == ["asha"]
    assert any("already assigned" in p for p in rows["problems"])


def test_unknown_case_id_in_assignments_is_flagged(world):
    review_dir = world["review_dir"]
    _registry(review_dir, [("asha", "Asha Verma", "reviewer", "", "lead")])
    with (review_dir / "assignments.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(ra.COLUMNS)
        writer.writerow(["golden", "g-does-not-exist", "asha", "lead", "2026-10-01"])
    registry = load_registry(review_dir)
    assignments = ra.load_assignments(review_dir / "assignments.csv")
    problems = ra.validate({"golden": world["golden"]}, assignments, registry)
    assert any("not an in-scope rag_answer case id" in p for p in problems)


def test_lead_consent_case_manually_slipped_into_assignments_is_flagged(world):
    review_dir = world["review_dir"]
    _registry(review_dir, [("asha", "Asha Verma", "reviewer", "", "lead")])
    with (review_dir / "assignments.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(ra.COLUMNS)
        writer.writerow(["golden", "g-lead-1", "asha", "lead", "2026-10-01"])
    registry = load_registry(review_dir)
    assignments = ra.load_assignments(review_dir / "assignments.csv")
    problems = ra.validate({"golden": world["golden"]}, assignments, registry)
    assert any("not an in-scope rag_answer case id" in p for p in problems)


def test_a_resolver_listed_as_a_case_reviewer_in_assignments_is_flagged(world):
    review_dir = world["review_dir"]
    _registry(review_dir, [("asha", "Asha Verma", "reviewer", "", "lead"),
                           ("kiran", "Kiran Das", "resolver", "", "lead")])
    with (review_dir / "assignments.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(ra.COLUMNS)
        writer.writerow(["golden", "g1", "kiran", "lead", "2026-10-01"])
    registry = load_registry(review_dir)
    assignments = ra.load_assignments(review_dir / "assignments.csv")
    problems = ra.validate({"golden": world["golden"]}, assignments, registry)
    assert any("'kiran' is registered as resolver, not reviewer" in p for p in problems)


def test_unregistered_reviewer_reference_is_flagged(world):
    review_dir = world["review_dir"]
    _registry(review_dir, [("asha", "Asha Verma", "reviewer", "", "lead")])
    with (review_dir / "assignments.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(ra.COLUMNS)
        writer.writerow(["golden", "g1", "ghost", "lead", "2026-10-01"])
    registry = load_registry(review_dir)
    assignments = ra.load_assignments(review_dir / "assignments.csv")
    problems = ra.validate({"golden": world["golden"]}, assignments, registry)
    assert any("'ghost' is not in reviewers.csv" in p for p in problems)


def test_dataset_label_mismatch_in_cli_input_is_rejected(world, capsys):
    swapped = {"golden": world["candidates"]}  # wrong path for the golden label
    rc = ra.main(["--golden", str(swapped["golden"]), "--review-dir", str(world["review_dir"]), "--check"])
    assert rc == 2
    assert "does not look like a 'golden' dataset" in capsys.readouterr().err


def test_coverage_reports_unassigned_and_single_reviewer_cases(world):
    review_dir = world["review_dir"]
    with (review_dir / "assignments.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(ra.COLUMNS)
        writer.writerow(["golden", "g1", "asha", "lead", "2026-10-01"])
        writer.writerow(["golden", "g1", "ravi", "lead", "2026-10-01"])
        writer.writerow(["golden", "g2", "asha", "lead", "2026-10-01"])
    assignments = ra.load_assignments(review_dir / "assignments.csv")
    report = ra.coverage({"golden": world["golden"]}, assignments["by_case"])
    assert report["golden"]["in_scope"] == 3
    assert report["golden"]["assigned_with_2plus"] == 1
    assert report["golden"]["single_reviewer_only"] == ["g2"]
    assert report["golden"]["unassigned"] == ["g3"]


def test_registry_problems_block_writing_new_assignments(world):
    review_dir = world["review_dir"]
    with (review_dir / "reviewers.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["reviewer_id", "display_name", "role", "sheets", "attested_by"])
        writer.writerow(["asha", "Asha Verma", "reviewer", "", ""])  # missing attested_by -> registry problem
        writer.writerow(["ravi", "Ravi Menon", "reviewer", "", "lead"])
    report = ra.write_new_assignments(review_dir, {"golden": world["golden"]})
    assert report["wrote"] is False
    assert report["registry_problems"]


def test_assignment_generation_is_deterministic_across_independent_runs(world, tmp_path):
    rows = [("asha", "Asha Verma", "reviewer", "", "lead"), ("ravi", "Ravi Menon", "reviewer", "", "lead"),
            ("kiran", "Kiran Das", "resolver", "", "lead")]
    rd1, rd2 = world["review_dir"], tmp_path / "review2"
    rd2.mkdir()
    _registry(rd1, rows)
    _registry(rd2, rows)
    ra.write_new_assignments(rd1, {"golden": world["golden"], "candidates": world["candidates"]})
    ra.write_new_assignments(rd2, {"golden": world["golden"], "candidates": world["candidates"]})
    assert (rd1 / "assignments.csv").read_text(encoding="utf-8") == (rd2 / "assignments.csv").read_text(encoding="utf-8")


def test_assignment_generation_never_touches_review_sheets(world):
    review_dir = world["review_dir"]
    _registry(review_dir, [("asha", "Asha Verma", "reviewer", "", "lead"),
                           ("ravi", "Ravi Menon", "reviewer", "", "lead")])
    sheet = review_dir / "sheet_asha-golden.csv"
    sheet.write_text("case_id,answerable_from_kb\ng1,yes\n", encoding="utf-8")
    before = sheet.read_text(encoding="utf-8")
    ra.write_new_assignments(review_dir, {"golden": world["golden"]})
    assert sheet.read_text(encoding="utf-8") == before
    assert sorted(p.name for p in review_dir.iterdir()) == ["assignments.csv", "reviewers.csv", "sheet_asha-golden.csv"]


def test_all_111_style_in_scope_cases_across_both_datasets_get_assigned(world):
    _registry(world["review_dir"], [("asha", "Asha Verma", "reviewer", "", "lead"),
                                     ("ravi", "Ravi Menon", "reviewer", "", "lead")])
    report = ra.write_new_assignments(world["review_dir"], {"golden": world["golden"], "candidates": world["candidates"]})
    assignments = ra.load_assignments(world["review_dir"] / "assignments.csv")
    cov = ra.coverage({"golden": world["golden"], "candidates": world["candidates"]}, assignments["by_case"])
    total_in_scope = cov["golden"]["in_scope"] + cov["candidates"]["in_scope"]
    total_unassigned = len(cov["golden"]["unassigned"]) + len(cov["candidates"]["unassigned"])
    assert total_in_scope == 5  # 3 golden rag_answer + 2 candidate cases in this fixture
    assert total_unassigned == 0
    assert report["new_rows"] == total_in_scope * 2


def test_check_mode_writes_nothing(world):
    review_dir = world["review_dir"]
    _registry(review_dir, [("asha", "Asha Verma", "reviewer", "", "lead"),
                           ("ravi", "Ravi Menon", "reviewer", "", "lead")])
    rc = ra.main(["--golden", str(world["golden"]), "--review-dir", str(review_dir), "--check"])
    assert rc == 0  # unassigned cases are expected pre-assignment, not a problem; nothing is written either way
    assert not (review_dir / "assignments.csv").exists()
