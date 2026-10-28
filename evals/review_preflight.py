"""Read-only preflight for the human review: is every sheet usable, and what is still missing?

    python -m evals.review_preflight [--dataset PATH ...] [--review-dir DIR] [--knowledge-dir DIR]
                                     [--json] [--strict]

Run it before the review starts (are the blank templates intact?), while reviewers work (is each sheet
well formed? who is missing?) and before anyone edits a dataset (are all disagreements settled?).
It never writes, edits or creates anything: not the datasets, not a sheet, not a template.

Findings have a severity:
  error    something is wrong and must be fixed before the sheet or file can count (unreadable or renamed
           columns, deleted rows, duplicate or unknown case ids, malformed rows, one person in two files,
           an incomplete resolution, a template with labels filled in)
  pending  honest unfinished work (blank rows, a reviewer or dataset with no sheet yet, one reviewer only,
           a dispute with no recorded resolution)
  warning  a human should look (identical sheets, an evidence section that matches no heading in the
           knowledge files, a sheet name without -golden/-candidates, an unexpected file in the review folder)
  info     context

Identity and disputes (see evals/review_identity.py and docs/EVAL_REVIEW_GUIDE.md)
  With evals/review/reviewers.csv present, every sheet must be declared there (one author each), one identity
  can never count twice for a dataset, and a dispute resolver must be a declared resolver who filled no sheet.
  Without it, identity is only inferred from file names: that is reported as pending work, and the status tool
  withholds readiness. Dispute records are checked for unknown cases, incomplete or contradictory rows, invalid
  decisions and invalid state transitions (open -> resolved -> reopened -> resolved); nothing is repaired.

Exit status: 0 unless there is an error (1); with --strict also 1 when anything is pending.
This tool reports on evidence. Readiness for validated metrics is decided only by evals.review_status.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

from evals import review_assign as ra, review_identity as ri, review_policy as rp, review_status as rs

DEFAULT_KNOWLEDGE_DIR = Path("knowledge/nimbuscrm")
MIN_REVIEWERS = 2
IDENTICAL_MIN_ROWS = 10  # fewer shared filled rows than this says nothing about copying
EXAMPLES = 5


def _finding(findings: list[dict], severity: str, scope: str, check: str, message: str,
             sheet: str | None = None, case_ids: list[str] | None = None) -> None:
    entry = {"severity": severity, "scope": scope, "check": check, "message": message}
    if sheet:
        entry["sheet"] = sheet
    if case_ids:
        entry["case_ids"] = case_ids[:EXAMPLES] if len(case_ids) > EXAMPLES else list(case_ids)
        if len(case_ids) > EXAMPLES:
            entry["case_ids_total"] = len(case_ids)
    findings.append(entry)


def _sample(ids: list[str]) -> str:
    shown = ", ".join(ids[:EXAMPLES])
    return shown + (f" (+{len(ids) - EXAMPLES} more)" if len(ids) > EXAMPLES else "")


def knowledge_sections(knowledge_dir: Path) -> set[str] | None:
    """'Title > Heading' for every ## heading in the knowledge files (the names reviewers must write)."""
    if not knowledge_dir.is_dir():
        return None
    sections: set[str] = set()
    for path in sorted(knowledge_dir.glob("*.md")):
        title = None
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
            elif line.startswith("## ") and title:
                sections.add(f"{title} > {line[3:].strip()}")
    return sections or None


def _read_sheet(path: Path):
    """(fieldnames, rows) or an error string. Never raises."""
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            return reader.fieldnames, rows
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        return f"unreadable ({type(error).__name__})"


def _row_labels(row: dict) -> dict:
    labels = {name: (rs._column(row, name) if name == "evidence_sections" else rs._column(row, name).lower())
              for name in rs.LABELS}
    labels["comment"] = rs._column(row, "comment")
    return labels


