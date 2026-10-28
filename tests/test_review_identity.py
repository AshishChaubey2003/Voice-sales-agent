"""Reviewer identity registry and dispute validation. Offline; synthetic fixtures only.

Nothing here is a real review: every sheet, registry and resolution is generated in tmp_path to exercise the
checks. The real evals/review folder is only read (never written) by the last tests."""

import csv
import hashlib
import json
import os
from pathlib import Path

import pytest

from evals import review_identity as ri, review_preflight as pf, review_sheets, review_status as rs

REPO = Path(__file__).resolve().parent.parent
HEADER = ["case_id", "question", "answerable_from_kb (yes/no/partly)", "evidence_sections", "expected_behavior",
          "should_save (yes/no)", "ambiguous_or_exclude (keep/ambiguous/exclude)", "comment"]
AGREE = ["yes", "Doc > Alpha", "answer", "", "keep", ""]
DIFFER = ["no", "", "decline_or_abstain", "", "keep", ""]
RES_COLUMNS = ["case_id", "resolution", "resolved_by", "final_ambiguous_or_exclude", "resolution_status",
               "resolution_evidence", "final_answerable_from_kb", "final_evidence_sections",
               "final_expected_behavior", "final_should_save"]
GOOD_REGISTRY = [("ann", "Ann Rao", "reviewer", "ann-golden", "review lead"),
                 ("bob", "Bob Iyer", "reviewer", "bob-golden", "review lead"),
                 ("carol", "Carol Shah", "resolver", "", "review lead")]


def case(i, status="reviewed", **kw):
    row = {"id": i, "kind": "rag_answer", "category": "factual", "messages": [{"role": "user", "content": f"question {i}"}],
           "expected_sources": ["Doc > Alpha"], "review_status": status}
    if status != "unreviewed":
        row["reviewer"] = "ann; bob"
    return {**row, **kw}


