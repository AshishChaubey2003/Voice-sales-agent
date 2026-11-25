"""Human-review workflow: sheets, status, disagreements, readiness. Offline; fixtures only."""

import csv
import json

import pytest

from evals import review_identity as ri, review_sheets, review_status as rs

HEADER = ["case_id", "question", "answerable_from_kb (yes/no/partly)", "evidence_sections", "expected_behavior",
          "should_save (yes/no)", "ambiguous_or_exclude (keep/ambiguous/exclude)", "comment"]


def write_dataset(path, cases):
    path.write_text("\n".join(json.dumps(c) for c in cases), encoding="utf-8")


def rag(i, sources=("S1",), **kw):
    return {"id": i, "kind": "rag_answer", "category": "factual", "messages": [{"role": "user", "content": "q"}],
            "expected_sources": list(sources), "review_status": "unreviewed", **kw}


REGISTRY_HEADER = list(ri.COLUMNS)


def register(rd, resolvers=("r3", "carol")):
    """Write reviewers.csv the way a review lead would: one identity per sheet-name stem, plus resolvers.

    Test fixtures only. The identity of a sheet here is its file-name stem, so two files that differ only by a
    -golden/-candidates ending are one person, exactly as the legacy rule had it."""
    people: dict[str, list[str]] = {}
    for path in sorted(rd.glob("sheet_*.csv")):
        key = ri.norm_sheet(path.name)
        people.setdefault(rs.reviewer_identity(key), []).append(key)
    with open(rd / "reviewers.csv", "w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(REGISTRY_HEADER)
        for rid, keys in people.items():
            w.writerow([rid, f"Person {rid}", "reviewer", "; ".join(keys), "review lead"])
        for rid in resolvers:
            w.writerow([rid, f"Resolver {rid}", "resolver", "", "review lead"])


def sheet(path, rows, declare=True):
    with open(path, "w", newline="", encoding="utf-8-sig") as h:
        w = csv.writer(h)
        w.writerow(HEADER)
        for r in rows:
            w.writerow(r)
    if declare:
        register(path.parent)


RESOLUTION_COLUMNS = ["case_id", "resolution", "resolved_by", "final_ambiguous_or_exclude", "resolution_status",
                      "resolution_evidence", "final_answerable_from_kb", "final_evidence_sections",
                      "final_expected_behavior", "final_should_save"]
RAG_FINAL = {"final_answerable_from_kb": "yes", "final_evidence_sections": "S1", "final_expected_behavior": "answer"}


def resolution_row(case_id, **kw):
    """A complete, valid resolution record for a rag case (override any column)."""
    row = {"case_id": case_id, "resolution": "reviewer 1 read the heading correctly", "resolved_by": "r3",
           "final_ambiguous_or_exclude": "", "resolution_status": "resolved",
           "resolution_evidence": "S1 states the fact", **RAG_FINAL}
    row.update(kw)
    return row


def write_resolutions(rd, *rows):
    with open(rd / "disagreements.csv", "w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=RESOLUTION_COLUMNS)
        w.writeheader()
        w.writerows({c: r.get(c, "") for c in RESOLUTION_COLUMNS} for r in rows)


def good(i, answerable="yes", sections="S1", behavior="answer", exclude="keep", comment="", save=""):
    return [i, "q", answerable, sections, behavior, save, exclude, comment]


@pytest.fixture
def env(tmp_path):
    ds = tmp_path / "d.jsonl"
    rd = tmp_path / "review"
    rd.mkdir()
    return ds, rd


def counts(ds, rd):
    return rs.build_report([ds], rd)["datasets"][0]["counts"]


def test_nothing_is_reviewed_without_sheets(env):
    ds, rd = env
    write_dataset(ds, [rag("a"), rag("b")])
    report = rs.build_report([ds], rd)["datasets"][0]
    assert report["counts"]["unreviewed"] == 2 and report["ready_for_validated_metrics"] is False
    assert report["dataset_marked_reviewed"] == 0


def test_a_sheet_file_alone_does_not_mark_anything_reviewed(env):
    ds, rd = env
    write_dataset(ds, [rag("a")])
    sheet(rd / "sheet_r1.csv", [["a", "q", "", "", "", "", "", ""]])  # exists, but blank
    assert counts(ds, rd)["unreviewed"] == 1


def test_two_matching_complete_sheets_approve_but_the_dataset_stays_unchanged(env):
    ds, rd = env
    write_dataset(ds, [rag("a")])
    before = ds.read_text()
    sheet(rd / "sheet_r1.csv", [good("a")])
    sheet(rd / "sheet_r2.csv", [good("a")])
    report = rs.build_report([ds], rd)["datasets"][0]
    assert report["counts"]["approved"] == 1
    assert report["ready_for_validated_metrics"] is False  # the .jsonl is still unreviewed
    assert ds.read_text() == before


def test_one_reviewer_is_not_enough(env):
    ds, rd = env
    write_dataset(ds, [rag("a")])
    sheet(rd / "sheet_r1.csv", [good("a")])
    assert counts(ds, rd)["single_review"] == 1


def test_disagreement_stays_visible_until_resolved(env):
    ds, rd = env
    write_dataset(ds, [rag("a")])
    sheet(rd / "sheet_r1.csv", [good("a")])
    sheet(rd / "sheet_r2.csv", [good("a", answerable="no", sections="", behavior="decline_or_abstain")])
    assert counts(ds, rd)["disputed"] == 1
    (rd / "disagreements.csv").write_text("case_id,resolution,resolved_by\na,,\n")
    assert counts(ds, rd)["disputed"] == 1  # a row without a decision resolves nothing
    (rd / "disagreements.csv").write_text("case_id,resolution,resolved_by\na,answerable,r3\n")
    assert counts(ds, rd)["disputed"] == 1  # with a reviewer registry the bare legacy record lacks evidence and final labels
    write_resolutions(rd, resolution_row("a"))
    assert counts(ds, rd)["resolved"] == 1


def test_malformed_and_incomplete_rows_are_reported_and_never_counted(env):
    ds, rd = env
    write_dataset(ds, [rag("a"), rag("b"), rag("c")])
    sheet(rd / "sheet_r1.csv", [
        good("a", answerable="maybe"),                       # not in the vocabulary
        good("b", answerable="partly"),                      # partly needs a comment
        ["c", "q", "yes", "", "answer", "", "keep", ""],     # answerable but no evidence
    ])
    sheet(rd / "sheet_r2.csv", [good("a"), good("b"), good("c")])
    report = rs.build_report([ds], rd)
    assert report["datasets"][0]["counts"]["needs_fix"] == 3
    issues = " ".join(p["issue"] for p in report["problems"])
    assert "answerable_from_kb must be one of" in issues and "comment required" in issues and "evidence_sections required" in issues


def test_duplicate_case_rows_in_one_sheet_are_a_problem(env):
    ds, rd = env
    write_dataset(ds, [rag("a")])
    sheet(rd / "sheet_r1.csv", [good("a"), good("a")])
    report = rs.build_report([ds], rd)
    assert any("more than once" in p["issue"] for p in report["problems"])
    assert report["datasets"][0]["counts"]["unreviewed"] == 1


def test_excluded_when_both_reviewers_exclude(env):
    ds, rd = env
    write_dataset(ds, [rag("a")])
    for name in ("r1", "r2"):
        sheet(rd / f"sheet_{name}.csv", [good("a", exclude="exclude", comment="duplicate")])
    assert counts(ds, rd)["excluded"] == 1


def test_unknown_case_ids_are_reported(env):
    ds, rd = env
    write_dataset(ds, [rag("a")])
    sheet(rd / "sheet_r1.csv", [good("zzz")])
    assert rs.build_report([ds], rd)["sheet_ids_not_in_any_dataset"] == ["zzz"]


def test_approved_labels_that_differ_from_the_dataset_are_flagged(env):
    ds, rd = env
    write_dataset(ds, [rag("a", sources=("S1",))])
    for name in ("r1", "r2"):
        sheet(rd / f"sheet_{name}.csv", [good("a", sections="S2")])
    report = rs.build_report([ds], rd)["datasets"][0]
    assert report["approved_but_dataset_differs"] == [{"case_id": "a", "fields": ["expected_sources"]}]
    assert any("differ" in b for b in report["blockers"])


def test_lead_cases_compare_should_save_with_the_expected_outcome(env):
    ds, rd = env
    gate = {"id": "g", "kind": "lead_gate", "category": "lead_gate", "messages": [{"role": "user", "content": "q"}],
            "expected_gate": "saved", "review_status": "unreviewed"}
    write_dataset(ds, [gate])
    for name in ("r1", "r2"):
        sheet(rd / f"sheet_{name}.csv", [["g", "q", "", "", "", "no", "keep", ""]])
    report = rs.build_report([ds], rd)["datasets"][0]
    assert report["counts"]["approved"] == 1
    assert report["approved_but_dataset_differs"] == [{"case_id": "g", "fields": ["expected_gate"]}]


def test_ready_only_when_every_case_is_marked_reviewed_and_supported(env):
    ds, rd = env
    reviewed = rag("a", review_status="reviewed", reviewer="r1; r2")
    write_dataset(ds, [reviewed])
    assert rs.build_report([ds], rd)["datasets"][0]["ready_for_validated_metrics"] is False  # no sheet support
    for name in ("r1", "r2"):
        sheet(rd / f"sheet_{name}.csv", [good("a")])
    report = rs.build_report([ds], rd)["datasets"][0]
    assert report["ready_for_validated_metrics"] is True and report["blockers"] == []


def test_disagreement_template_is_new_file_only_and_lists_disputed_cases(env):
    ds, rd = env
    write_dataset(ds, [rag("a"), rag("b")])
    sheet(rd / "sheet_r1.csv", [good("a"), good("b")])
    sheet(rd / "sheet_r2.csv", [good("a", answerable="no", sections="", behavior="decline_or_abstain"), good("b")])
    target = rd / "disagreements_template.csv"
    assert rs.write_disagreements([ds], rd, target) == 1
    text = target.read_text(encoding="utf-8-sig")
    assert "a," in text and "resolution" in text and "labels_r1" in text and "labels_r2" in text
    with pytest.raises(FileExistsError):
        rs.write_disagreements([ds], rd, target)


def test_blank_sheet_hides_expected_labels_keeps_ids_and_never_overwrites(tmp_path):
    ds = tmp_path / "d.jsonl"
    case = {"id": "gate-x", "kind": "lead_gate", "category": "lead_gate",
            "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok?"}],
            "proposed_tool_call": {"name": "save_lead", "arguments": {"name": "N"}}, "expected_gate": "saved",
            "review_status": "unreviewed"}
    write_dataset(ds, [case, rag("r1", sources=("SECRET SECTION",), required_facts=["secret fact"])])
    target = tmp_path / "blank.csv"
    assert review_sheets.write_blank(ds, target) == 2
    text = target.read_text(encoding="utf-8-sig")
    assert "gate-x" in text and "user: hi || assistant: ok?" in text and "save_lead" in text
    assert "SECRET SECTION" not in text and "secret fact" not in text and "saved" not in text.split("\n", 1)[1].replace("save_lead", "")
    with pytest.raises(FileExistsError):
        review_sheets.write_blank(ds, target)