def _check_template(path: Path | None, label: str, cases: list[dict], review_dir: Path,
                    findings: list[dict]) -> dict:
    info = {"path": str(path) if path else None, "ok": False}
    if path is None:
        _finding(findings, "error", label, "template_missing",
                 f"no blank {label} sheet found in {review_dir}. Create one (it never overwrites): "
                 f"python -m evals.review_sheets --dataset <{label} .jsonl> --out {review_dir}/{label.rstrip('s')}_review_sheet_blank.csv")
        return info
    read = _read_sheet(path)
    if isinstance(read, str):
        _finding(findings, "error", label, "template_unreadable", f"{path.name}: {read}")
        return info
    fieldnames, rows = read
    issue = rs.header_problem(fieldnames)
    if issue:
        _finding(findings, "error", label, "template_header", f"{path.name}: {issue}")
        return info
    ids = [rs._column(r, "case_id") for r in rows if rs._column(r, "case_id")]
    expected = [c["id"] for c in cases]
    if ids != expected:
        missing = [i for i in expected if i not in ids]
        extra = [i for i in ids if i not in expected]
        detail = []
        if missing:
            detail.append(f"missing {len(missing)} ({_sample(missing)})")
        if extra:
            detail.append(f"unknown {len(extra)} ({_sample(extra)})")
        if not missing and not extra:
            detail.append("same ids in a different order or duplicated")
        _finding(findings, "error", label, "template_cases",
                 f"{path.name} does not match the dataset: {'; '.join(detail)}. Do not edit templates; regenerate a fresh one.")
        return info
    filled = [rs._column(r, "case_id") for r in rows
              if any(rs._column(r, n) for n in rs.LABELS) or rs._column(r, "comment")]
    if filled:
        _finding(findings, "error", label, "template_filled",
                 f"{path.name} has labels or comments filled in on {len(filled)} row(s) ({_sample(filled)}). "
                 "A template must stay blank; restore it from version control and copy it for your own sheet.",
                 case_ids=filled)
        return info
    info.update(ok=True, rows=len(rows))
    return info


def _find_template(review_dir: Path, label: str) -> Path | None:
    stem = label.rstrip("s")  # candidate_review_sheet_blank.csv, golden_review_sheet_blank.csv
    matches = sorted(p for p in review_dir.glob("*_review_sheet_blank.csv") if p.name.startswith(stem))
    return matches[0] if matches else None