def write_registry(rd, rows, header=None, delimiter=","):
    with open(rd / "reviewers.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter=delimiter)
        writer.writerow(header or ri.COLUMNS)
        writer.writerows(rows)


def write_sheet(path, first_case_row, n):
    """Every row filled; case c0 carries `first_case_row`, the others agree."""
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        for index in range(n):
            writer.writerow([f"c{index}", "q", *(first_case_row if index == 0 else AGREE)])


def resolution_row(case_id="c0", **kw):
    row = {"case_id": case_id, "resolution": "Alpha states the fact", "resolved_by": "carol", "final_ambiguous_or_exclude": "",
           "resolution_status": "resolved", "resolution_evidence": "Doc > Alpha: the sentence that settles it",
           "final_answerable_from_kb": "yes", "final_evidence_sections": "Doc > Alpha", "final_expected_behavior": "answer",
           "final_should_save": ""}
    row.update(kw)
    return row


def write_resolutions(rd, *rows):
    with open(rd / "disagreements.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RES_COLUMNS)
        writer.writeheader()
        writer.writerows({c: r.get(c, "") for c in RES_COLUMNS} for r in rows)


class World:
    def __init__(self, tmp_path, n=3, status="reviewed", disputed=True, registry=GOOD_REGISTRY, sheets=("ann", "bob")):
        self.ds = tmp_path / "x_golden_v1.jsonl"
        self.ds.write_text("\n".join(json.dumps(case(f"c{i}", status)) for i in range(n)), encoding="utf-8")
        self.rd = tmp_path / "review"
        self.rd.mkdir()
        review_sheets.write_blank(self.ds, self.rd / "golden_review_sheet_blank.csv")
        self.kd = tmp_path / "knowledge"
        self.kd.mkdir()
        (self.kd / "doc.md").write_text("# Doc\n\n## Alpha\ntext\n\n## Beta\ntext\n", encoding="utf-8")
        for name in sheets:
            first = DIFFER if (disputed and name == "bob") else AGREE
            write_sheet(self.rd / f"sheet_{name}-golden.csv", first, n)
        if registry is not None:
            write_registry(self.rd, registry)

    def status(self):
        return rs.build_report([self.ds], self.rd)

    def dataset(self):
        return self.status()["datasets"][0]

    def preflight(self):
        return pf.run_preflight([self.ds], self.rd, self.kd)

    def problems(self):
        return [p["issue"] for p in self.status()["problems"]]


def found(report, severity=None):
    return {f["check"] for f in report["findings"] if severity in (None, f["severity"])}


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


# ---------------------------------------------------------------- reviewer identity

def test_one_reviewer_submitting_two_sheets_under_different_filenames_counts_once(tmp_path):
    registry = [("ann", "Ann Rao", "reviewer", "ann-golden; annie-golden", "review lead"),
                ("carol", "Carol Shah", "resolver", "", "review lead")]
    w = World(tmp_path, disputed=False, sheets=("ann", "annie"), registry=registry)
    assert [w.dataset()["counts"][k] for k in ("approved", "single_review")] == [0, 3]   # never two reviewers
    assert w.dataset()["identity"]["same_reviewer_twice"] == {"ann": ["ann-golden", "annie-golden"]}
    assert w.dataset()["identity"]["established"] is False
    pre = w.preflight()
    assert "same_reviewer_twice" in found(pre, "error")
    assert any("already reviewed this case" in p for p in w.problems())


def test_two_distinct_declared_reviewers_establish_identity_and_can_make_the_dataset_ready(w):
    w.disputed = False
    write_sheet(w.rd / "sheet_bob-golden.csv", AGREE, 3)
    data = w.dataset()
    assert data["identity"] == {"source": "registry", "established": True, "reviewers": ["ann", "bob"],
                                "unregistered_sheets": [], "same_reviewer_twice": {}, "reasons": []}
    assert data["review_state"] == "ready" and data["ready_for_validated_metrics"] is True
    assert not [f for f in w.preflight()["findings"] if f["severity"] == "error"]


def test_a_blank_reviewer_id_is_reported(w):
    write_registry(w.rd, [*GOOD_REGISTRY, ("", "Nobody", "reviewer", "", "review lead")])
    assert "identity_missing" in found(w.preflight(), "error")


@pytest.mark.parametrize("bad", ["Ann Rao!", "a", "UPPER space", "x" * 41, "-ann"])
def test_a_malformed_reviewer_id_is_rejected(w, bad):
    write_registry(w.rd, [(bad, "Dee", "reviewer", "", "review lead"), *GOOD_REGISTRY])
    assert "identity_malformed" in found(w.preflight(), "error")
    assert bad not in ri.load_registry(w.rd)["entries"]


def test_incomplete_identity_rows_are_rejected(w):
    write_registry(w.rd, [("dee", "", "reviewer", "", "review lead"), ("eve", "Eve", "tester", "", "review lead"),
                          ("fay", "Fay", "reviewer", "", ""), *GOOD_REGISTRY])
    messages = [p["message"] for p in ri.load_registry(w.rd)["problems"] if p["code"] == "identity_incomplete"]
    assert len(messages) == 3 and "display_name" in messages[0] and "role" in messages[1] and "attested_by" in messages[2]


def test_conflicting_declarations_for_one_identity_are_not_merged(w):
    write_registry(w.rd, [*GOOD_REGISTRY, ("ann", "Someone Else", "reviewer", "other-golden", "review lead")])
    registry = ri.load_registry(w.rd)
    assert "identity_duplicate" in {p["code"] for p in registry["problems"]}
    assert "ann" not in registry["entries"]                                         # neither declaration is trusted
    assert "sheet_unregistered" in found(w.preflight(), "error")                    # so ann's sheet counts for nothing
    assert w.dataset()["counts"]["approved"] == 0


def test_a_sheet_claimed_by_two_identities_is_a_conflict(w):
    write_registry(w.rd, [*GOOD_REGISTRY, ("dee", "Dee Nair", "reviewer", "ann-golden", "review lead")])
    assert "sheet_claimed_twice" in {p["code"] for p in ri.load_registry(w.rd)["problems"]}
    assert "ann-golden" not in ri.load_registry(w.rd)["sheet_owner"]
    assert w.dataset()["identity"]["established"] is False


def test_the_file_name_never_decides_identity_once_a_registry_exists(tmp_path):
    # The names look unrelated, the registry says they are one person: the registry wins.
    registry = [("ann", "Ann Rao", "reviewer", "zeta-golden; omega-golden", "review lead"),
                ("carol", "Carol Shah", "resolver", "", "review lead")]
    w = World(tmp_path, disputed=False, sheets=("zeta", "omega"), registry=registry)
    assert w.dataset()["counts"]["approved"] == 0 and w.dataset()["identity"]["reviewers"] == ["ann"]
    # ...and the reverse: the file name 'ann-golden' proves nothing about who wrote it.
    registry = [("ann", "Ann Rao", "reviewer", "bob-golden", "review lead"), ("bob", "Bob Iyer", "reviewer", "ann-golden", "review lead")]
    (tmp_path / "second").mkdir()
    swapped = World(tmp_path / "second", disputed=False, registry=registry)
    assert swapped.dataset()["identity"]["reviewers"] == ["ann", "bob"]


def test_display_names_are_compared_exactly_never_fuzzily(w):
    write_registry(w.rd, [("ann", "Ann  Rao", "reviewer", "ann-golden", "review lead"),
                          ("ann2", "ann rao", "reviewer", "bob-golden", "review lead")])
    assert "identity_ambiguous" in {p["code"] for p in ri.load_registry(w.rd)["problems"]}
    assert w.dataset()["identity"]["established"] is False
    write_registry(w.rd, [("ann", "Ann Rao", "reviewer", "ann-golden", "review lead"),
                          ("bob", "Ann Rau", "reviewer", "bob-golden", "review lead")])      # similar is not the same
    assert "identity_ambiguous" not in {p["code"] for p in ri.load_registry(w.rd)["problems"]}


def test_reviewer_independence_is_violated_by_a_resolver_who_also_holds_a_sheet(w):
    write_registry(w.rd, [("ann", "Ann Rao", "reviewer", "ann-golden", "review lead"),
                          ("bob", "Bob Iyer", "resolver", "bob-golden", "review lead")])
    assert "resolver_has_sheets" in found(w.preflight(), "error")
    assert w.dataset()["identity"]["established"] is False and w.dataset()["ready_for_validated_metrics"] is False


def test_identity_that_cannot_be_established_withholds_readiness(w):
    w.disputed = False
    write_sheet(w.rd / "sheet_bob-golden.csv", AGREE, 3)
    (w.rd / "reviewers.csv").unlink()                     # complete, matching sheets and flags, but nobody declared
    data = w.dataset()
    assert data["counts"]["approved"] == 3
    assert data["review_state"] == "identity_unverified" and data["ready_for_validated_metrics"] is False
    assert any("file names" in b for b in data["blockers"])
    assert "identity_unestablished" in found(w.preflight(), "pending")
    assert rs.dataset_review_summary(w.ds, w.rd)["identity_established"] is False


def test_an_undeclared_sheet_counts_for_nothing_and_is_an_error(w):
    write_registry(w.rd, [GOOD_REGISTRY[0], GOOD_REGISTRY[2]])            # bob never declared
    data = w.dataset()
    assert data["identity"]["unregistered_sheets"] == ["bob-golden"] and data["counts"]["single_review"] == 3
    assert "sheet_unregistered" in found(w.preflight(), "error")
    assert any("not listed in reviewers.csv" in p for p in w.problems())


def test_a_registry_with_the_wrong_delimiter_or_missing_columns_is_reported(w):
    write_registry(w.rd, [("ann;Ann;reviewer;ann-golden;lead",)], header=["reviewer_id;display_name;role;sheets;attested_by"])
    problem = ri.load_registry(w.rd)["problems"][0]
    assert problem["code"] == "registry_header" and "CSV UTF-8" in problem["message"]
    write_registry(w.rd, [("ann", "Ann")], header=["reviewer_id", "display_name"])
    assert "role" in ri.load_registry(w.rd)["problems"][0]["message"]
    assert w.dataset()["identity"]["established"] is False


def test_legacy_sheets_without_a_registry_still_parse_but_never_make_a_dataset_ready(w):
    (w.rd / "reviewers.csv").unlink()
    w.disputed = False
    write_sheet(w.rd / "sheet_bob-golden.csv", AGREE, 3)
    assert w.dataset()["counts"]["approved"] == 3 and w.dataset()["identity"]["source"] == "filename"
    assert w.dataset()["ready_for_validated_metrics"] is False


# ---------------------------------------------------------------- dispute resolution

def test_a_valid_documented_resolution_settles_the_dispute_and_the_dataset_is_ready(w):
    assert w.dataset()["counts"]["disputed"] == 1
    write_resolutions(w.rd, resolution_row())
    data = w.dataset()
    assert data["counts"]["resolved"] == 1 and data["review_state"] == "ready" and data["ready_for_validated_metrics"] is True
    assert not [f for f in w.preflight()["findings"] if f["severity"] == "error"]


def test_a_resolution_for_an_unknown_case_is_an_error_and_attaches_to_nothing(w):
    write_resolutions(w.rd, resolution_row("ghost"))
    assert "resolution_unknown_case" in found(w.preflight(), "error")
    assert "case id is not in any dataset" in " ".join(w.problems())
    assert w.dataset()["counts"]["disputed"] == 1


def test_an_invalid_final_decision_resolves_nothing(w):
    write_resolutions(w.rd, resolution_row(final_ambiguous_or_exclude="maybe"))
    assert w.dataset()["counts"]["disputed"] == 1
    assert any("final_ambiguous_or_exclude" in p for p in w.problems())
    assert "resolution_record" in found(w.preflight(), "error")


def test_a_resolved_dispute_without_a_resolver_stays_disputed(w):
    write_resolutions(w.rd, resolution_row(resolved_by=""))
    assert w.dataset()["counts"]["disputed"] == 1 and "resolution_incomplete" in found(w.preflight(), "error")
    write_resolutions(w.rd, resolution_row(resolution="", resolved_by=""))                 # status says resolved, nothing else
    assert w.dataset()["counts"]["disputed"] == 1 and "resolution_record" in found(w.preflight(), "error")


@pytest.mark.parametrize("by,code", [("ann", "resolver_wrong_role"), ("zed", "resolver_unregistered"),
                                     ("ann; carol", "resolver_unregistered")])
def test_a_resolver_who_is_a_reviewer_or_unknown_cannot_settle(w, by, code):
    write_resolutions(w.rd, resolution_row(resolved_by=by))
    assert w.dataset()["counts"]["disputed"] == 1
    assert code in found(w.preflight(), "error")


def test_the_resolution_check_independently_refuses_a_resolver_who_is_among_the_dataset_reviewers():
    # Defence in depth: the registry already refuses a resolver that holds a sheet, but the check must not rely on it.
    registry = {"present": True, "entries": {"carol": {"role": "resolver"}}}
    resolution = {"resolved_by": "carol", "evidence": "Doc > Alpha", "final": "exclude", "resolution": "x"}
    codes = [p["code"] for p in rs.resolution_problems("rag_answer", resolution, {"ann", "carol"}, registry)]
    assert codes == ["resolver_is_reviewer"]
    assert rs.resolution_problems("rag_answer", resolution, {"ann"}, registry) == []
    assert rs.resolution_problems("rag_answer", resolution, {"ann"}, None) == []        # no registry: nothing to check against


def test_a_reviewer_registered_in_both_roles_is_blocked_at_the_registry(w):
    write_registry(w.rd, [*GOOD_REGISTRY, ("ann", "Ann Rao", "resolver", "", "review lead")])
    assert "identity_duplicate" in {p["code"] for p in ri.load_registry(w.rd)["problems"]}


def test_a_resolution_without_evidence_is_incomplete(w):
    write_resolutions(w.rd, resolution_row(resolution_evidence=""))
    assert w.dataset()["counts"]["disputed"] == 1
    assert "resolution_evidence_missing" in found(w.preflight(), "error")


def test_final_labels_are_required_and_validated_unless_the_case_is_excluded(w):
    write_resolutions(w.rd, resolution_row(final_answerable_from_kb="", final_expected_behavior=""))
    assert w.dataset()["counts"]["disputed"] == 1 and "final_labels_invalid" in found(w.preflight(), "error")
    write_resolutions(w.rd, resolution_row(final_answerable_from_kb="maybe"))
    assert w.dataset()["counts"]["disputed"] == 1
    write_resolutions(w.rd, resolution_row(final_ambiguous_or_exclude="exclude", final_answerable_from_kb="",
                                           final_expected_behavior="", final_evidence_sections=""))
    assert w.dataset()["counts"]["excluded"] == 1


def test_an_unresolved_dispute_that_already_carries_a_final_decision_is_an_error(w):
    write_resolutions(w.rd, resolution_row(resolution_status="open"))
    assert w.dataset()["counts"]["disputed"] == 1
    assert any("already holds resolution fields" in p for p in w.problems())
    assert "resolution_record" in found(w.preflight(), "error")


def test_contradictory_and_duplicate_records_never_overwrite_each_other(w):
    write_resolutions(w.rd, resolution_row(resolution_status=""), resolution_row(resolution_status="", final_ambiguous_or_exclude="exclude"))
    assert w.dataset()["counts"]["disputed"] == 1 and any("contradictory" in p for p in w.problems())
    write_resolutions(w.rd, resolution_row(resolution_status=""), resolution_row(resolution_status=""))
    assert w.dataset()["counts"]["disputed"] == 1 and any("duplicate resolution record" in p for p in w.problems())
    assert "resolution_record" in found(w.preflight(), "error")


def test_a_second_resolution_without_reopening_is_an_invalid_transition(w):
    write_resolutions(w.rd, resolution_row(), resolution_row(final_ambiguous_or_exclude="exclude"))
    assert w.dataset()["counts"]["disputed"] == 1
    assert any("invalid state transition resolved -> resolved" in p for p in w.problems())
    write_resolutions(w.rd, resolution_row(resolution_status="reopened", resolution="x", resolved_by="carol"))
    assert any("invalid state transition start -> reopened" in p for p in w.problems())
    write_resolutions(w.rd, resolution_row(resolution_status="open", resolution="", resolved_by="", resolution_evidence="",
                                           final_answerable_from_kb="", final_evidence_sections="", final_expected_behavior=""),
                      resolution_row(resolution_status="open", resolution="", resolved_by="", resolution_evidence="",
                                     final_answerable_from_kb="", final_evidence_sections="", final_expected_behavior=""))
    assert any("invalid state transition open -> open" in p for p in w.problems())


def test_a_correction_follows_an_explicit_reopen_and_keeps_the_history(w):
    first = resolution_row(final_ambiguous_or_exclude="exclude", resolution="out of scope")
    reopened = resolution_row(resolution_status="reopened", resolution="reviewer 2 found the sentence", resolved_by="carol",
                              resolution_evidence="", final_answerable_from_kb="", final_evidence_sections="",
                              final_expected_behavior="")
    final = resolution_row(resolution="settled after reopening")
    write_resolutions(w.rd, first)
    assert w.dataset()["counts"]["excluded"] == 1
    write_resolutions(w.rd, first, reopened)
    assert w.dataset()["counts"]["disputed"] == 1                                  # reopened: back to disputed
    write_resolutions(w.rd, first, reopened, final)
    assert w.dataset()["counts"]["resolved"] == 1 and not w.problems()             # last valid record wins, history stays
    assert len(list(csv.DictReader(open(w.rd / "disagreements.csv", encoding="utf-8")))) == 3


def test_a_reopened_row_must_not_hold_a_decision(w):
    write_resolutions(w.rd, resolution_row(), resolution_row(resolution_status="reopened"))
    assert any("reopened dispute must not hold a final decision" in p for p in w.problems())


def test_a_resolution_that_contradicts_the_dataset_expectation_blocks_readiness(tmp_path):
    w = World(tmp_path)
    w.ds.write_text("\n".join(json.dumps(case(f"c{i}", expected_sources=["Doc > Beta"] if i == 0 else ["Doc > Alpha"]))
                              for i in range(3)), encoding="utf-8")
    write_resolutions(w.rd, resolution_row())                                      # adjudicator says Alpha, dataset says Beta
    data = w.dataset()
    assert data["counts"]["resolved"] == 1 and data["approved_but_dataset_differs"] == [{"case_id": "c0", "fields": ["expected_sources"]}]
    assert data["ready_for_validated_metrics"] is False and data["review_state"] == "evidence_inconsistent"


def test_a_resolution_to_exclude_conflicts_with_a_dataset_flag_of_reviewed(w):
    write_resolutions(w.rd, resolution_row(final_ambiguous_or_exclude="exclude"))
    data = w.dataset()
    assert data["counts"]["excluded"] == 1 and data["marked_reviewed_without_sheet_support"] == ["c0"]
    assert data["ready_for_validated_metrics"] is False


def test_a_resolution_for_a_case_the_reviewers_agreed_on_changes_nothing_and_is_flagged(w):
    write_resolutions(w.rd, resolution_row("c1"))
    assert w.dataset()["counts"]["approved"] == 2
    assert "resolution_not_needed" in found(w.preflight(), "warning")


def test_legacy_minimal_records_still_resolve_without_a_registry_but_never_make_a_dataset_ready(w):
    (w.rd / "reviewers.csv").unlink()
    (w.rd / "disagreements.csv").write_text("case_id,resolution,resolved_by\nc0,answerable,r3\n")
    data = w.dataset()
    assert data["counts"]["resolved"] == 1 and data["ready_for_validated_metrics"] is False
    write_registry(w.rd, GOOD_REGISTRY)                                            # the same record under a registry
    assert w.dataset()["counts"]["disputed"] == 1


def test_half_filled_and_partial_rows_and_a_malformed_disagreements_file_are_reported(w):
    (w.rd / "disagreements.csv").write_text("case_id,resolution,resolved_by\nc0,answerable,\n")
    assert w.dataset()["counts"]["disputed"] == 1 and "resolution and resolved_by must both be filled" in " ".join(w.problems())
    (w.rd / "disagreements.csv").write_text("case_id;resolution;resolved_by\nc0;x;carol\n")
    assert "disagreements_columns" in found(w.preflight(), "error")
    assert w.dataset()["counts"]["disputed"] == 1


def test_the_disagreements_template_carries_the_lifecycle_columns_and_starts_open(w, tmp_path):
    target = w.rd / "disagreements_template.csv"
    assert rs.write_disagreements([w.ds], w.rd, target) == 1
    rows = list(csv.DictReader(open(target, encoding="utf-8-sig")))
    assert rows[0]["resolution_status"] == "open" and rows[0]["resolution"] == "" and rows[0]["resolved_by"] == ""
    assert {"resolution_evidence", "final_answerable_from_kb", "final_should_save"} <= set(rows[0])
    with pytest.raises(FileExistsError):
        rs.write_disagreements([w.ds], w.rd, target)
    # the untouched template (resolution_status open, everything else blank) is a valid, still-disputed record
    (w.rd / "disagreements.csv").write_text(target.read_text(encoding="utf-8-sig"), encoding="utf-8")
    assert w.dataset()["counts"]["disputed"] == 1 and not any(p["sheet"] == "disagreements" for p in w.status()["problems"])


# ---------------------------------------------------------------- read-only, folder hygiene, real repository

def test_identity_and_dispute_checks_are_deterministic_and_read_only(w):
    write_resolutions(w.rd, resolution_row())
    watched = sorted(w.rd.iterdir()) + [w.ds]
    before = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in watched}
    names = sorted(os.listdir(w.rd))
    first = (w.status(), w.preflight())
    second = (w.status(), w.preflight())
    pf.main(["--dataset", str(w.ds), "--review-dir", str(w.rd), "--knowledge-dir", str(w.kd)])
    rs.main(["--dataset", str(w.ds), "--review-dir", str(w.rd), "--text"])
    assert first == second and names == sorted(os.listdir(w.rd))
    assert before == {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in watched}


def test_unexpected_files_in_the_review_folder_are_flagged_and_ignored(w):
    (w.rd / "notes.txt").write_text("scratch")
    (w.rd / "sheet_ann-golden.csv.bak").write_text("old")
    assert "unexpected_file" in found(w.preflight(), "warning")
    assert w.dataset()["counts"]["disputed"] == 1


def test_standalone_registry_cli_reports_absent_registry_as_exit_0(tmp_path, capsys):
    rd = tmp_path / "review"
    rd.mkdir()
    rc = ri.main(["--review-dir", str(rd)])
    assert rc == 0
    assert "does not exist yet" in capsys.readouterr().out
    assert not (rd / ri.REGISTRY_FILE).exists()  # the CLI is read-only


def test_standalone_registry_cli_flags_reviewers_with_no_resolver(tmp_path, capsys):
    rd = tmp_path / "review"
    rd.mkdir()
    write_registry(rd, [("asha", "Asha Verma", "reviewer", "", "lead"),
                        ("ravi", "Ravi Menon", "reviewer", "", "lead")])
    rc = ri.main(["--review-dir", str(rd)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "no resolver is registered yet" in out
    report = ri.registry_report(rd)
    assert report["ok"] is False and report["resolver_available"] is False
    assert report["reviewers"] == ["asha", "ravi"]


def test_standalone_registry_cli_is_clean_with_reviewers_and_a_resolver(tmp_path, capsys):
    rd = tmp_path / "review"
    rd.mkdir()
    write_registry(rd, [("asha", "Asha Verma", "reviewer", "", "lead"),
                        ("ravi", "Ravi Menon", "reviewer", "", "lead"),
                        ("kiran", "Kiran Das", "resolver", "", "lead")])
    rc = ri.main(["--review-dir", str(rd), "--json"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True and report["resolvers"] == ["kiran"]


def test_standalone_registry_cli_reports_malformed_registry_as_exit_1(tmp_path, capsys):
    rd = tmp_path / "review"
    rd.mkdir()
    (rd / ri.REGISTRY_FILE).write_text(
        "reviewer_id,display_name,role,sheets,attested_by\nAnn Rao!,Ann Rao,reviewer,,lead\n", encoding="utf-8")
    rc = ri.main(["--review-dir", str(rd)])
    assert rc == 1
    assert "problem:" in capsys.readouterr().out


def test_no_resolver_registered_is_pending_in_preflight_only_when_reviewers_exist(w):
    write_registry(w.rd, [("ann", "Ann Rao", "reviewer", "ann-golden", "lead")])
    assert "no_resolver_registered" in found(w.preflight(), "pending")


def test_no_resolver_registered_finding_absent_when_a_resolver_exists(w):
    write_registry(w.rd, [("ann", "Ann Rao", "reviewer", "ann-golden", "lead"),
                          ("bob", "Bob Iyer", "reviewer", "bob-golden", "lead"),
                          ("kiran", "Kiran Das", "resolver", "", "lead")])
    assert "no_resolver_registered" not in found(w.preflight(), "pending")


def test_the_real_review_folder_has_no_registry_sheets_or_disputes_and_stays_pending(monkeypatch):
    monkeypatch.chdir(REPO)
    folder = sorted(p.name for p in Path("evals/review").iterdir())
    assert folder == ["candidate_review_sheet_blank.csv", "golden_review_sheet_blank.csv"]   # nothing was fabricated
    report = rs.build_report([Path("evals/datasets/nimbuscrm_golden_v1.jsonl"), Path("evals/datasets/nimbuscrm_candidates_v2.jsonl")],
                             Path("evals/review"))
    assert report["registry"]["present"] is False
    for data in report["datasets"]:
        assert data["review_state"] == "not_started" and data["ready_for_validated_metrics"] is False
        assert data["identity"]["established"] is False and data["readiness_counts"]["eligible_metric_cases"] == 0