def test_real_datasets_are_unreviewed_and_the_report_says_so():
    report = rs.build_report([rs.Path("evals/datasets/nimbuscrm_golden_v1.jsonl"),
                              rs.Path("evals/datasets/nimbuscrm_candidates_v2.jsonl")], rs.Path("evals/review"))
    for data in report["datasets"]:
        if data["dataset_marked_reviewed"] == 0:
            assert data["ready_for_validated_metrics"] is False


def test_section_headings_keep_their_case_and_order_does_not_cause_a_dispute(env):
    ds, rd = env
    write_dataset(ds, [rag("a", sources=("Title > Section One", "Title > Section Two"))])
    sheet(rd / "sheet_r1.csv", [good("a", sections="Title > Section One; Title > Section Two")])
    sheet(rd / "sheet_r2.csv", [good("a", sections=" Title > Section Two;Title > Section One ")])
    report = rs.build_report([ds], rd)["datasets"][0]
    assert report["counts"]["approved"] == 1 and report["approved_but_dataset_differs"] == []


# ---------------------------------------------------------------------------------------------
# Excluded-case policy, review state model, reviewer identity, sheet/dataset mapping, read-only
# ---------------------------------------------------------------------------------------------
import hashlib
from pathlib import Path

from evals import runner
from evals.report import render_summary
from evals.schema import parse_case