def _analyse_sheet(path: Path, name: str, label: str, cases: list[dict], all_ids: set[str],
                   sections: set[str] | None, findings: list[dict]) -> dict | None:
    """Row-level checks of one sheet against one dataset. None when the sheet cannot be used."""
    read = _read_sheet(path)
    if isinstance(read, str):
        _finding(findings, "error", label, "sheet_unreadable", f"{path.name}: {read}", sheet=name)
        return None
    fieldnames, rows = read
    issue = rs.header_problem(fieldnames)
    if issue:
        _finding(findings, "error", label, "sheet_header", f"{path.name}: {issue}; the sheet is not used", sheet=name)
        return None
    names = {(f or "").split(" ")[0].strip() for f in fieldnames or []}
    kinds = {c["id"]: c.get("kind", "rag_answer") for c in cases}
    own_ids = [rs._column(r, "case_id") for r in rows if rs._column(r, "case_id")]
    hint = rs.sheet_hint(name)
    in_dataset = [i for i in own_ids if i in kinds]
    if hint is None and not in_dataset:
        return None  # an unlabelled sheet for another dataset says nothing about this one
    result = {"sheet": name, "reviewer": rs.reviewer_identity(name), "rows": len(own_ids)}

    needed = set()
    if any(kinds[i] in ("lead_gate", "lead_llm") for i in kinds):
        needed.add("should_save")
    needed.add("comment")
    absent = sorted(needed - names)
    if absent:
        _finding(findings, "error", label, "sheet_columns",
                 f"{path.name}: missing column(s) {', '.join(absent)} that this dataset's rows need", sheet=name)

    # Same rule as evals.review_status: only rows with a label filled in count, so a second row that is
    # entirely blank is harmless (a warning), while two filled rows for one case are both discarded.
    filled_ids = [rs._column(r, "case_id") for r in rows
                  if rs._column(r, "case_id") and any(_row_labels(r)[n] for n in rs.LABELS)]
    duplicates = sorted({i for i in filled_ids if filled_ids.count(i) > 1 and i in kinds})
    if duplicates:
        _finding(findings, "error", label, "sheet_duplicate_rows",
                 f"{path.name}: case id appears more than once with labels: {_sample(duplicates)}; neither row is used",
                 sheet=name, case_ids=duplicates)
    extra_blank = sorted({i for i in own_ids if own_ids.count(i) > 1 and i in kinds} - set(duplicates))
    if extra_blank:
        _finding(findings, "warning", label, "sheet_extra_blank_rows",
                 f"{path.name}: case id appears on more than one row (the extra rows are blank and ignored): "
                 f"{_sample(extra_blank)}", sheet=name, case_ids=extra_blank)
    unknown = sorted({i for i in own_ids if i not in kinds}) if hint else sorted({i for i in own_ids if i not in all_ids})
    if unknown:
        _finding(findings, "error", label, "sheet_unknown_ids",
                 f"{path.name}: {len(unknown)} case id(s) are not in the {label if hint else 'any'} dataset: "
                 f"{_sample(unknown)}", sheet=name, case_ids=unknown)
    missing = [i for i in kinds if i not in own_ids]
    if missing:
        _finding(findings, "error", label, "sheet_missing_rows",
                 f"{path.name}: {len(missing)} case row(s) are missing (rows must never be deleted): {_sample(missing)}",
                 sheet=name, case_ids=missing)

    blank, complete, malformed, kind_mismatch, bad_sections = [], [], [], [], []
    labelled: dict[str, dict] = {}
    for row in rows:
        case_id = rs._column(row, "case_id")
        if case_id not in kinds or case_id in duplicates:
            continue
        if "kind" in names and rs._column(row, "kind") and rs._column(row, "kind") != kinds[case_id]:
            kind_mismatch.append(case_id)
        labels = _row_labels(row)
        if not any(labels[n] for n in rs.LABELS):
            if case_id not in blank and case_id not in labelled:
                blank.append(case_id)
            continue
        issues = rs.row_problems(kinds[case_id], labels)
        if issues:
            malformed.append((case_id, "; ".join(issues)))
            continue
        complete.append(case_id)
        labelled[case_id] = labels
        if sections is not None and labels.get("evidence_sections"):
            for part in (p.strip() for p in labels["evidence_sections"].split(";") if p.strip()):
                if part not in sections:
                    bad_sections.append((case_id, part))
    if kind_mismatch:
        _finding(findings, "error", label, "sheet_kind_mismatch",
                 f"{path.name}: the kind column differs from the dataset for {_sample(kind_mismatch)} "
                 "(rows were probably moved or edited)", sheet=name, case_ids=kind_mismatch)
    if malformed:
        _finding(findings, "error", label, "sheet_malformed_rows",
                 f"{path.name}: {len(malformed)} row(s) are incomplete or invalid and do not count: "
                 + "; ".join(f"{i}: {why}" for i, why in malformed[:EXAMPLES])
                 + (f" (+{len(malformed) - EXAMPLES} more)" if len(malformed) > EXAMPLES else ""),
                 sheet=name, case_ids=[i for i, _ in malformed])
    if blank:
        _finding(findings, "pending", label, "sheet_blank_rows",
                 f"{path.name}: {len(blank)} of {len(kinds)} rows are still blank", sheet=name, case_ids=blank)
    if bad_sections:
        _finding(findings, "warning", label, "unknown_evidence_section",
                 f"{path.name}: {len(bad_sections)} evidence section(s) match no heading in the knowledge files "
                 "(write them exactly as 'Title > Heading'): "
                 + "; ".join(f"{i}: '{part}'" for i, part in bad_sections[:EXAMPLES]),
                 sheet=name, case_ids=sorted({i for i, _ in bad_sections}))
    result.update(blank=len(blank), complete=len(complete), malformed=len(malformed),
                  missing=len(missing), unknown=len(unknown), duplicate_rows=len(duplicates))
    result["_labelled"] = labelled
    return result


def _identical_pairs(analysed: list[dict], label: str, findings: list[dict]) -> None:
    people = {}
    for item in analysed:
        people.setdefault(item["reviewer"], item)
    names = sorted(people)
    for a in range(len(names)):
        for b in range(a + 1, len(names)):
            la, lb = people[names[a]]["_labelled"], people[names[b]]["_labelled"]
            common = sorted(set(la) & set(lb))
            if len(common) >= IDENTICAL_MIN_ROWS and all(la[i] == lb[i] for i in common):
                _finding(findings, "warning", label, "identical_sheets",
                         f"reviewers '{names[a]}' and '{names[b]}' have identical labels and comments on all "
                         f"{len(common)} shared rows. Confirm they worked independently.")


