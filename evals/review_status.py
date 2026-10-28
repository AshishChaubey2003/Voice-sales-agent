"""Read-only report of human-review progress for evaluation datasets.

    python -m evals.review_status [--dataset PATH ...] [--review-dir DIR]
                                  [--write-disagreements PATH] [--write-report PATH]

Reads the dataset(s), the per-reviewer sheets (evals/review/sheet_<name>-<candidates|golden>.csv) and
evals/review/disagreements.csv. It never edits a dataset or a sheet, and it never marks a case
reviewed or excluded: promoting a case into the .jsonl is a human edit (see docs/EVAL_REVIEW_GUIDE.md).
The optional --write-disagreements output is a NEW file only; an existing file is never overwritten.
Well-formedness of sheets and templates (columns, rows, ids, duplicate identities, resolutions) is checked
by the separate read-only evals.review_preflight; readiness is decided only here.
This module is the single source of truth for readiness: the CLI, run summaries, reports and the
dashboard all use dataset_review_summary()/build_report() instead of re-deriving it.

Per-case status
  unreviewed     no reviewer has filled the row
  single_review  one reviewer only (not enough)
  needs_fix      a row is malformed or incomplete (see "problems"); it never counts as a label
  disputed       two complete rows from different sheets differ, no recorded resolution
  resolved       they differ but a resolution and resolved_by are recorded
  excluded       every complete row (two or more reviewers) says ambiguous/exclude, OR a dispute was
                 resolved with final_ambiguous_or_exclude = ambiguous/exclude. A deliberate decision
                 to keep the case out of validated metrics; it is never approved
  approved       two complete, matching rows from different reviewers

Dataset flag (review_status in the .jsonl, a human edit) vs reviewer evidence
  reviewed + approved/resolved       eligible metric case
  excluded + excluded                confirmed exclusion (not a metric example, does not block readiness)
  any other combination              a blocker (open work, awaiting a dataset update, or inconsistent)

Dataset review state
  not_started | in_progress | unresolved | awaiting_dataset_update | evidence_inconsistent
  | identity_unverified | no_eligible_cases | ready

Reviewer identity (evals/review_identity.py) and dispute records
  evals/review/reviewers.csv declares who the reviewers and resolvers are; a file name is not evidence of
  authorship. Without it, or with an undeclared sheet, a registry error or one identity counted twice, a
  dataset that would otherwise be ready reports identity_unverified. A dispute resolution settles a case only
  when its record is complete and valid (see load_resolutions / resolution_problems); an invalid record is
  reported and the case stays disputed. Nothing is repaired, overwritten or written by this module.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from pathlib import Path

from evals import review_identity as ri

VOCAB = {
    "answerable_from_kb": {"yes", "no", "partly"},
    "expected_behavior": {"answer", "correct_premise", "decline_or_abstain", "redirect_to_sales", "other"},
    "should_save": {"yes", "no"},
    "ambiguous_or_exclude": {"keep", "ambiguous", "exclude"},
}
LABELS = ("answerable_from_kb", "evidence_sections", "expected_behavior", "should_save", "ambiguous_or_exclude")
# Labels a reviewer must fill, by kind of case. evidence_sections is required only when the case
# is answerable (checked separately).
REQUIRED = {
    "rag_answer": ("answerable_from_kb", "expected_behavior", "ambiguous_or_exclude"),
    "lead_gate": ("should_save", "ambiguous_or_exclude"),
    "lead_llm": ("should_save", "ambiguous_or_exclude"),
}
STATUSES = ("unreviewed", "single_review", "needs_fix", "disputed", "resolved", "excluded", "approved")
STATES = ("not_started", "in_progress", "unresolved", "awaiting_dataset_update", "evidence_inconsistent",
          "identity_unverified", "no_eligible_cases", "ready")
DEFAULT_REVIEW_DIR = Path("evals/review")
DATASET_LABELS = ("golden", "candidates")
_SUFFIX = re.compile(r"[-_](golden|candidates)$")
FLAGGED = ("ambiguous", "exclude")  # ambiguous_or_exclude values that remove a case from metrics
# Columns every reviewer sheet must have (first word of the header cell). should_save and comment are
# checked per dataset by evals.review_preflight; a sheet without these cannot hold any label at all.
CORE_COLUMNS = ("case_id", "answerable_from_kb", "evidence_sections", "expected_behavior", "ambiguous_or_exclude")
FACT_FIELDS = ("required_facts", "forbidden_facts", "forbidden_regex")
FACTS_REASON = ("required_facts / forbidden_facts / forbidden_regex are author-written. The reviewer sheets do not "
                "check them and no evidence format for checking them exists yet, so every metric built on them "
                "(including correctness) stays provisional even when the dataset is ready.")


def _column(row: dict, name: str) -> str:
    """Header cells may carry hints, e.g. 'answerable_from_kb (yes/no/partly)': match the first word."""
    for key, value in row.items():
        if key and key.split(" ")[0].strip() == name:
            return (value or "").strip()
    return ""


def _read_csv(path: Path):
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def header_problem(fieldnames) -> str | None:
    """Why a sheet's header row cannot hold labels (None when it can).

    A sheet saved with the wrong delimiter or with renamed columns would otherwise read as 'all blank'
    and silently count as nothing."""
    names = {(name or "").split(" ")[0].strip() for name in (fieldnames or [])}
    missing = [column for column in CORE_COLUMNS if column not in names]
    if not missing:
        return None
    first = (fieldnames or [""])[0] or ""
    hint = ""
    if len(fieldnames or []) == 1 and (";" in first or "\t" in first):
        hint = " (the file looks semicolon- or tab-separated: save it as 'CSV UTF-8 (Comma delimited)')"
    return f"missing column(s): {', '.join(missing)}{hint}"


def load_dataset(path: Path) -> list[dict]:
    """Dataset records as plain dicts (read-only). Malformed input raises ValueError with the line."""
    cases = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"{path.name} line {number}: not valid JSON ({error.msg})") from None
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"].strip():
            raise ValueError(f"{path.name} line {number}: every case needs a string 'id'")
        cases.append(case)
    return cases


def dataset_label(path: Path) -> str | None:
    """'golden' or 'candidates' from the dataset file name; None for any other name."""
    stem = path.stem.lower()
    return next((label for label in DATASET_LABELS if label in stem), None)


def sheet_hint(sheet: str) -> str | None:
    """Which dataset a sheet says it is for, from the trailing -golden / -candidates (else None)."""
    match = _SUFFIX.search(sheet.lower())
    return match.group(1) if match else None


def reviewer_identity(sheet: str) -> str:
    """The person behind a sheet: its name without the -golden / -candidates suffix.

    sheet_asha-golden and sheet_asha-candidates are one reviewer; two files for the SAME dataset by the
    same identity must never count as two reviewers."""
    return _SUFFIX.sub("", sheet.lower()) or sheet.lower()


def fingerprint(path: Path) -> str:
    """Short content hash so a changed dataset is visible between status runs."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def load_sheets(review_dir: Path, problems: list[dict] | None = None) -> dict[str, dict[str, dict]]:
    """reviewer -> case_id -> {label: value}. Rows with every label blank are 'not yet reviewed'.

    A case id that appears twice in one sheet is a problem and neither row is used."""
    sheets: dict[str, dict[str, dict]] = {}
    for path in sorted(review_dir.glob("sheet_*.csv")):
        reviewer = path.stem[len("sheet_"):]
        rows: dict[str, dict] = {}
        duplicates: set[str] = set()
        try:
            with path.open(newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                raw_rows = list(reader)
                header_issue = header_problem(reader.fieldnames)
        except (OSError, UnicodeDecodeError, csv.Error) as error:
            if problems is not None:
                problems.append({"sheet": reviewer, "case_id": None, "issue": f"unreadable sheet ({type(error).__name__})"})
            continue
        if header_issue:
            if problems is not None:
                problems.append({"sheet": reviewer, "case_id": None, "issue": f"{header_issue}; sheet not used"})
            continue
        for row in raw_rows:
            case_id = _column(row, "case_id")
            if not case_id:
                continue
            # Vocabulary labels are case-insensitive; section headings are compared exactly as written.
            labels = {name: (_column(row, name) if name == "evidence_sections" else _column(row, name).lower())
                      for name in LABELS}
            labels["comment"] = _column(row, "comment")
            if not any(labels[name] for name in LABELS):
                continue
            if case_id in rows:
                duplicates.add(case_id)
            rows[case_id] = labels
        for case_id in duplicates:
            rows.pop(case_id, None)
            if problems is not None:
                problems.append({"sheet": reviewer, "case_id": case_id, "issue": "case id appears more than once"})
        sheets[reviewer] = rows
    return sheets


def row_problems(kind: str, labels: dict) -> list[str]:
    """Why a filled row cannot count as a label. Empty list = complete and valid."""
    issues = []
    for name in REQUIRED.get(kind, ()):
        if not labels.get(name):
            issues.append(f"missing {name}")
    for name, allowed in VOCAB.items():
        value = labels.get(name)
        if value and value not in allowed:
            issues.append(f"{name} must be one of {'/'.join(sorted(allowed))}")
    if kind == "rag_answer":
        if labels.get("answerable_from_kb") in ("yes", "partly") and not labels.get("evidence_sections"):
            issues.append("evidence_sections required when answerable is yes/partly")
    flagged = labels.get("answerable_from_kb") == "partly" or labels.get("ambiguous_or_exclude") in ("ambiguous", "exclude")
    if flagged and not labels.get("comment"):
        issues.append("comment required for partly/ambiguous/exclude")
    return issues


# Dispute lifecycle (evals/review/disagreements.csv, one or more rows per case, in file order)
#   start -> open | resolved      open -> resolved      resolved -> reopened      reopened -> resolved
# Anything else (a second "resolved" without a "reopened" between, "open" after "open", ...) is an
# invalid transition: the earlier decision is never silently overwritten. A row with no resolution_status
# is the legacy form and is only valid as the single row of its case.
RES_STATUSES = ("open", "resolved", "reopened")
RES_NEXT = {None: ("open", "resolved"), "open": ("resolved",), "resolved": ("reopened",), "reopened": ("resolved",)}
FINAL_LABELS = ("answerable_from_kb", "evidence_sections", "expected_behavior", "should_save")
FINAL_KEEP = ("", "keep", "ambiguous", "exclude")
HALF_FILLED = "resolution and resolved_by must both be filled; the case stays disputed"


def _resolution_record(row: dict) -> dict:
    finals = {name: (_column(row, f"final_{name}") if name == "evidence_sections" else _column(row, f"final_{name}").lower())
              for name in FINAL_LABELS}
    return {"status": _column(row, "resolution_status").lower(), "resolution": _column(row, "resolution"),
            "resolved_by": _column(row, "resolved_by"), "final": _column(row, "final_ambiguous_or_exclude").lower(),
            "evidence": _column(row, "resolution_evidence"), "finals": {k: v for k, v in finals.items() if v}}


def _effective_resolution(records: list[dict], issue) -> dict | None:
    """The settled resolution for one case, or None when the case is (still) disputed. Reports why via issue()."""
    if len(records) > 1 and any(not r["status"] for r in records):
        kind = "duplicate resolution record" if all(r == records[0] for r in records) else "contradictory resolution records"
        issue(f"{kind}: the case has {len(records)} rows and not all carry a resolution_status. Keep one row, or give "
              "every row a resolution_status (open/resolved/reopened) so the history is explicit; the case stays disputed")
        return None
    previous = None
    for record in records:
        status = record["status"]
        if not status:
            continue
        if status not in RES_STATUSES:
            issue(f"resolution_status '{status}' must be one of open/resolved/reopened; resolution not used")
            return None
        if status not in RES_NEXT[previous]:
            issue(f"invalid state transition {previous or 'start'} -> {status}: a decision is never overwritten; "
                  "reopen it first. Resolution not used")
            return None
        previous = status
    last = records[-1]
    holds_final = bool(last["final"] or last["evidence"] or last["finals"])
    if last["status"] == "open":
        if last["resolution"] or last["resolved_by"] or holds_final:
            issue("an unresolved dispute (resolution_status open) already holds resolution fields; set "
                  "resolution_status to resolved, or clear them. The case stays disputed")
        return None
    if last["status"] == "reopened":
        if holds_final:
            issue("a reopened dispute must not hold a final decision, evidence or final labels")
        elif not (last["resolution"] and last["resolved_by"]):
            issue("a reopened row needs resolution (the reason) and resolved_by (who reopened it)")
        return None
    if bool(last["resolution"]) != bool(last["resolved_by"]):
        issue(HALF_FILLED)
        return None
    if not last["resolution"]:
        if last["status"] == "resolved" or holds_final:
            issue("a final decision or resolution_status 'resolved' is recorded without resolution and resolved_by; "
                  "the case stays disputed")
        return None
    if last["final"] not in FINAL_KEEP:
        issue("final_ambiguous_or_exclude must be blank or one of keep/ambiguous/exclude; resolution not used")
        return None
    return {**last, "status": "resolved", "records": len(records)}


def load_resolutions(review_dir: Path, problems: list[dict] | None = None) -> dict[str, dict]:
    """case_id -> settled resolution; a case missing here is still disputed. Structure only (see resolution_problems).

    Columns: resolution (the rationale), resolved_by (a reviewers.csv resolver id), final_ambiguous_or_exclude
    (blank/keep = the case stays a metric example, ambiguous/exclude = it leaves metrics), resolution_evidence,
    final_answerable_from_kb / final_evidence_sections / final_expected_behavior / final_should_save and
    resolution_status (open/resolved/reopened). Nothing is repaired or overwritten: an invalid record is
    reported and the case stays disputed."""
    path = review_dir / "disagreements.csv"
    if not path.exists():
        return {}
    try:
        raw_rows = _read_csv(path)
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        if problems is not None:
            problems.append({"sheet": "disagreements", "case_id": None, "issue": f"unreadable file ({type(error).__name__})"})
        return {}
    grouped: dict[str, list[dict]] = {}
    for row in raw_rows:
        case_id = _column(row, "case_id")
        if case_id:
            grouped.setdefault(case_id, []).append(_resolution_record(row))
    resolutions = {}
    for case_id, records in grouped.items():
        def issue(text: str, case_id: str = case_id) -> None:
            if problems is not None:
                problems.append({"sheet": "disagreements", "case_id": case_id, "issue": text})
        settled = _effective_resolution(records, issue)
        if settled is not None:
            resolutions[case_id] = settled
    return resolutions


def resolution_problems(kind: str, resolution: dict, dataset_reviewers, registry: dict | None) -> list[dict]:
    """Who settled the dispute and on what evidence. Checked only against a reviewer registry (no registry,
    no way to establish identity: the dataset is then not ready anyway). Each item {code, severity, message}."""
    if not registry or not registry.get("present"):
        return []
    found: list[dict] = []

    def add(code: str, message: str) -> None:
        found.append({"code": code, "severity": "error", "message": message})

    by = resolution["resolved_by"].strip().lower()
    entry = registry["entries"].get(by)
    if entry is None:
        add("resolver_unregistered", f"resolved_by '{resolution['resolved_by']}' is not one declared reviewer_id in "
                                     "reviewers.csv (name exactly one resolver)")
    else:
        if entry["role"] != "resolver":
            add("resolver_wrong_role", f"'{by}' is registered as {entry['role']}, not resolver")
        if by in dataset_reviewers:
            add("resolver_is_reviewer", f"'{by}' filled a sheet for this dataset and cannot settle its disputes")
    if not resolution.get("evidence"):
        add("resolution_evidence_missing", "resolution_evidence is blank: cite the knowledge-file heading or sentence "
                                           "that supports the decision")
    if resolution.get("final") not in FLAGGED:
        labels = {**(resolution.get("finals") or {}), "ambiguous_or_exclude": "keep", "comment": resolution.get("resolution", "")}
        issues = row_problems(kind, labels)
        if issues:
            add("final_labels_invalid", "the final decision is incomplete or invalid (" + "; ".join(issues) +
                "). Fill the final_* columns, or choose ambiguous/exclude in final_ambiguous_or_exclude")
    return found


def _sections(text: str) -> tuple:
    return tuple(sorted({part.strip() for part in text.split(";") if part.strip()}))


def _agreed_key(labels: dict) -> tuple:
    """Order and spacing of the evidence sections do not make two reviewers disagree."""
    return tuple((name, _sections(labels.get(name, "")) if name == "evidence_sections" else labels.get(name, ""))
                 for name in LABELS)


def classify(cases: list[dict], sheets: dict[str, dict[str, dict]], resolutions: dict[str, dict],
             problems: list[dict] | None = None, identity_of=None, registry: dict | None = None) -> dict[str, str]:
    """Per-case status from reviewer evidence only; the dataset's own review_status flag is not an input.

    identity_of maps a sheet to its reviewer (default: the file-name rule; with a reviewers.csv registry a
    sheet that is not declared maps to None and counts for nothing). A resolution whose resolver or
    evidence fails resolution_problems() does not settle the dispute."""
    lookup = identity_of or reviewer_identity
    dataset_reviewers = {lookup(name) for name, rows in sheets.items() if rows} - {None}
    status: dict[str, str] = {}
    for case in cases:
        case_id, kind = case["id"], case.get("kind", "rag_answer")
        valid, invalid, seen = [], 0, {}
        for reviewer, rows in sheets.items():
            labels = rows.get(case_id)
            if labels is None:
                continue
            person = lookup(reviewer)
            if person is None:
                continue  # sheet not declared in reviewers.csv: reported once per sheet, never counted
            issues = row_problems(kind, labels)
            if issues:
                invalid += 1
                if problems is not None:
                    problems.append({"sheet": reviewer, "case_id": case_id, "issue": "; ".join(issues)})
                continue
            if person in seen:
                # One person in two files must never satisfy the two-reviewer requirement.
                if problems is not None:
                    problems.append({"sheet": reviewer, "case_id": case_id,
                                     "issue": f"reviewer '{person}' already reviewed this case in sheet '{seen[person]}'; counted once"})
                continue
            seen[person] = reviewer
            valid.append(labels)
        if invalid and len(valid) < 2:
            status[case_id] = "needs_fix"
        elif not valid:
            status[case_id] = "unreviewed"
        elif len(valid) == 1:
            status[case_id] = "single_review"
        elif all(v["ambiguous_or_exclude"] in FLAGGED for v in valid):
            status[case_id] = "excluded"  # every reviewer flagged it, whatever their other labels say
        elif any(_agreed_key(v) != _agreed_key(valid[0]) for v in valid[1:]):
            resolution = resolutions.get(case_id)
            blocked = []
            if resolution is not None:
                blocked = [p for p in resolution_problems(kind, resolution, dataset_reviewers, registry)
                           if p["severity"] == "error"]
                if problems is not None:
                    problems.extend({"sheet": "disagreements", "case_id": case_id, "issue": p["message"]} for p in blocked)
            if resolution is None or blocked:
                status[case_id] = "disputed"
            elif resolution.get("final") in FLAGGED:
                status[case_id] = "excluded"
            else:
                status[case_id] = "resolved"
        else:
            status[case_id] = "approved"
    return status


def dataset_differences(case: dict, labels: dict) -> list[str]:
    """Where the (agreed) reviewer labels disagree with what the dataset currently expects.

    Reported only; the dataset is never changed here."""
    kind = case.get("kind")
    diffs = []
    if kind == "lead_gate":
        if (labels.get("should_save") == "yes") != (case.get("expected_gate") == "saved"):
            diffs.append("expected_gate")
    elif kind == "lead_llm":
        if (labels.get("should_save") == "yes") != bool(case.get("expected_lead_saved")):
            diffs.append("expected_lead_saved")
    elif kind == "rag_answer":
        sections = {s.strip() for s in labels.get("evidence_sections", "").split(";") if s.strip()}
        if sections != set(case.get("expected_sources") or []):
            diffs.append("expected_sources")
        if (labels.get("expected_behavior") == "decline_or_abstain") != bool(case.get("expect_abstain")):
            diffs.append("expect_abstain")
    return diffs


def _agreed_labels(case_id: str, kind: str, sheets) -> dict | None:
    for rows in sheets.values():
        labels = rows.get(case_id)
        if labels is not None and not row_problems(kind, labels):
            return labels
    return None


def usable_sheets(sheets: dict[str, dict[str, dict]], label: str | None) -> dict[str, dict[str, dict]]:
    """Sheets that apply to a dataset: unlabelled sheets, and sheets named for this dataset (-golden/-candidates)."""
    if label is None:
        return dict(sheets)
    return {name: rows for name, rows in sheets.items() if sheet_hint(name) in (None, label)}


def _state(counts: dict, rc: dict, inconsistent: bool, started: bool) -> str:
    if rc["eligible_metric_cases"] > 0 and not inconsistent and not _open(counts) and not rc["awaiting_dataset_update"] \
            and rc["marked_reviewed"] + rc["confirmed_excluded"] == rc["total_cases"]:
        return "ready"
    if inconsistent:
        return "evidence_inconsistent"
    if not started:
        return "not_started"
    if _open(counts):
        awaiting = counts["unreviewed"] + counts["single_review"]
        return "unresolved" if (counts["disputed"] + counts["needs_fix"]) and not awaiting else "in_progress"
    if rc["awaiting_dataset_update"]:
        return "awaiting_dataset_update"
    return "no_eligible_cases" if rc["eligible_metric_cases"] == 0 else "evidence_inconsistent"


def _open(counts: dict) -> int:
    return counts["unreviewed"] + counts["single_review"] + counts["needs_fix"] + counts["disputed"]


def _explain(state: str, rc: dict) -> tuple[str, str]:
    total, eligible = rc["total_cases"], rc["eligible_metric_cases"]
    if state == "not_started":
        return ("No reviewer has labelled any case, so every metric on this dataset is a provisional smoke-test signal.",
                "Two reviewers copy the blank sheet and fill it in independently (docs/EVAL_REVIEW_GUIDE.md).")
    if state == "in_progress":
        return (f"Review is in progress: {rc['unreviewed']} case(s) not started, {rc['awaiting_second_reviewer']} awaiting a "
                f"second reviewer, {rc['missing_or_conflicting_evidence']} with a dispute or malformed row. "
                f"{eligible} of {total} cases are eligible metric cases.",
                "Finish the remaining sheets, fix malformed rows, then re-run this command.")
    if state == "unresolved":
        return (f"Every case has been looked at, but {rc['missing_or_conflicting_evidence']} case(s) have a dispute or a "
                "malformed row. Metrics stay provisional.",
                "Fix malformed rows; settle disputes with --write-disagreements and a recorded resolution.")
    if state == "awaiting_dataset_update":
        return (f"The reviewers have settled {rc['awaiting_dataset_update']} case(s) that the dataset file does not yet record.",
                "A named human sets review_status to reviewed (or excluded) with reviewer names in the .jsonl for those cases.")
    if state == "evidence_inconsistent":
        return ("The dataset file and the reviewer evidence disagree (see blockers). Metrics stay provisional.",
                "Fix the dataset flags or the sheets so each reviewed/excluded flag has matching reviewer evidence.")
    if state == "identity_unverified":
        return ("The reviewer evidence is complete, but who the reviewers are has not been established, so their "
                "independence cannot be confirmed. Metrics stay provisional.",
                "The review lead declares every reviewer and resolver in evals/review/reviewers.csv "
                "(docs/EVAL_REVIEW_GUIDE.md), then re-run this command.")
    if state == "no_eligible_cases":
        return ("Every case is excluded or otherwise not a metric example, so there is nothing to validate.",
                "Add or approve in-scope cases before reporting validated metrics.")
    return (f"Review complete: {eligible} eligible metric case(s) with reviewer agreement; "
            f"{rc['confirmed_excluded']} excluded case(s) are not metric examples.", "Validated metrics may now be reported.")


def _dataset_report(path: Path, sheets: dict, resolutions: dict, problems: list[dict], registry: dict | None = None) -> dict:
    cases = load_dataset(path)
    ids = [c["id"] for c in cases]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    usable = usable_sheets(sheets, dataset_label(path))
    lookup = ri.identity_of(registry)
    status = classify(cases, usable, resolutions, problems, identity_of=lookup, registry=registry)
    counts = {name: 0 for name in STATUSES}
    for value in status.values():
        counts[value] += 1
    flags = {c["id"]: c.get("review_status", "unreviewed") for c in cases}
    unknown_flags = sorted(i for i, f in flags.items() if f not in ("unreviewed", "reviewed", "excluded"))
    marked = {i for i, f in flags.items() if f == "reviewed"}
    marked_excluded = {i for i, f in flags.items() if f == "excluded"}
    supported = {i for i, s in status.items() if s in ("approved", "resolved")}
    confirmed_excluded = {i for i in marked_excluded if status[i] == "excluded"}
    eligible = marked & supported
    awaiting_update = sorted(i for i in ids if flags[i] not in ("reviewed", "excluded")
                             and status[i] in ("approved", "resolved", "excluded"))
    reviewed_unsupported = sorted(marked - supported)
    excluded_unsupported = sorted(marked_excluded - confirmed_excluded)
    needs_update = []
    for case in cases:
        diffs = []
        if status[case["id"]] == "approved":
            labels = _agreed_labels(case["id"], case.get("kind", "rag_answer"), usable)
            diffs = dataset_differences(case, labels) if labels else []
        elif status[case["id"]] == "resolved":
            settled = resolutions.get(case["id"]) or {}
            if settled.get("finals"):
                diffs = dataset_differences(case, settled["finals"])  # the adjudicator's final labels, not a vote
        if diffs:
            needs_update.append({"case_id": case["id"], "fields": diffs})
    rc = {
        "total_cases": len(cases),
        "unreviewed": counts["unreviewed"],
        "awaiting_second_reviewer": counts["single_review"],
        "missing_or_conflicting_evidence": counts["needs_fix"] + counts["disputed"],
        "approved_or_resolved": counts["approved"] + counts["resolved"],
        "excluded_by_reviewers": counts["excluded"],
        "marked_reviewed": len(marked),
        "marked_excluded": len(marked_excluded),
        "confirmed_excluded": len(confirmed_excluded),
        "awaiting_dataset_update": len(awaiting_update),
        "eligible_metric_cases": len(eligible),
    }
    facts_cases = [c for c in cases if c.get("review_status") != "excluded" and any(c.get(k) for k in FACT_FIELDS)]
    facts_review = {
        "cases_with_facts": len(facts_cases),
        "marked_reviewed": sum(1 for c in facts_cases if c.get("facts_review_status") == "reviewed"),
        "validated": False,  # no evidence format for fact review exists; a flag alone is not evidence
        "reason": FACTS_REASON,
    }
    blockers = []
    unmarked = len(cases) - len(marked) - len(marked_excluded)
    if unmarked:
        blockers.append(f"{unmarked} of {len(cases)} cases are not marked reviewed or excluded in the dataset file")
    if _open(counts):
        blockers.append(f"{_open(counts)} case(s) still need reviewer work: {counts['unreviewed']} not started, "
                        f"{counts['single_review']} awaiting a second reviewer, {counts['needs_fix']} malformed, "
                        f"{counts['disputed']} disputed")
    if awaiting_update:
        blockers.append(f"{len(awaiting_update)} case(s) are settled by the reviewers but not yet updated in the dataset file")
    if reviewed_unsupported:
        blockers.append(f"{len(reviewed_unsupported)} case(s) marked reviewed have no two matching sheets or resolution")
    if excluded_unsupported:
        blockers.append(f"{len(excluded_unsupported)} case(s) marked excluded without reviewer agreement to exclude")
    if needs_update:
        blockers.append(f"{len(needs_update)} approved or resolved case(s) have final labels that differ from the dataset's expectations")
    if duplicates:
        blockers.append(f"dataset has duplicate case ids: {', '.join(duplicates)}")
    if unknown_flags:
        blockers.append(f"{len(unknown_flags)} case(s) have an unknown review_status value")
    if not eligible and not blockers:
        blockers.append("no eligible metric cases: validated metrics need at least one reviewed, approved case")
    inconsistent = bool(reviewed_unsupported or excluded_unsupported or needs_update or duplicates or unknown_flags)
    started = any(v != "unreviewed" for v in status.values())
    state = _state(counts, rc, inconsistent, started)
    identity = ri.summarise(registry, [name for name, rows in usable.items() if rows])
    if state == "ready":
        blockers = []
        if not identity["established"]:
            state = "identity_unverified"
            blockers = list(identity["reasons"])
    explanation, next_action = _explain(state, rc)
    return {
        "dataset": path.name, "fingerprint": fingerprint(path), "cases": len(cases), "counts": counts,
        "review_state": state, "explanation": explanation, "next_action": next_action,
        "readiness_counts": rc,
        "identity": identity,
        "facts_review": facts_review,
        "dataset_marked_reviewed": len(marked),
        "dataset_marked_excluded": len(marked_excluded),
        "marked_reviewed_without_sheet_support": reviewed_unsupported,
        "marked_excluded_without_agreement": excluded_unsupported,
        "awaiting_dataset_update": awaiting_update,
        "approved_but_dataset_differs": needs_update,
        "ready_for_validated_metrics": state == "ready",
        "blockers": blockers,
    }


def build_report(datasets: list[Path], review_dir: Path) -> dict:
    """Read-only. The one calculation behind the CLI, run summaries, reports and the dashboard."""
    problems: list[dict] = []
    sheets = load_sheets(review_dir, problems)
    resolutions = load_resolutions(review_dir, problems)
    registry = ri.load_registry(review_dir)
    problems.extend({"sheet": "reviewers", "case_id": None, "issue": p["message"]} for p in registry["problems"])
    lookup = ri.identity_of(registry)
    if registry["present"]:
        problems.extend({"sheet": name, "case_id": None,
                         "issue": "sheet is not listed in reviewers.csv; its labels are not counted"}
                        for name, rows in sorted(sheets.items()) if rows and lookup(name) is None)
    out_datasets = []
    all_ids: set[str] = set()
    ids_by_label: dict[str, set[str]] = {}
    for path in datasets:
        out_datasets.append(_dataset_report(path, sheets, resolutions, problems, registry))
        ids = {c["id"] for c in load_dataset(path)}
        all_ids |= ids
        if dataset_label(path):
            ids_by_label.setdefault(dataset_label(path), set()).update(ids)
    for name, rows in sheets.items():
        hint = sheet_hint(name)
        if hint in ids_by_label:
            for case_id in sorted(set(rows) - ids_by_label[hint]):
                problems.append({"sheet": name, "case_id": case_id,
                                 "issue": f"sheet is named for the {hint} dataset but this case id is not in it"})
    for case_id in sorted(set(resolutions) - all_ids):
        problems.append({"sheet": "disagreements", "case_id": case_id, "issue": "case id is not in any dataset"})
    warnings = []
    if not review_dir.is_dir():
        warnings.append(f"review directory not found: {review_dir} (no reviewer sheets can be read)")
    return {
        "reviewer_sheets": sorted(sheets),
        "registry": {"present": registry["present"], "problems": len(registry["problems"]),
                     "identities": {rid: e["role"] for rid, e in sorted(registry["entries"].items())}},
        "datasets": out_datasets,
        "sheet_ids_not_in_any_dataset": sorted({i for rows in sheets.values() for i in rows} - all_ids),
        "problems": problems,
        "warnings": warnings,
        "note": "Read-only. approved/resolved mean the sheets support promotion; a dataset changes only by a "
                "human edit. Until a dataset is ready_for_validated_metrics, every metric computed on it is provisional.",
    }


def provisional_message(summary: dict) -> str | None:
    """Banner text for any consumer; None only when the dataset is ready for validated metrics."""
    if summary.get("ready_for_validated_metrics"):
        return None
    return (f"Provisional: review state is '{summary.get('state', 'unknown')}' "
            f"({summary.get('eligible_metric_cases', 0)} of {summary.get('total_cases', '?')} cases are eligible metric cases). "
            "Treat every number as a smoke-test signal, not a validated result. "
            "Run python -m evals.review_status for details.")


def run_banner(dataset_meta: dict) -> str | None:
    """Notice for a saved run's dataset block (new runs carry a 'review' block; older runs do not)."""
    review = (dataset_meta or {}).get("review")
    if review:
        return provisional_message(review)
    if not (dataset_meta or {}).get("reviewed_cases"):
        return ("No case in this dataset has been reviewed by a human. Treat every number "
                "as a smoke-test signal, not a quality guarantee.")
    return ("This run did not record reviewer evidence (older run): the dataset file's reviewed flags were not "
            "checked against reviewer sheets. Treat every number as provisional.")


def dataset_review_summary(dataset_path: str | Path, review_dir: str | Path = DEFAULT_REVIEW_DIR) -> dict:
    """Compact, read-only readiness block that run summaries store next to their dataset metadata."""
    try:
        d = build_report([Path(dataset_path)], Path(review_dir))["datasets"][0]
    except (OSError, ValueError) as error:
        summary = {"state": "unknown", "ready_for_validated_metrics": False, "eligible_metric_cases": 0,
                   "total_cases": None, "excluded_cases": 0, "facts_validated": False,
                   "identity_established": False,
                   "blockers": [f"review status could not be calculated ({type(error).__name__})"]}
    else:
        rc = d["readiness_counts"]
        summary = {"state": d["review_state"], "ready_for_validated_metrics": d["ready_for_validated_metrics"],
                   "eligible_metric_cases": rc["eligible_metric_cases"], "total_cases": rc["total_cases"],
                   "excluded_cases": rc["confirmed_excluded"], "unreviewed": rc["unreviewed"],
                   "awaiting_second_reviewer": rc["awaiting_second_reviewer"],
                   "missing_or_conflicting_evidence": rc["missing_or_conflicting_evidence"],
                   "facts_validated": d["facts_review"]["validated"],
                   "identity_established": d["identity"]["established"],
                   "blockers": d["blockers"]}
    summary["message"] = provisional_message(summary) or _ready_message(summary)
    return summary


def _ready_message(summary: dict) -> str:
    return (f"Review complete: {summary['eligible_metric_cases']} eligible metric case(s); "
            f"{summary['excluded_cases']} excluded case(s) are not metric examples.")


def format_text(report: dict) -> str:
    lines = []
    for w in report.get("warnings", []):
        lines.append(f"WARNING: {w}")
    lines.append(f"reviewer sheets: {', '.join(report['reviewer_sheets']) or 'none'}")
    for d in report["datasets"]:
        rc = d["readiness_counts"]
        lines += ["", f"{d['dataset']}  ({d['cases']} cases, fingerprint {d['fingerprint']})",
                  f"  state: {d['review_state']}  ready_for_validated_metrics: {str(d['ready_for_validated_metrics']).lower()}",
                  f"  {d['explanation']}", f"  next: {d['next_action']}",
                  "  cases: " + ", ".join(f"{k} {v}" for k, v in d["counts"].items()),
                  f"  reviewer identity: {d['identity']['source']}, established: {str(d['identity']['established']).lower()}",
                  f"  eligible metric cases: {rc['eligible_metric_cases']}   confirmed exclusions: {rc['confirmed_excluded']}"]
        lines += [f"  blocker: {b}" for b in d["blockers"]]
    lines += ["", f"problems: {len(report['problems'])}"]
    lines += [f"  {p['sheet']} / {p['case_id']}: {p['issue']}" for p in report["problems"][:25]]
    return "\n".join(lines)


def write_disagreements(datasets: list[Path], review_dir: Path, target: Path) -> int:
    """New CSV listing every disputed case with each reviewer's labels side by side. Never overwrites."""
    if target.exists():
        raise FileExistsError(f"{target} already exists; choose another name (existing files are never overwritten)")
    sheets = load_sheets(review_dir)
    resolutions = load_resolutions(review_dir)
    reviewers = sorted(sheets)
    rows = []
    for path in datasets:
        cases = load_dataset(path)
        usable = usable_sheets(sheets, dataset_label(path))
        status = classify(cases, usable, resolutions)
        for case in cases:
            if status[case["id"]] != "disputed":
                continue
            filled = {r: usable[r][case["id"]] for r in reviewers if r in usable and case["id"] in usable[r]
                      and not row_problems(case.get("kind", "rag_answer"), usable[r][case["id"]])}
            differing = [n for n in LABELS if len({v.get(n, "") for v in filled.values()}) > 1]
            rows.append({"case_id": case["id"], "differing_labels": ";".join(differing),
                         **{f"labels_{r}": "; ".join(f"{n}={filled[r].get(n, '')}" for n in LABELS if filled[r].get(n))
                            if r in filled else "" for r in reviewers},
                         "resolution": "", "resolved_by": "", "final_ambiguous_or_exclude": "",
                         "resolution_status": "open", "resolution_evidence": "",
                         **{f"final_{n}": "" for n in FINAL_LABELS}})
    with target.open("x", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["case_id", "differing_labels", *[f"labels_{r}" for r in reviewers],
                                                    "resolution", "resolved_by", "final_ambiguous_or_exclude",
                                                    "resolution_status", "resolution_evidence",
                                                    *[f"final_{n}" for n in FINAL_LABELS]])
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dataset", action="append", default=None,
                        help="dataset .jsonl (repeatable; default: golden and candidates)")
    parser.add_argument("--review-dir", default=str(DEFAULT_REVIEW_DIR))
    parser.add_argument("--write-disagreements", metavar="PATH", help="write a NEW disagreements template (never overwrites)")
    parser.add_argument("--text", action="store_true", help="print a short plain-text summary instead of JSON")
    parser.add_argument("--require-ready", action="store_true",
                        help="exit with status 1 unless every dataset is ready_for_validated_metrics")
    args = parser.parse_args(argv)
    paths = [Path(p) for p in (args.dataset or ["evals/datasets/nimbuscrm_golden_v1.jsonl",
                                                "evals/datasets/nimbuscrm_candidates_v2.jsonl"])]
    missing = [p for p in paths if not p.exists()]
    if missing:
        print(f"error: dataset not found: {', '.join(map(str, missing))}", file=sys.stderr)
        return 2
    review_dir = Path(args.review_dir)
    try:
        report = build_report(paths, review_dir)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(format_text(report) if args.text else json.dumps(report, indent=2))
    if args.write_disagreements:
        try:
            count = write_disagreements(paths, review_dir, Path(args.write_disagreements))
        except FileExistsError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        print(f"wrote {count} disputed case(s) to {args.write_disagreements}")
    if args.require_ready and not all(d["ready_for_validated_metrics"] for d in report["datasets"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