def flagged(i, status, reviewer="r1; r2", **kw):
    return rag(i, review_status=status, reviewer=reviewer, **kw)


def both_sheets(rd, rows):
    for name in ("r1", "r2"):
        sheet(rd / f"sheet_{name}.csv", rows)


def one(ds, rd):
    return rs.build_report([ds], rd)["datasets"][0]


def snapshot(*roots):
    """Path -> content hash for every file under the roots; used to prove read-only behaviour."""
    state = {}
    for root in roots:
        for path in sorted(Path(root).rglob("*")):
            if path.is_file():
                state[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return state


EXCLUDE = dict(exclude="exclude", comment="question is duplicated by another case")


# ---- Workstream A: excluded cases -------------------------------------------------------------

def test_a_fully_reviewed_dataset_with_an_intentional_exclusion_is_ready(env):
    ds, rd = env
    write_dataset(ds, [flagged("a", "reviewed"), flagged("b", "excluded")])
    both_sheets(rd, [good("a"), good("b", **EXCLUDE)])
    report = one(ds, rd)
    assert report["counts"]["approved"] == 1 and report["counts"]["excluded"] == 1
    assert report["review_state"] == "ready" and report["ready_for_validated_metrics"] is True and report["blockers"] == []
    assert report["readiness_counts"]["eligible_metric_cases"] == 1       # the excluded case is not a metric example
    assert report["readiness_counts"]["confirmed_excluded"] == 1
    assert report["readiness_counts"]["total_cases"] == 2


def test_an_excluded_flag_without_reviewer_evidence_does_not_count_and_blocks_readiness(env):
    ds, rd = env
    # 1. flag only, no sheets at all
    write_dataset(ds, [flagged("a", "reviewed"), flagged("b", "excluded")])
    both_sheets(rd, [good("a")])
    report = one(ds, rd)
    assert report["marked_excluded_without_agreement"] == ["b"] and report["ready_for_validated_metrics"] is False
    assert report["review_state"] == "evidence_inconsistent"
    # 2. only ONE reviewer excluded it
    sheet(rd / "sheet_r1.csv", [good("a"), good("b", **EXCLUDE)])
    sheet(rd / "sheet_r2.csv", [good("a")])
    assert one(ds, rd)["marked_excluded_without_agreement"] == ["b"]
    # 3. both reviewers say KEEP: the case is an approved metric example, so an excluded flag is wrong
    both_sheets(rd, [good("a"), good("b")])
    report = one(ds, rd)
    assert report["marked_excluded_without_agreement"] == ["b"] and report["counts"]["approved"] == 2
    assert report["readiness_counts"]["confirmed_excluded"] == 0 and report["ready_for_validated_metrics"] is False


def test_reviewers_who_exclude_a_case_marked_reviewed_in_the_dataset_is_inconsistent(env):
    ds, rd = env
    write_dataset(ds, [flagged("a", "reviewed")])
    both_sheets(rd, [good("a", **EXCLUDE)])
    report = one(ds, rd)
    assert report["counts"]["excluded"] == 1 and report["counts"]["approved"] == 0   # never silently approved
    assert report["marked_reviewed_without_sheet_support"] == ["a"]
    assert report["ready_for_validated_metrics"] is False and report["review_state"] == "evidence_inconsistent"


def test_an_ordinary_unreviewed_case_blocks_readiness(env):
    ds, rd = env
    write_dataset(ds, [flagged("a", "reviewed"), rag("b")])
    both_sheets(rd, [good("a")])
    report = one(ds, rd)
    assert report["ready_for_validated_metrics"] is False and report["review_state"] == "in_progress"
    assert any("not marked reviewed or excluded" in b for b in report["blockers"])


def test_a_disputed_case_blocks_readiness_until_resolved(env):
    ds, rd = env
    write_dataset(ds, [flagged("a", "reviewed"), flagged("b", "reviewed")])
    sheet(rd / "sheet_r1.csv", [good("a"), good("b")])
    sheet(rd / "sheet_r2.csv", [good("a"), good("b", answerable="no", sections="", behavior="decline_or_abstain")])
    report = one(ds, rd)
    assert report["counts"]["disputed"] == 1 and report["marked_reviewed_without_sheet_support"] == ["b"]
    assert report["review_state"] == "evidence_inconsistent" and report["ready_for_validated_metrics"] is False
    write_resolutions(rd, resolution_row("b"))
    report = one(ds, rd)
    assert report["counts"]["resolved"] == 1 and report["ready_for_validated_metrics"] is True


def test_keep_versus_exclude_is_a_disagreement_but_exclude_versus_ambiguous_is_an_exclusion(env):
    ds, rd = env
    write_dataset(ds, [rag("a"), rag("b")])
    sheet(rd / "sheet_r1.csv", [good("a", **EXCLUDE), good("b", exclude="ambiguous", comment="two readings")])
    sheet(rd / "sheet_r2.csv", [good("a"), good("b", **EXCLUDE)])
    got = counts(ds, rd)
    assert got["disputed"] == 1 and got["excluded"] == 1 and got["approved"] == 0


def test_a_dispute_can_be_resolved_to_an_exclusion_only_with_a_recorded_decision(env):
    ds, rd = env
    write_dataset(ds, [rag("a")])
    sheet(rd / "sheet_r1.csv", [good("a")])
    sheet(rd / "sheet_r2.csv", [good("a", answerable="no", sections="", behavior="decline_or_abstain")])
    write_resolutions(rd, resolution_row("a", resolution="out of scope for the benchmark", final_ambiguous_or_exclude="exclude"))
    assert counts(ds, rd)["excluded"] == 1
    write_resolutions(rd, resolution_row("a", resolution="out of scope", final_ambiguous_or_exclude="maybe"))
    report = rs.build_report([ds], rd)
    assert report["datasets"][0]["counts"]["disputed"] == 1          # an invalid decision resolves nothing
    assert any("final_ambiguous_or_exclude" in p["issue"] for p in report["problems"])


def test_a_dataset_with_only_excluded_cases_is_never_ready(env):
    ds, rd = env
    write_dataset(ds, [flagged("a", "excluded"), flagged("b", "excluded")])
    both_sheets(rd, [good("a", **EXCLUDE), good("b", **EXCLUDE)])
    report = one(ds, rd)
    assert report["readiness_counts"]["eligible_metric_cases"] == 0 and report["readiness_counts"]["confirmed_excluded"] == 2
    assert report["ready_for_validated_metrics"] is False and report["review_state"] == "no_eligible_cases"
    assert any("no eligible metric cases" in b for b in report["blockers"])


def test_mixed_approved_resolved_and_excluded_cases_use_only_eligible_cases_as_the_denominator(env):
    ds, rd = env
    write_dataset(ds, [flagged("a", "reviewed"), flagged("b", "reviewed"), flagged("c", "excluded")])
    sheet(rd / "sheet_r1.csv", [good("a"), good("b"), good("c", **EXCLUDE)])
    sheet(rd / "sheet_r2.csv", [good("a"), good("b", answerable="no", sections="", behavior="decline_or_abstain"),
                                good("c", **EXCLUDE)])
    write_resolutions(rd, resolution_row("b"))
    report = one(ds, rd)
    rc = report["readiness_counts"]
    assert (rc["total_cases"], rc["approved_or_resolved"], rc["eligible_metric_cases"], rc["confirmed_excluded"]) == (3, 2, 2, 1)
    assert report["ready_for_validated_metrics"] is True


def test_settled_cases_not_yet_recorded_in_the_dataset_leave_the_state_awaiting_update(env):
    ds, rd = env
    write_dataset(ds, [rag("a"), rag("b")])
    both_sheets(rd, [good("a"), good("b", **EXCLUDE)])
    report = one(ds, rd)
    assert report["review_state"] == "awaiting_dataset_update" and report["awaiting_dataset_update"] == ["a", "b"]
    assert report["ready_for_validated_metrics"] is False


def test_legacy_records_and_the_real_datasets_still_load_and_are_not_reinterpreted():
    for name in ("evals/datasets/nimbuscrm_golden_v1.jsonl", "evals/datasets/nimbuscrm_candidates_v2.jsonl"):
        records = rs.load_dataset(Path(name))
        assert records and all(r.get("review_status", "unreviewed") == "unreviewed" for r in records)
    legacy = {"id": "old", "kind": "rag_answer", "category": "factual", "messages": [{"role": "user", "content": "q"}],
              "expected_sources": ["S"]}                                  # no review_status key at all
    assert parse_case(legacy).review_status == "unreviewed"


def test_an_excluded_case_never_runs_so_it_never_enters_a_metric(env):
    excluded = parse_case({**rag("x", review_status="excluded", reviewer="r1; r2")})
    assert runner.skip_reason(excluded, "retrieval") == runner.EXCLUDED_REASON
    assert runner.skip_reason(excluded, "full") == runner.EXCLUDED_REASON
    gate = parse_case({"id": "g", "kind": "lead_gate", "category": "lead_gate", "messages": [{"role": "user", "content": "q"}],
                       "proposed_tool_call": {"arguments": {}}, "expected_gate": "rejected",
                       "review_status": "excluded", "reviewer": "r1; r2"})
    assert runner.skip_reason(gate, "retrieval") == runner.EXCLUDED_REASON   # even pure-code cases are removed
    ordinary = parse_case(rag("y"))
    assert runner.skip_reason(ordinary, "retrieval") is None


# ---- Workstream B: status model ---------------------------------------------------------------

def test_zero_reviewed_cases_reads_as_not_started(env):
    ds, rd = env
    write_dataset(ds, [rag("a"), rag("b")])
    sheet(rd / "sheet_r1.csv", [["a", "q", "", "", "", "", "", ""]])      # a blank sheet is not evidence
    report = one(ds, rd)
    assert report["review_state"] == "not_started" and report["ready_for_validated_metrics"] is False
    assert "provisional" in report["explanation"]


def test_one_reviewed_case_out_of_many_is_in_progress_and_the_warning_stays(env):
    ds, rd = env
    write_dataset(ds, [flagged("a", "reviewed"), rag("b"), rag("c"), rag("d")])
    both_sheets(rd, [good("a")])
    report = one(ds, rd)
    assert report["review_state"] == "in_progress"
    summary = rs.dataset_review_summary(ds, rd)
    assert summary["ready_for_validated_metrics"] is False and "Provisional" in summary["message"]
    assert summary["eligible_metric_cases"] == 1 and summary["total_cases"] == 4
    # the same banner is what a saved run shows, even though one case IS marked reviewed in the file
    meta = {"reviewed_cases": 1, "review": summary}
    assert rs.run_banner(meta) and "in_progress" in rs.run_banner(meta)


def test_all_cases_marked_reviewed_without_matching_evidence_is_not_ready(env):
    ds, rd = env
    write_dataset(ds, [flagged("a", "reviewed"), flagged("b", "reviewed")])
    sheet(rd / "sheet_r1.csv", [good("a"), good("b")])                   # one reviewer only
    report = one(ds, rd)
    assert report["review_state"] == "evidence_inconsistent" and report["ready_for_validated_metrics"] is False
    assert report["marked_reviewed_without_sheet_support"] == ["a", "b"]


def test_a_reviewed_flag_alone_never_starts_or_completes_a_review(env):
    ds, rd = env
    write_dataset(ds, [flagged("a", "reviewed")])
    assert one(ds, rd)["review_state"] == "evidence_inconsistent"
    assert one(ds, rd)["ready_for_validated_metrics"] is False


def test_sheets_with_an_unresolved_disagreement_are_unresolved_not_ready(env):
    ds, rd = env
    write_dataset(ds, [rag("a"), rag("b")])
    sheet(rd / "sheet_r1.csv", [good("a"), good("b")])
    sheet(rd / "sheet_r2.csv", [good("a"), good("b", answerable="no", sections="", behavior="decline_or_abstain")])
    report = one(ds, rd)
    assert report["review_state"] == "unresolved" and report["ready_for_validated_metrics"] is False


def test_state_walks_from_not_started_to_ready_only_when_every_condition_is_met(env):
    ds, rd = env
    write_dataset(ds, [rag("a"), rag("b")])
    assert one(ds, rd)["review_state"] == "not_started"
    sheet(rd / "sheet_r1.csv", [good("a")])
    assert one(ds, rd)["review_state"] == "in_progress"
    both_sheets(rd, [good("a"), good("b")])
    assert one(ds, rd)["review_state"] == "awaiting_dataset_update"
    write_dataset(ds, [flagged("a", "reviewed"), rag("b")])
    assert one(ds, rd)["ready_for_validated_metrics"] is False
    write_dataset(ds, [flagged("a", "reviewed"), flagged("b", "reviewed")])
    assert one(ds, rd)["review_state"] == "ready" and one(ds, rd)["ready_for_validated_metrics"] is True


def test_status_runs_are_repeatable_read_only_and_counts_match_the_records(env):
    ds, rd = env
    write_dataset(ds, [flagged("a", "reviewed"), flagged("b", "excluded"), rag("c"), rag("d")])
    sheet(rd / "sheet_r1.csv", [good("a"), good("b", **EXCLUDE), good("c")])
    sheet(rd / "sheet_r2.csv", [good("a"), good("b", **EXCLUDE)])
    before = snapshot(ds.parent)
    first = rs.build_report([ds], rd)
    second = rs.build_report([ds], rd)
    assert first == second and snapshot(ds.parent) == before             # deterministic, nothing written
    d = first["datasets"][0]
    assert sum(d["counts"].values()) == d["cases"] == d["readiness_counts"]["total_cases"] == 4
    rc = d["readiness_counts"]
    assert (d["counts"]["approved"], d["counts"]["excluded"], d["counts"]["single_review"], d["counts"]["unreviewed"]) == (1, 1, 1, 1)
    assert rc["awaiting_second_reviewer"] == 1 and rc["eligible_metric_cases"] == 1


def test_the_cli_text_summary_json_and_exit_codes(env, capsys):
    ds, rd = env
    write_dataset(ds, [flagged("a", "reviewed")])
    both_sheets(rd, [good("a")])
    before = snapshot(ds.parent)
    assert rs.main(["--dataset", str(ds), "--review-dir", str(rd), "--text", "--require-ready"]) == 0
    out = capsys.readouterr().out
    assert "state: ready" in out and "ready_for_validated_metrics: true" in out
    assert snapshot(ds.parent) == before
    (rd / "sheet_r2.csv").unlink()
    before = snapshot(ds.parent)
    assert rs.main(["--dataset", str(ds), "--review-dir", str(rd), "--require-ready"]) == 1
    assert json.loads(capsys.readouterr().out)["datasets"][0]["ready_for_validated_metrics"] is False
    assert rs.main(["--dataset", str(ds), "--review-dir", str(rd)]) == 0      # without --require-ready the exit code is 0
    capsys.readouterr()
    assert snapshot(ds.parent) == before                                      # the CLI wrote nothing


def test_registry_and_report_keep_partly_reviewed_runs_provisional(env):
    from evals import registry
    ds, rd = env
    write_dataset(ds, [flagged("a", "reviewed"), rag("b")])
    both_sheets(rd, [good("a")])
    summary = rs.dataset_review_summary(ds, rd)
    dataset_meta = {"name": "t", "sha256": "abc", "n_cases": 2, "reviewed_cases": 1, "review": summary}
    run = {"metrics": {"retrieval.mrr": {"value": 0.5, "n": 4}}, "dataset": dataset_meta}
    row = {r["name"]: r for r in registry.resolve_metrics(run)}["retrieval.mrr"]
    assert row["status"] == "provisional"
    text = render_summary({"run_id": "r", "status": "completed", "mode": "retrieval", "dataset": dataset_meta,
                           "metrics": {}, "counts": {}}, [])
    assert "Provisional" in text and "in_progress" in text


def test_saved_runs_without_a_review_block_are_never_called_validated():
    legacy_zero = {"reviewed_cases": 0}
    legacy_some = {"reviewed_cases": 3}
    assert "smoke-test" in rs.run_banner(legacy_zero)
    assert "did not record reviewer evidence" in rs.run_banner(legacy_some)
    ready = {"reviewed_cases": 2, "review": {"ready_for_validated_metrics": True, "state": "ready"}}
    assert rs.run_banner(ready) is None


def test_the_dashboard_shows_the_stored_review_state_not_only_a_zero_count():
    html = Path("api/static/eval_dashboard.html").read_text(encoding="utf-8")
    assert "ds.review.ready_for_validated_metrics" in html and "older run" in html
    assert "Review state (when this run was made)" in html


# ---- Workstream D: end-to-end audit ------------------------------------------------------------

def test_one_person_in_two_files_never_satisfies_the_two_reviewer_rule(env):
    ds, rd = env
    write_dataset(ds, [rag("a")])
    sheet(rd / "sheet_asha-candidates.csv", [good("a")])
    sheet(rd / "sheet_asha.csv", [good("a")])                              # same person, second file
    report = rs.build_report([ds], rd)
    assert report["datasets"][0]["counts"]["single_review"] == 1 and report["datasets"][0]["counts"]["approved"] == 0
    assert any("already reviewed this case" in p["issue"] for p in report["problems"])


def test_one_person_may_hold_a_golden_and_a_candidates_sheet(env):
    assert rs.reviewer_identity("asha-golden") == rs.reviewer_identity("asha-candidates") == "asha"
    assert rs.sheet_hint("asha-golden") == "golden" and rs.sheet_hint("asha") is None


def test_a_sheet_named_for_one_dataset_is_ignored_by_the_other_and_reported(tmp_path):
    golden, candidates = tmp_path / "nimbuscrm_golden_v1.jsonl", tmp_path / "nimbuscrm_candidates_v2.jsonl"
    write_dataset(golden, [rag("g1")])
    write_dataset(candidates, [rag("c1")])
    rd = tmp_path / "review"
    rd.mkdir()
    # a golden-named sheet that wrongly contains a candidates case, and a candidates case reviewed twice in golden files
    sheet(rd / "sheet_r1-golden.csv", [good("g1"), good("c1")])
    sheet(rd / "sheet_r2-golden.csv", [good("g1"), good("c1")])
    report = rs.build_report([golden, candidates], rd)
    by_name = {d["dataset"]: d for d in report["datasets"]}
    assert by_name[golden.name]["counts"]["approved"] == 1
    assert by_name[candidates.name]["counts"]["unreviewed"] == 1           # golden-named sheets never count here
    assert any("named for the golden dataset" in p["issue"] and p["case_id"] == "c1" for p in report["problems"])


def test_duplicate_dataset_ids_and_unknown_flags_block_readiness(env):
    ds, rd = env
    write_dataset(ds, [flagged("a", "reviewed"), flagged("a", "reviewed")])
    both_sheets(rd, [good("a")])
    report = one(ds, rd)
    assert report["ready_for_validated_metrics"] is False and any("duplicate case ids" in b for b in report["blockers"])
    write_dataset(ds, [rag("a", review_status="approved")])
    assert any("unknown review_status" in b for b in one(ds, rd)["blockers"])


def test_bad_inputs_give_actionable_errors_and_create_nothing(tmp_path, capsys):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"id": "a"}\nnot json\n', encoding="utf-8")
    rd = tmp_path / "missing_review_dir"
    assert rs.main(["--dataset", str(bad), "--review-dir", str(rd)]) == 2
    assert "line 2" in capsys.readouterr().err and not rd.exists()
    assert rs.main(["--dataset", str(tmp_path / "nope.jsonl")]) == 2
    assert "dataset not found" in capsys.readouterr().err
    good_ds = tmp_path / "d.jsonl"
    write_dataset(good_ds, [rag("a")])
    report = rs.build_report([good_ds], rd)
    assert any("review directory not found" in w for w in report["warnings"]) and not rd.exists()