def _resolver_findings(review_dir: Path, status: dict[str, str], identities_by_case: dict[str, set[str]],
                       all_ids: set[str], findings: list[dict], registry: dict | None = None,
                       resolutions: dict | None = None, context_by_case: dict | None = None) -> None:
    path = review_dir / "disagreements.csv"
    if not path.exists():
        disputed = sorted(i for i, s in status.items() if s == "disputed")
        if disputed:
            _finding(findings, "pending", "disagreements", "no_disagreements_file",
                     f"{len(disputed)} disputed case(s) and no disagreements.csv yet: "
                     "python -m evals.review_status --write-disagreements evals/review/disagreements.csv",
                     case_ids=disputed)
        return
    read = _read_sheet(path)
    if isinstance(read, str):
        _finding(findings, "error", "disagreements", "disagreements_unreadable", f"disagreements.csv: {read}")
        return
    fieldnames, rows = read
    names = {(f or "").split(" ")[0].strip() for f in fieldnames or []}
    absent = [c for c in ("case_id", "resolution", "resolved_by") if c not in names]
    if absent:
        _finding(findings, "error", "disagreements", "disagreements_columns",
                 f"disagreements.csv: missing column(s) {', '.join(absent)}")
        return
    for row in rows:
        case_id = rs._column(row, "case_id")
        if not case_id:
            continue
        resolution, by = rs._column(row, "resolution"), rs._column(row, "resolved_by")
        if case_id not in all_ids:
            _finding(findings, "error", "disagreements", "resolution_unknown_case",
                     f"disagreements.csv lists '{case_id}', which is in no dataset", case_ids=[case_id])
            continue
        if bool(resolution) != bool(by):
            _finding(findings, "error", "disagreements", "resolution_incomplete",
                     f"{case_id}: resolution and resolved_by must both be filled; the case stays disputed",
                     case_ids=[case_id])
        elif resolution and status.get(case_id) not in ("resolved", "excluded"):
            _finding(findings, "warning", "disagreements", "resolution_not_needed",
                     f"{case_id}: has a resolution but its status is '{status.get(case_id, 'unknown')}'; "
                     "it only counts for a case the reviewers disagreed on", case_ids=[case_id])
        if by and not (registry and registry.get("present")):
            tokens = {t.strip().lower() for t in re.split(r"[;,&/]|\band\b", by) if t.strip()}
            overlap = sorted(tokens & identities_by_case.get(case_id, set()))
            if overlap:
                _finding(findings, "warning", "disagreements", "resolver_is_reviewer",
                         f"{case_id}: resolved_by includes reviewer(s) {', '.join(overlap)}. Allowed only when "
                         "no third person exists; note it in the resolution.", case_ids=[case_id])
    structural: list[dict] = []
    rs.load_resolutions(review_dir, structural)
    for item in structural:
        if item["case_id"] in all_ids and item["issue"] != rs.HALF_FILLED:
            _finding(findings, "error", "disagreements", "resolution_record", f"{item['case_id']}: {item['issue']}",
                     case_ids=[item["case_id"]])
    for case_id, resolution in sorted((resolutions or {}).items()):
        context = (context_by_case or {}).get(case_id)
        if context is None or status.get(case_id) not in ("resolved", "disputed"):
            continue
        for problem in rs.resolution_problems(context[0], resolution, context[1], registry):
            _finding(findings, problem["severity"], "disagreements", problem["code"], f"{case_id}: {problem['message']}",
                     case_ids=[case_id])
    unresolved = sorted(i for i, s in status.items() if s == "disputed")
    if unresolved:
        _finding(findings, "pending", "disagreements", "unresolved_disputes",
                 f"{len(unresolved)} disputed case(s) have no complete resolution: {_sample(unresolved)}",
                 case_ids=unresolved)


