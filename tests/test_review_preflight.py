"""Read-only review preflight: what it must catch before a sheet is trusted. Offline; fixtures only."""

import csv
import hashlib
import json
import os
from pathlib import Path

import pytest

from evals import review_assign as ra, review_preflight as pf, review_sheets, review_status as rs

REPO = Path(__file__).resolve().parent.parent
KNOWLEDGE = "# Doc\n\n## Alpha\ntext\n\n## Beta\ntext\n"


def rag(i, **kw):
    return {"id": i, "kind": "rag_answer", "category": "factual", "messages": [{"role": "user", "content": f"question {i}"}],
            "expected_sources": ["Doc > Alpha"], "review_status": "unreviewed", **kw}


def gate(i):
    return {"id": i, "kind": "lead_gate", "category": "lead_gate", "messages": [{"role": "user", "content": "hi"}],
            "proposed_tool_call": {"name": "save_lead", "arguments": {}}, "expected_gate": "rejected",
            "review_status": "unreviewed"}


def column(header, name):
    return next(h for h in header if h.split(" ")[0] == name)


def fill(target, template, answers, drop=(), extra=()):
    """Copy the blank template to `target` and fill the rows named in `answers` ({case_id: {label: value}})."""
    with open(template, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames
        rows = list(reader)
    out = []
    for row in rows:
        if row["case_id"] in drop:
            continue
        for label, value in answers.get(row["case_id"], {}).items():
            row[column(header, label)] = value
        out.append(row)
    for extra_row in extra:
        new = {h: "" for h in header}
        for label, value in extra_row.items():
            new[column(header, label)] = value
        out.append(new)
    with open(target, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        writer.writerows(out)


ANSWER = {"answerable_from_kb": "yes", "evidence_sections": "Doc > Alpha", "expected_behavior": "answer",
          "ambiguous_or_exclude": "keep"}
LEAD_NO = {"should_save": "no", "ambiguous_or_exclude": "keep"}


@pytest.fixture
def env(tmp_path):
    ds = tmp_path / "x_golden_v1.jsonl"
    cases = [rag(f"c{n:02d}") for n in range(12)] + [gate("g1")]
    ds.write_text("\n".join(json.dumps(c) for c in cases), encoding="utf-8")
    rd = tmp_path / "review"
    rd.mkdir()
    template = rd / "golden_review_sheet_blank.csv"
    review_sheets.write_blank(ds, template)
    kd = tmp_path / "knowledge"
    kd.mkdir()
    (kd / "doc.md").write_text(KNOWLEDGE, encoding="utf-8")
    ids = [c["id"] for c in cases]
    everything = {i: (LEAD_NO if i == "g1" else ANSWER) for i in ids}
    return ds, rd, kd, template, everything


def run(env_):
    ds, rd, kd, *_ = env_
    return pf.run_preflight([ds], rd, kd)


def checks(report, severity=None):
    return {f["check"] for f in report["findings"] if severity in (None, f["severity"])}


def test_real_repository_state_is_startable_and_only_pending(monkeypatch):
    monkeypatch.chdir(REPO)
    report = pf.run_preflight([Path("evals/datasets/nimbuscrm_golden_v1.jsonl"),
                               Path("evals/datasets/nimbuscrm_candidates_v2.jsonl")], Path("evals/review"),
                              Path("knowledge/nimbuscrm"))
    assert report["counts"]["error"] == 0 and report["can_start_review"] is True
    assert report["verdict"] in ("incomplete", "clean")
    for dataset in report["datasets"]:
        assert dataset["template"]["ok"] is True
    assert {d["cases"] for d in report["datasets"]} == {75, 60}


def test_preflight_changes_nothing(env):
    ds, rd, kd, template, everything = env
    fill(rd / "sheet_ann-golden.csv", template, everything)
    fill(rd / "sheet_bob-golden.csv", template, everything)
    watched = [ds, *sorted(rd.iterdir()), *sorted(kd.iterdir())]
    before = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in watched}
    listing = sorted(os.listdir(rd))
    run(env)
    pf.main(["--dataset", str(ds), "--review-dir", str(rd), "--knowledge-dir", str(kd)])
    assert listing == sorted(os.listdir(rd))
    assert before == {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in watched}


def test_untouched_templates_and_no_sheets_is_pending_not_an_error(env):
    report = run(env)
    assert report["counts"]["error"] == 0 and report["can_start_review"] is True
    # assignment_not_prepared is a separate, additional pending item: no reviewer has been ASSIGNED to any
    # case yet, which is distinct from no one having SUBMITTED a sheet yet (no_reviewer_sheets).
    assert checks(report, "pending") == {"no_reviewer_sheets", "assignment_not_prepared"}


def test_a_template_with_labels_filled_in_is_an_error_and_blocks_starting(env):
    ds, rd, kd, template, everything = env
    fill(template, template, {"c00": ANSWER})
    report = run(env)
    assert "template_filled" in checks(report, "error") and report["can_start_review"] is False


def test_missing_or_mismatched_template_is_an_error(env):
    ds, rd, kd, template, _ = env
    template.unlink()
    report = run(env)
    assert "template_missing" in checks(report, "error") and report["can_start_review"] is False
    review_sheets.write_blank(ds, template)
    fill(template, template, {}, drop={"c03"})
    assert "template_cases" in checks(run(env), "error")


def test_wrong_delimiter_sheet_is_reported_not_silently_blank(env):
    ds, rd, kd, template, everything = env
    text = template.read_text(encoding="utf-8-sig").splitlines()
    (rd / "sheet_ann-golden.csv").write_text("\n".join(line.replace(",", ";") for line in text), encoding="utf-8")
    report = run(env)
    messages = " ".join(f["message"] for f in report["findings"] if f["check"] == "sheet_header")
    assert "missing column" in messages and "CSV UTF-8" in messages
    status = rs.build_report([ds], rd)
    assert any("sheet not used" in p["issue"] for p in status["problems"])
    assert status["reviewer_sheets"] == [] and status["datasets"][0]["counts"]["unreviewed"] == 13


def test_renamed_label_column_is_an_error(env):
    ds, rd, kd, template, everything = env
    text = template.read_text(encoding="utf-8-sig").replace("expected_behavior", "behaviour")
    (rd / "sheet_ann-golden.csv").write_text(text, encoding="utf-8")
    assert "sheet_header" in checks(run(env), "error")


def test_deleted_rows_unknown_ids_and_duplicates_are_errors(env):
    ds, rd, kd, template, everything = env
    fill(rd / "sheet_ann-golden.csv", template, everything, drop={"c05"},
         extra=[{"case_id": "zzz"}, {"case_id": "c01", **ANSWER}])
    report = run(env)
    errors = checks(report, "error")
    assert {"sheet_missing_rows", "sheet_unknown_ids", "sheet_duplicate_rows"} <= errors
    missing = next(f for f in report["findings"] if f["check"] == "sheet_missing_rows")
    assert missing["case_ids"] == ["c05"]
    # the duplicated case is not counted for that reviewer, nor is the deleted one
    assert rs.build_report([ds], rd)["datasets"][0]["counts"]["single_review"] == 11


def test_a_blank_extra_row_for_a_case_is_only_a_warning(env):
    ds, rd, kd, template, everything = env
    fill(rd / "sheet_ann-golden.csv", template, everything, extra=[{"case_id": "c01"}])
    report = run(env)
    assert "sheet_duplicate_rows" not in checks(report, "error") and "sheet_extra_blank_rows" in checks(report, "warning")
    assert rs.build_report([ds], rd)["datasets"][0]["counts"]["single_review"] == 13


def test_malformed_blank_and_complete_rows_are_counted_separately(env):
    ds, rd, kd, template, everything = env
    answers = {i: v for i, v in everything.items() if i not in ("c00", "c01", "c02")}
    answers["c00"] = {**ANSWER, "answerable_from_kb": "maybe"}
    answers["c01"] = {**ANSWER, "answerable_from_kb": "partly"}  # partly needs a comment
    fill(rd / "sheet_ann-golden.csv", template, answers)
    report = run(env)
    sheet = report["datasets"][0]["sheets"][0]
    assert sheet["malformed"] == 2 and sheet["blank"] == 1 and sheet["complete"] == 10
    assert "sheet_malformed_rows" in checks(report, "error") and "sheet_blank_rows" in checks(report, "pending")


def test_kind_column_mismatch_is_an_error(env):
    ds, rd, kd, template, everything = env
    text = template.read_text(encoding="utf-8-sig").replace("g1,lead_gate", "g1,rag_answer")
    (rd / "sheet_ann-golden.csv").write_text(text, encoding="utf-8")
    assert "sheet_kind_mismatch" in checks(run(env), "error")


def test_sheet_for_the_other_dataset_is_rejected(env):
    ds, rd, kd, template, everything = env
    fill(rd / "sheet_ann-candidates.csv", template, everything)  # golden ids in a -candidates sheet
    other = rd / "sheet_ann-candidates.csv"
    cand = ds.parent / "x_candidates_v2.jsonl"
    cand.write_text(json.dumps(rag("k1")), encoding="utf-8")
    report = pf.run_preflight([ds, cand], rd, ds.parent / "knowledge")
    assert any(f["scope"] == "candidates" and f["check"] == "sheet_unknown_ids" for f in report["findings"])
    assert other.exists()


def test_one_person_in_two_files_is_one_reviewer(env):
    ds, rd, kd, template, everything = env
    fill(rd / "sheet_ann-golden.csv", template, everything)
    fill(rd / "sheet_ann.csv", template, everything)  # same person, unlabelled name
    report = run(env)
    assert "same_reviewer_twice" in checks(report, "error")
    assert "too_few_reviewers" in checks(report, "pending")
    counts = rs.build_report([ds], rd)["datasets"][0]["counts"]
    assert counts["approved"] == 0 and counts["single_review"] == 13


def test_two_reviewers_remove_the_reviewer_count_finding_and_a_third_file_is_not_a_second_person(env):
    ds, rd, kd, template, everything = env
    fill(rd / "sheet_ann-golden.csv", template, everything)
    assert "too_few_reviewers" in checks(run(env), "pending")
    fill(rd / "sheet_bob-golden.csv", template, everything)
    report = run(env)
    assert not ({"too_few_reviewers", "no_reviewer_sheets"} & checks(report))
    assert report["datasets"][0]["case_status"]["approved"] == 13


def test_identical_sheets_trigger_a_warning_only(env):
    ds, rd, kd, template, everything = env
    fill(rd / "sheet_ann-golden.csv", template, everything)
    fill(rd / "sheet_bob-golden.csv", template, everything)
    report = run(env)
    assert "identical_sheets" in checks(report, "warning") and report["counts"]["error"] == 0
    # a visible difference between the two removes the warning
    different = {**everything, "c03": {**ANSWER, "evidence_sections": "Doc > Beta"}}
    fill(rd / "sheet_bob-golden.csv", template, different)
    assert "identical_sheets" not in checks(run(env))


def test_a_few_shared_rows_say_nothing_about_copying(env):
    ds, rd, kd, template, everything = env
    few = {i: everything[i] for i in list(everything)[:5]}
    fill(rd / "sheet_ann-golden.csv", template, few)
    fill(rd / "sheet_bob-golden.csv", template, few)
    assert "identical_sheets" not in checks(run(env))


def test_unknown_evidence_section_is_a_warning_and_missing_knowledge_is_noted(env):
    ds, rd, kd, template, everything = env
    fill(rd / "sheet_ann-golden.csv", template, {"c00": {**ANSWER, "evidence_sections": "Doc > Alpa"}})
    report = run(env)
    assert "unknown_evidence_section" in checks(report, "warning")
    report = pf.run_preflight([env[0]], env[1], env[0].parent / "nope")
    assert "knowledge_missing" in checks(report, "warning")


def test_unlabelled_sheet_name_is_a_warning(env):
    ds, rd, kd, template, everything = env
    fill(rd / "sheet_ann.csv", template, everything)
    assert "sheet_name_unlabelled" in checks(run(env), "warning")


def disputed_env(env_):
    ds, rd, kd, template, everything = env_
    fill(rd / "sheet_ann-golden.csv", template, everything)
    other = {**everything, "c00": {**ANSWER, "answerable_from_kb": "no", "evidence_sections": "",
                                    "expected_behavior": "decline_or_abstain"}}
    fill(rd / "sheet_bob-golden.csv", template, other)
    return ds, rd


def test_disputes_are_pending_and_need_a_disagreements_file(env):
    disputed_env(env)
    report = run(env)
    assert "no_disagreements_file" in checks(report, "pending")
    assert report["datasets"][0]["case_status"]["disputed"] == 1


def test_resolution_problems_are_reported(env):
    ds, rd = disputed_env(env)
    (rd / "disagreements.csv").write_text(
        "case_id,resolution,resolved_by\nc00,answerable,\nnope,x,carol\nc01,fine,carol\n", encoding="utf-8")
    report = run(env)
    errors = checks(report, "error")
    assert {"resolution_incomplete", "resolution_unknown_case"} <= errors
    assert "resolution_not_needed" in checks(report, "warning")  # c01 was never disputed
    assert "unresolved_disputes" in checks(report, "pending")
    assert rs.build_report([ds], rd)["datasets"][0]["counts"]["disputed"] == 1
    assert any("must both be filled" in p["issue"] for p in rs.build_report([ds], rd)["problems"])


def test_a_resolver_who_is_also_a_reviewer_is_flagged(env):
    ds, rd = disputed_env(env)
    (rd / "disagreements.csv").write_text("case_id,resolution,resolved_by\nc00,keep answer,ann\n", encoding="utf-8")
    report = run(env)
    assert "resolver_is_reviewer" in checks(report, "warning")
    assert not checks(report, "pending") & {"unresolved_disputes"}
    (rd / "disagreements.csv").write_text("case_id,resolution,resolved_by\nc00,keep answer,carol\n", encoding="utf-8")
    assert "resolver_is_reviewer" not in checks(run(env))


def test_cli_exit_codes_and_json(env, capsys):
    ds, rd, kd, template, everything = env
    argv = ["--dataset", str(ds), "--review-dir", str(rd), "--knowledge-dir", str(kd)]
    assert pf.main(argv) == 0  # only pending
    assert "verdict: incomplete" in capsys.readouterr().out
    assert pf.main(argv + ["--strict"]) == 1
    capsys.readouterr()
    assert pf.main(argv + ["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["read_only"] is True
    fill(rd / "sheet_ann-golden.csv", template, everything, drop={"c05"})
    assert pf.main(argv) == 1  # an error
    capsys.readouterr()
    assert pf.main(["--dataset", str(ds.parent / "absent.jsonl"), "--review-dir", str(rd)]) == 2


def test_clean_when_two_independent_complete_sheets_exist(env):
    ds, rd, kd, template, everything = env
    fill(rd / "sheet_ann-golden.csv", template, everything)
    different = {**everything, "c04": {**ANSWER, "evidence_sections": "Doc > Beta"}}
    different["c04"]["ambiguous_or_exclude"] = "keep"
    fill(rd / "sheet_bob-golden.csv", template, different)
    (rd / "reviewers.csv").write_text(
        "reviewer_id,display_name,role,sheets,attested_by\nann,Ann Rao,reviewer,ann-golden,review lead\n"
        "bob,Bob Iyer,reviewer,bob-golden,review lead\ncarol,Carol Shah,resolver,,review lead\n", encoding="utf-8")
    (rd / "disagreements.csv").write_text(
        "case_id,resolution,resolved_by,resolution_status,resolution_evidence,final_answerable_from_kb,"
        "final_evidence_sections,final_expected_behavior\n"
        "c04,Alpha is right,carol,resolved,Doc > Alpha states it,yes,Doc > Alpha,answer\n", encoding="utf-8")
    report = run(env)
    # Two independent, resolved sheets make the LABELS clean, but no one has prepared an assignments.csv
    # for this dataset, so case-level assignment is still pending - a separate, additional gate from
    # whether the submitted sheets themselves are well formed and agree.
    assert report["counts"]["error"] == 0
    assert checks(report, "pending") == {"assignment_not_prepared"}
    assert report["verdict"] == "incomplete"
    # clean does NOT mean ready: the dataset file is still unreviewed
    assert rs.build_report([ds], rd)["datasets"][0]["ready_for_validated_metrics"] is False


def test_preparing_assignments_removes_the_pending_finding_and_only_covers_in_scope_cases(env):
    ds, rd, kd, template, everything = env
    (rd / "reviewers.csv").write_text(
        "reviewer_id,display_name,role,sheets,attested_by\nann,Ann Rao,reviewer,,review lead\n"
        "bob,Bob Iyer,reviewer,,review lead\n", encoding="utf-8")
    report = ra.write_new_assignments(rd, {"golden": ds})
    assert report["wrote"] is True
    assert report["new_rows"] == 12 * 2  # the 12 rag_answer cases, not the lead_gate case g1
    preflight = run(env)
    assert "assignment_not_prepared" not in checks(preflight, "pending")
    assert preflight["datasets"][0]["assignment"]["in_scope"] == 12
    assert preflight["datasets"][0]["assignment"]["unassigned"] == []
    assignments = ra.load_assignments(rd / "assignments.csv")
    assert ("golden", "g1") not in assignments["by_case"]


def test_a_case_assigned_to_only_one_reviewer_is_reported_pending_in_preflight(env):
    ds, rd, kd, template, everything = env
    (rd / "reviewers.csv").write_text(
        "reviewer_id,display_name,role,sheets,attested_by\nann,Ann Rao,reviewer,,review lead\n", encoding="utf-8")
    with (rd / "assignments.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(ra.COLUMNS)
        writer.writerow(["golden", "c00", "ann", "review lead", "2026-11-01"])
    preflight = run(env)
    assert "assignment_single_reviewer" in checks(preflight, "pending")
    assert "assignment_missing_for_case" in checks(preflight, "pending")


def test_an_unregistered_reviewer_in_assignments_csv_is_an_error(env):
    ds, rd, kd, template, everything = env
    with (rd / "assignments.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(ra.COLUMNS)
        writer.writerow(["golden", "c00", "ghost", "review lead", "2026-11-01"])
    preflight = run(env)
    assert "assignment_invalid_reference" in checks(preflight, "error")
    assert preflight["can_start_review"] is True  # assignment problems never block starting the review itself


def test_open_policy_decisions_are_reported_as_info_and_never_block_starting(env):
    preflight = run(env)
    assert "policy_open" in checks(preflight, "info")
    assert set(preflight["policy"]["open"]) == {"A", "B", "C1", "C2", "C3"}
    assert preflight["policy"]["file_present"] is False
    assert preflight["can_start_review"] is True