def test_fingerprint_is_stable_and_detects_a_changed_dataset(env):
    ds, rd = env
    write_dataset(ds, [rag("a")])
    first = rs.fingerprint(ds)
    assert first == rs.fingerprint(ds) == one(ds, rd)["fingerprint"]
    write_dataset(ds, [rag("a"), rag("b")])
    assert rs.fingerprint(ds) != first


def test_disagreement_template_carries_the_final_decision_column_and_stays_new_file_only(env):
    ds, rd = env
    write_dataset(ds, [rag("a")])
    sheet(rd / "sheet_r1.csv", [good("a")])
    sheet(rd / "sheet_r2.csv", [good("a", answerable="no", sections="", behavior="decline_or_abstain")])
    target = rd / "disagreements_template.csv"
    rs.write_disagreements([ds], rd, target)
    assert "final_ambiguous_or_exclude" in target.read_text(encoding="utf-8-sig").splitlines()[0]
    assert rs.load_resolutions(rd) == {}                                    # a blank template resolves nothing


def test_real_blank_templates_are_intact_and_hide_all_labels():
    for dataset, blank in (("nimbuscrm_golden_v1.jsonl", "golden_review_sheet_blank.csv"),
                           ("nimbuscrm_candidates_v2.jsonl", "candidate_review_sheet_blank.csv")):
        ids = [c["id"] for c in rs.load_dataset(Path("evals/datasets") / dataset)]
        with open(Path("evals/review") / blank, newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        assert [r["case_id"] for r in rows] == ids
        label_columns = [c for c in rows[0] if c.split(" ")[0] in rs.LABELS or c == "comment"]
        assert label_columns and all(not (r[c] or "").strip() for r in rows for c in label_columns)
        assert not any("expected" in c.lower() and "(" not in c for c in rows[0])


# ---------------------------------------------------------------------------------------------
# Review-preparation milestone: sheet headers, resolutions, and the facts / correctness boundary
# ---------------------------------------------------------------------------------------------

def test_header_problem_names_missing_columns_and_the_delimiter_mistake():
    assert rs.header_problem(HEADER) is None
    assert "missing column(s): expected_behavior" in rs.header_problem([h for h in HEADER if "expected_behavior" not in h])
    semicolons = rs.header_problem([";".join(HEADER)])
    assert "CSV UTF-8" in semicolons and "case_id" in semicolons
    assert rs.header_problem(None).startswith("missing column(s)")


def test_a_sheet_with_the_wrong_delimiter_is_a_problem_and_counts_for_nothing(env):
    ds, rd = env
    write_dataset(ds, [rag("a")])
    (rd / "sheet_r1.csv").write_text(";".join(HEADER) + "\na;q;yes;S1;answer;;keep;\n", encoding="utf-8")
    sheet(rd / "sheet_r2.csv", [good("a")])
    report = rs.build_report([ds], rd)
    assert report["reviewer_sheets"] == ["r2"] and report["datasets"][0]["counts"]["single_review"] == 1
    assert any(p["sheet"] == "r1" and "sheet not used" in p["issue"] for p in report["problems"])


def test_a_half_filled_resolution_is_reported_and_the_case_stays_disputed(env):
    ds, rd = env
    write_dataset(ds, [rag("a")])
    sheet(rd / "sheet_r1.csv", [good("a")])
    sheet(rd / "sheet_r2.csv", [good("a", answerable="no", sections="", behavior="decline_or_abstain")])
    (rd / "disagreements.csv").write_text("case_id,resolution,resolved_by\na,answerable,\n", encoding="utf-8")
    report = rs.build_report([ds], rd)
    assert report["datasets"][0]["counts"]["disputed"] == 1
    assert any(p["case_id"] == "a" and "must both be filled" in p["issue"] for p in report["problems"])
    # an untouched template row (both blank) is just pending, not a problem
    (rd / "disagreements.csv").write_text("case_id,resolution,resolved_by\na,,\n", encoding="utf-8")
    assert not any(p["sheet"] == "disagreements" for p in rs.build_report([ds], rd)["problems"])


def test_a_resolution_for_an_unknown_case_is_reported(env):
    ds, rd = env
    write_dataset(ds, [rag("a")])
    (rd / "disagreements.csv").write_text("case_id,resolution,resolved_by\nghost,x,carol\n", encoding="utf-8")
    assert any(p["case_id"] == "ghost" for p in rs.build_report([ds], rd)["problems"])


def facts_case(i, **kw):
    return flagged(i, "reviewed", required_facts=["29"], **kw)


def test_facts_are_never_validated_by_the_sheets_even_when_the_dataset_is_ready(env):
    ds, rd = env
    write_dataset(ds, [facts_case("a"), flagged("b", "reviewed", forbidden_facts=["x"]), flagged("c", "excluded", required_facts=["9"]),
                       flagged("d", "reviewed")])
    both_sheets(rd, [good("a"), good("b"), good("c", **EXCLUDE), good("d")])
    report = one(ds, rd)
    assert report["ready_for_validated_metrics"] is True
    facts = report["facts_review"]
    assert facts["cases_with_facts"] == 2 and facts["marked_reviewed"] == 0   # the excluded case is not counted
    assert facts["validated"] is False and "author-written" in facts["reason"]
    assert rs.dataset_review_summary(ds, rd)["facts_validated"] is False


def test_a_facts_reviewed_flag_is_counted_as_an_attestation_but_does_not_validate(env):
    ds, rd = env
    write_dataset(ds, [facts_case("a", facts_review_status="reviewed", facts_reviewer="r1")])
    both_sheets(rd, [good("a")])
    facts = one(ds, rd)["facts_review"]
    assert facts["marked_reviewed"] == 1 and facts["validated"] is False


def test_real_datasets_have_unreviewed_facts_and_no_validation():
    for name in ("nimbuscrm_golden_v1.jsonl", "nimbuscrm_candidates_v2.jsonl"):
        path = Path("evals/datasets") / name
        cases = rs.load_dataset(path)
        report = rs.build_report([path], Path("evals/review"))["datasets"][0]
        expected = sum(1 for c in cases if any(c.get(k) for k in rs.FACT_FIELDS))
        assert report["facts_review"]["cases_with_facts"] == expected > 0
        assert report["facts_review"]["marked_reviewed"] == 0 and report["facts_review"]["validated"] is False
        assert not any(c.get("facts_review_status") for c in cases)


def test_a_ready_dataset_still_leaves_fact_and_judge_metrics_provisional(env):
    from evals import registry
    ds, rd = env
    write_dataset(ds, [facts_case("a"), flagged("b", "reviewed")])
    both_sheets(rd, [good("a"), good("b")])
    summary = rs.dataset_review_summary(ds, rd)
    assert summary["ready_for_validated_metrics"] is True
    metrics = {name: {"value": 0.8, "n": 5} for name in (
        "retrieval.mrr", "lead.gate_correct", "answer.required_facts", "answer.forbidden_clear", "run.pass_rate",
        "judge.correctness", "eval.correctness", "eval.faithfulness", "eval.pii_leak", "run.error_rate")}
    rows = {r["name"]: r for r in registry.resolve_metrics({"metrics": metrics, "dataset": {"review": summary}})}
    assert rows["retrieval.mrr"]["status"] == "measured" and rows["lead.gate_correct"]["status"] == "measured"
    assert rows["eval.pii_leak"]["status"] == "measured" and rows["run.error_rate"]["status"] == "measured"
    for name in ("answer.required_facts", "answer.forbidden_clear", "run.pass_rate", "judge.correctness",
                 "eval.correctness"):
        assert rows[name]["status"] == "provisional" and "author wrote" in rows[name]["why_provisional"]
    for name in ("judge.correctness", "eval.correctness", "eval.faithfulness"):
        assert "human reviewers" in rows[name]["why_provisional"]