def run_preflight(datasets: list[Path], review_dir: Path, knowledge_dir: Path | None = None) -> dict:
    """The whole preflight as a plain dict. Read-only."""
    findings: list[dict] = []
    knowledge_dir = DEFAULT_KNOWLEDGE_DIR if knowledge_dir is None else knowledge_dir
    sections = knowledge_sections(knowledge_dir)
    if sections is None:
        _finding(findings, "warning", "global", "knowledge_missing",
                 f"knowledge directory not found or empty ({knowledge_dir}): evidence section names were not checked")
    if not review_dir.is_dir():
        _finding(findings, "pending", "global", "review_dir_missing",
                 f"review directory not found: {review_dir} (no templates or sheets can be read)")

    sheet_paths = sorted(review_dir.glob("sheet_*.csv")) if review_dir.is_dir() else []
    registry = ri.load_registry(review_dir)
    lookup = ri.identity_of(registry)
    for problem in registry["problems"]:
        _finding(findings, "error", "identity", problem["code"], problem["message"], sheet=problem.get("sheet"))
    if not registry["present"]:
        if sheet_paths:
            _finding(findings, "pending", "identity", "identity_unestablished",
                     f"no {ri.REGISTRY_FILE} in the review folder: reviewer identity is only inferred from file names, "
                     "which is not evidence. The review lead declares every reviewer and resolver there before the "
                     "datasets can be reported ready (docs/EVAL_REVIEW_GUIDE.md)")
        else:
            _finding(findings, "info", "identity", "registry_not_created",
                     f"{ri.REGISTRY_FILE} does not exist yet: the review lead creates it when reviewers are assigned")
    else:
        reviewers_declared = any(e["role"] == "reviewer" for e in registry["entries"].values())
        resolvers_declared = any(e["role"] == "resolver" for e in registry["entries"].values())
        if reviewers_declared and not resolvers_declared:
            _finding(findings, "pending", "identity", "no_resolver_registered",
                     f"{ri.REGISTRY_FILE} declares reviewer(s) but no resolver: a dispute between two reviewers cannot "
                     "be adjudicated under the current rules until a resolver (role=resolver, holding no sheet) is added")
        on_disk = {ri.norm_sheet(path.name) for path in sheet_paths}
        for path in sheet_paths:
            if lookup(path.stem[len("sheet_"):]) is None:
                _finding(findings, "error", "identity", "sheet_unregistered",
                         f"{path.name} is not listed in {ri.REGISTRY_FILE}: its author cannot be established, so it counts "
                         "for nothing", sheet=path.stem[len("sheet_"):])
        waiting = sorted(key for entry in registry["entries"].values() for key in entry["sheets"] if key not in on_disk)
        if waiting:
            _finding(findings, "pending", "identity", "registered_sheet_missing",
                     f"{len(waiting)} sheet(s) declared in {ri.REGISTRY_FILE} are not in the folder yet: {_sample(waiting)}")
    if review_dir.is_dir():
        for entry in sorted(review_dir.iterdir()):
            name = entry.name.lower()
            expected = (name.startswith("sheet_") and name.endswith(".csv")) or name.endswith("_blank.csv") \
                or (name.startswith("disagreements") and name.endswith(".csv")) or name == ri.REGISTRY_FILE
            if not expected and not name.startswith("."):
                _finding(findings, "warning", "global", "unexpected_file",
                         f"{entry.name} is not a sheet, template, disagreements file or {ri.REGISTRY_FILE}; it is ignored")
    loaded: list[tuple[Path, list[dict]]] = []
    for path in datasets:
        try:
            loaded.append((path, rs.load_dataset(path)))
        except (OSError, ValueError) as error:
            _finding(findings, "error", rs.dataset_label(path) or path.name, "dataset_unreadable", str(error))
    all_ids = {c["id"] for _, cases in loaded for c in cases}

    sheets_all = rs.load_sheets(review_dir, []) if review_dir.is_dir() else {}
    resolutions = rs.load_resolutions(review_dir, []) if review_dir.is_dir() else {}
    status_all: dict[str, str] = {}
    identities_by_case: dict[str, set[str]] = {}
    context_by_case: dict[str, tuple[str, set[str]]] = {}
    out_datasets = []
    reviewers_by_dataset: dict[str, set[str]] = {}

    for path, cases in loaded:
        label = rs.dataset_label(path) or path.stem
        ids = [c["id"] for c in cases]
        dups = sorted({i for i in ids if ids.count(i) > 1})
        if dups:
            _finding(findings, "error", label, "dataset_duplicate_ids", f"{path.name}: duplicate case ids: {_sample(dups)}",
                     case_ids=dups)
        template = _check_template(_find_template(review_dir, label) if review_dir.is_dir() else None,
                                   label, cases, review_dir, findings)
        analysed = []
        files_by_person: dict[str, list[str]] = {}
        for sheet_path in sheet_paths:
            name = sheet_path.stem[len("sheet_"):]
            if rs.sheet_hint(name) not in (None, rs.dataset_label(path)):
                continue
            item = _analyse_sheet(sheet_path, name, label, cases, all_ids, sections, findings)
            if item is None:
                continue
            person = lookup(name)
            item["reviewer"] = person if person is not None else f"unregistered:{name}"
            analysed.append(item)
            if person is not None:
                files_by_person.setdefault(person, []).append(name)
            if rs.sheet_hint(name) is None:
                _finding(findings, "warning", label, "sheet_name_unlabelled",
                         f"{sheet_path.name} has no -golden/-candidates ending; it is applied to every dataset "
                         "whose case ids it contains. Name sheets sheet_<name>-<golden|candidates>.csv.", sheet=name)
        for person, files in files_by_person.items():
            if len(files) > 1:
                _finding(findings, "error", label, "same_reviewer_twice",
                         f"sheets {', '.join(sorted(files))} belong to one reviewer ('{person}') for the {label} "
                         "dataset. They count as one reviewer.")
        people = sorted(files_by_person)
        reviewers_by_dataset[label] = set(people)
        if not people:
            _finding(findings, "pending", label, "no_reviewer_sheets",
                     f"no reviewer sheet for the {label} dataset yet (expected before the review starts)")
        elif len(people) < MIN_REVIEWERS:
            _finding(findings, "pending", label, "too_few_reviewers",
                     f"only {len(people)} reviewer ({', '.join(people)}) has a sheet for the {label} dataset; "
                     f"{MIN_REVIEWERS} independent reviewers are required")
        _identical_pairs(analysed, label, findings)
        usable = rs.usable_sheets(sheets_all, rs.dataset_label(path))
        status = rs.classify(cases, usable, resolutions, identity_of=lookup, registry=registry)
        status_all.update(status)
        for case in cases:
            case_id = case["id"]
            context_by_case[case_id] = (case.get("kind", "rag_answer"), set(people))
            for reviewer, rows in usable.items():
                if case_id in rows and lookup(reviewer) is not None:
                    identities_by_case.setdefault(case_id, set()).add(lookup(reviewer))
        waiting = sorted(i for i, s in status.items() if s == "single_review")
        if waiting and len(people) >= MIN_REVIEWERS:
            _finding(findings, "pending", label, "awaiting_second_reviewer",
                     f"{len(waiting)} case(s) have only one valid reviewer so far: {_sample(waiting)}", case_ids=waiting)
        assignment_info = None
        if label in ("golden", "candidates"):
            assignments = ra.load_assignments(review_dir / ra.ASSIGNMENTS_FILE) if review_dir.is_dir() \
                else {"present": False, "by_case": {}, "problems": []}
            for message in assignments["problems"]:
                _finding(findings, "error", label, "assignment_malformed", message)
            for message in ra.validate({label: path}, assignments, registry):
                if message not in assignments["problems"]:
                    _finding(findings, "error", label, "assignment_invalid_reference", message)
            cov = ra.coverage({label: path}, assignments["by_case"])[label]
            assignment_info = cov
            if not assignments["present"]:
                _finding(findings, "pending", label, "assignment_not_prepared",
                         f"no {ra.ASSIGNMENTS_FILE} yet: no reviewer has been assigned to any {label} "
                         "in-scope case (python -m evals.review_assign prepares it from reviewers.csv)")
            elif cov["unassigned"]:
                _finding(findings, "pending", label, "assignment_missing_for_case",
                         f"{len(cov['unassigned'])} in-scope {label} case(s) have no assignment yet: "
                         f"{_sample(cov['unassigned'])}", case_ids=cov["unassigned"])
            if cov["single_reviewer_only"]:
                _finding(findings, "pending", label, "assignment_single_reviewer",
                         f"{len(cov['single_reviewer_only'])} in-scope {label} case(s) are assigned to only one "
                         f"reviewer so far: {_sample(cov['single_reviewer_only'])}", case_ids=cov["single_reviewer_only"])
        out_datasets.append({
            "dataset": path.name, "label": label, "cases": len(cases), "fingerprint": rs.fingerprint(path),
            "template": template, "reviewers": people,
            "sheets": [{k: v for k, v in item.items() if not k.startswith("_")} for item in analysed],
            "case_status": {s: sum(1 for v in status.values() if v == s) for s in rs.STATUSES},
            "assignment": assignment_info,
        })

    labels = sorted(reviewers_by_dataset)
    if len(labels) > 1:
        everyone = set().union(*reviewers_by_dataset.values())
        for person in sorted(everyone):
            lacking = [lab for lab in labels if person not in reviewers_by_dataset[lab]]
            if lacking:
                _finding(findings, "info", "global", "reviewer_coverage",
                         f"reviewer '{person}' has no sheet for the {', '.join(lacking)} dataset")
    if review_dir.is_dir():
        _resolver_findings(review_dir, status_all, identities_by_case, all_ids, findings, registry, resolutions, context_by_case)

    policy = rp.status(review_dir)
    for decision_id in policy["open"]:
        info = next(d for d in policy["decisions"] if d["id"] == decision_id)
        _finding(findings, "info", "policy", "policy_open",
                 f"decision {decision_id} ({info['title']}) is still open: {info['needs']}")

    counts = {sev: sum(1 for f in findings if f["severity"] == sev) for sev in ("error", "pending", "warning", "info")}
    verdict = "errors" if counts["error"] else "incomplete" if counts["pending"] else "clean"
    start_blockers = [f for f in findings if f["severity"] == "error"
                      and f["check"] in ("template_missing", "template_unreadable", "template_header",
                                         "template_cases", "template_filled", "dataset_unreadable",
                                         "dataset_duplicate_ids")]
    return {
        "read_only": True,
        "review_dir": str(review_dir),
        "knowledge_dir": str(knowledge_dir),
        "datasets": out_datasets,
        "findings": findings,
        "registry": {"present": registry["present"],
                     "identities": {rid: e["role"] for rid, e in sorted(registry["entries"].items())}},
        "policy": {"open": policy["open"], "file_present": policy["file_present"]},
        "counts": counts,
        "verdict": verdict,
        "can_start_review": not start_blockers and bool(loaded),
        "note": "Read-only. A clean preflight means the files are well formed, not that the labels are right or "
                "that the dataset is ready: readiness is decided only by python -m evals.review_status.",
    }


def format_text(report: dict) -> str:
    c = report["counts"]
    lines = ["Review preflight (read-only)",
             f"review dir: {report['review_dir']}   knowledge dir: {report['knowledge_dir']}",
             f"verdict: {report['verdict']}  ({c['error']} error, {c['pending']} pending, {c['warning']} warning, {c['info']} info)",
             f"can start review: {'yes' if report['can_start_review'] else 'NO - fix the errors in the templates or datasets first'}"]
    for d in report["datasets"]:
        t = d["template"]
        lines += ["", f"{d['dataset']}  ({d['cases']} cases, fingerprint {d['fingerprint']})",
                  f"  blank template: {t['path'] or 'missing'} - " + (f"OK ({t['rows']} rows, nothing filled in)" if t["ok"] else "PROBLEM"),
                  f"  reviewers with a sheet: {', '.join(d['reviewers']) or 'none'}"]
        for s in d["sheets"]:
            lines.append(f"  sheet {s['sheet']}: {s['complete']} complete, {s['blank']} blank, {s['malformed']} malformed, "
                         f"{s['missing']} missing, {s['unknown']} unknown id(s)")
        lines.append("  case status: " + ", ".join(f"{k} {v}" for k, v in d["case_status"].items()))
    order = {"error": 0, "pending": 1, "warning": 2, "info": 3}
    if report["findings"]:
        lines.append("")
        for f in sorted(report["findings"], key=lambda f: order[f["severity"]]):
            lines.append(f"{f['severity'].upper():8} [{f['scope']}] {f['message']}")
    lines += ["", report["note"]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dataset", action="append", default=None,
                        help="dataset .jsonl (repeatable; default: golden and candidates)")
    parser.add_argument("--review-dir", default=str(rs.DEFAULT_REVIEW_DIR))
    parser.add_argument("--knowledge-dir", default=str(DEFAULT_KNOWLEDGE_DIR))
    parser.add_argument("--json", action="store_true", help="print the full report as JSON instead of text")
    parser.add_argument("--strict", action="store_true", help="exit 1 on pending work too, not only on errors")
    args = parser.parse_args(argv)
    paths = [Path(p) for p in (args.dataset or ["evals/datasets/nimbuscrm_golden_v1.jsonl",
                                                "evals/datasets/nimbuscrm_candidates_v2.jsonl"])]
    missing = [p for p in paths if not p.exists()]
    if missing:
        print(f"error: dataset not found: {', '.join(map(str, missing))}", file=sys.stderr)
        return 2
    report = run_preflight(paths, Path(args.review_dir), Path(args.knowledge_dir))
    print(json.dumps(report, indent=2) if args.json else format_text(report))
    counts = report["counts"]
    if counts["error"] or (args.strict and counts["pending"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
