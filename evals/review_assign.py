"""Prepare (never perform) the reviewer-assignment for the knowledge-base cases (read-mostly).

An ASSIGNMENT says who is meant to review a case. It is not a COMPLETED REVIEW: that is a row in
evals/review/sheet_<reviewer>-<dataset>.csv written by the reviewer themselves. This module only ever
writes evals/review/assignments.csv; it never writes a sheet and never invents a judgment.

Scope: only kind == "rag_answer" cases are in scope (51 golden + 60 candidates = 111), because that is
what the schema (evals/schema.py) calls a knowledge-base answer case. The 18 lead_gate and 6 lead_llm
golden cases are lead/consent cases and are never written to assignments.csv - not by filtering out a
hard-coded id list, but because they are never in the in-scope set to begin with. Decision A (the
lead-consent policy, see evals/review_policy.py) covers them and stays untouched here.

    python -m evals.review_assign --golden evals/datasets/nimbuscrm_golden_v1.jsonl \\
        --candidates evals/datasets/nimbuscrm_candidates_v2.jsonl --review-dir evals/review

Reviewers come ONLY from evals/review/reviewers.csv (role=reviewer), written by the review lead; this
module invents no identities. Each in-scope case is assigned to reviewers_per_case (default 2) distinct
registered reviewers, picked round-robin in reviewer_id order so the plan is reproducible from the same
registry and dataset. Rerunning is always safe:
  * a case that already has one or more assignment rows is left exactly as it is - never rewritten,
    never topped up, never removed, however many reviewers it names;
  * only cases with zero rows get new ones;
  * the dataset's own content is never read for anything but deriving case ids - no row here carries a
    label, a judgment or a decision.
--check reports status only and writes nothing, including when --out does not exist yet.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from evals.review_identity import load_registry
from evals.review_status import dataset_label, fingerprint, load_dataset

ASSIGNMENTS_FILE = "assignments.csv"
COLUMNS = ("dataset", "case_id", "reviewer_id", "assigned_by", "assigned_on")
IN_SCOPE_KIND = "rag_answer"


def in_scope_case_ids(dataset_path: Path) -> list[str]:
    """Case ids with kind == 'rag_answer', in file order. Never hard-coded; always read fresh."""
    return [case["id"] for case in load_dataset(dataset_path) if case.get("kind") == IN_SCOPE_KIND]


def load_assignments(path: Path) -> dict:
    """{"present","by_case": {(dataset,case_id): [reviewer_id,...]}, "rows": [...], "problems": [str]}."""
    out = {"present": path.exists(), "by_case": {}, "rows": [], "problems": []}
    if not out["present"]:
        return out
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            names = [(f or "").strip() for f in (reader.fieldnames or [])]
            rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        out["problems"].append(f"{path.name} cannot be read ({type(error).__name__}); treated as empty")
        return out
    absent = [c for c in COLUMNS if c not in names]
    if absent:
        out["problems"].append(f"{path.name} is missing column(s): {', '.join(absent)}; treated as empty")
        return out
    seen = set()
    for number, row in enumerate(rows, start=2):
        if not any((v or "").strip() for v in row.values()):
            continue
        cell = {c: (row.get(c) or "").strip() for c in COLUMNS}
        key = (cell["dataset"], cell["case_id"])
        pair = (key, cell["reviewer_id"])
        where = f"{path.name} line {number}"
        if not cell["dataset"] or not cell["case_id"] or not cell["reviewer_id"]:
            out["problems"].append(f"{where}: dataset, case_id and reviewer_id must all be filled")
            continue
        if pair in seen:
            out["problems"].append(f"{where}: {cell['dataset']}/{cell['case_id']} is already assigned to "
                                    f"'{cell['reviewer_id']}'; the duplicate row is ignored")
            continue
        seen.add(pair)
        out["by_case"].setdefault(key, []).append(cell["reviewer_id"])
        out["rows"].append(cell)
    for key, reviewers in out["by_case"].items():
        if len(reviewers) != len(set(reviewers)):
            out["problems"].append(f"{key[0]}/{key[1]} names the same reviewer more than once: {reviewers}")
    return out


def plan(datasets: dict[str, Path], existing_by_case: dict, registry: dict,
          reviewers_per_case: int = 2) -> tuple[list[dict], list[str]]:
    """New rows for cases with zero existing assignments. Never touches a case that has any row already."""
    problems = []
    reviewer_ids = sorted(rid for rid, e in registry.get("entries", {}).items() if e["role"] == "reviewer")
    if len(reviewer_ids) < reviewers_per_case:
        problems.append(f"only {len(reviewer_ids)} registered reviewer(s) ({', '.join(reviewer_ids) or 'none'}); "
                        f"{reviewers_per_case} are needed per case. No new assignments were planned.")
        return [], problems
    new_rows: list[dict] = []
    cursor = 0
    for label, dataset_path in datasets.items():
        for case_id in in_scope_case_ids(dataset_path):
            key = (label, case_id)
            if key in existing_by_case:
                continue
            chosen, i = [], cursor
            while len(chosen) < reviewers_per_case:
                candidate = reviewer_ids[i % len(reviewer_ids)]
                if candidate not in chosen:
                    chosen.append(candidate)
                i += 1
            cursor += 1
            for reviewer_id in chosen:
                new_rows.append({"dataset": label, "case_id": case_id, "reviewer_id": reviewer_id,
                                 "assigned_by": "review_assign (round-robin)", "assigned_on": ""})
    return new_rows, problems


def coverage(datasets: dict[str, Path], by_case: dict) -> dict:
    """Per dataset: in-scope case count, how many have >=1 / >=2 distinct reviewers, which have none."""
    report = {}
    for label, dataset_path in datasets.items():
        ids = in_scope_case_ids(dataset_path)
        zero = [c for c in ids if (label, c) not in by_case or not by_case[(label, c)]]
        one = [c for c in ids if (label, c) in by_case and len(set(by_case[(label, c)])) == 1]
        report[label] = {"in_scope": len(ids), "fingerprint": fingerprint(dataset_path),
                         "unassigned": sorted(zero), "single_reviewer_only": sorted(one),
                         "assigned_with_2plus": len(ids) - len(zero) - len(one)}
    return report


def validate(datasets: dict[str, Path], assignments: dict, registry: dict) -> list[str]:
    """Reviewer-reference and scope problems in assignments.csv, beyond parsing (load_assignments)."""
    problems = list(assignments["problems"])
    in_scope = {label: set(in_scope_case_ids(p)) for label, p in datasets.items()}
    reviewers = registry.get("entries", {})
    for (label, case_id), reviewer_ids in assignments["by_case"].items():
        if label not in in_scope:
            problems.append(f"{label}/{case_id}: '{label}' is not a known dataset label")
            continue
        if case_id not in in_scope[label]:
            problems.append(f"{label}/{case_id}: not an in-scope rag_answer case id in that dataset "
                            "(stale, mistyped, or a lead/consent case that must stay out of scope)")
        for rid in reviewer_ids:
            entry = reviewers.get(rid)
            if entry is None:
                problems.append(f"{label}/{case_id}: reviewer_id '{rid}' is not in reviewers.csv")
            elif entry["role"] != "reviewer":
                problems.append(f"{label}/{case_id}: '{rid}' is registered as {entry['role']}, not reviewer")
    return problems


def write_new_assignments(review_dir: Path, datasets: dict[str, Path], reviewers_per_case: int = 2) -> dict:
    """Append newly planned rows to assignments.csv. Existing rows are byte-for-byte preserved."""
    review_dir = Path(review_dir)
    target = review_dir / ASSIGNMENTS_FILE
    registry = load_registry(review_dir)
    existing = load_assignments(target)
    report = {"registry_problems": [p["message"] for p in registry["problems"]],
             "existing_problems": validate(datasets, existing, registry), "new_rows": 0, "wrote": False}
    if not registry["present"]:
        report["registry_problems"].append("reviewers.csv does not exist; nothing can be assigned yet")
        return report
    if report["registry_problems"] or any("reviewer_id" in p or "role" in p for p in report["existing_problems"]):
        report["blocked"] = "registry or existing-assignment problems must be fixed before adding more rows"
        return report
    new_rows, plan_problems = plan(datasets, existing["by_case"], registry, reviewers_per_case)
    report["plan_problems"] = plan_problems
    if not new_rows:
        return report
    write_header = not target.exists()
    with target.open("a", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        if write_header:
            writer.writeheader()
        writer.writerows(new_rows)
    report["new_rows"] = len(new_rows)
    report["wrote"] = True
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--golden", type=Path)
    parser.add_argument("--candidates", type=Path)
    parser.add_argument("--review-dir", type=Path, default=Path("evals/review"))
    parser.add_argument("--reviewers-per-case", type=int, default=2)
    parser.add_argument("--check", action="store_true", help="report only; write nothing")
    args = parser.parse_args(argv)

    datasets = {}
    for label, path in (("golden", args.golden), ("candidates", args.candidates)):
        if path is None:
            continue
        if not path.exists():
            print(f"error: {label} dataset not found: {path}", file=sys.stderr)
            return 2
        if dataset_label(path) not in (None, label):
            print(f"error: {path} does not look like a '{label}' dataset by file name", file=sys.stderr)
            return 2
        datasets[label] = path
    if not datasets:
        print("error: pass --golden and/or --candidates", file=sys.stderr)
        return 2

    if args.check:
        registry = load_registry(args.review_dir)
        assignments = load_assignments(args.review_dir / ASSIGNMENTS_FILE)
        problems = [p["message"] for p in registry["problems"]] + validate(datasets, assignments, registry)
        for label, info in coverage(datasets, assignments["by_case"]).items():
            print(f"{label}: {info['in_scope']} in scope, {info['assigned_with_2plus']} have 2+ distinct "
                  f"reviewers, {len(info['single_reviewer_only'])} have only 1, {len(info['unassigned'])} unassigned")
        for message in problems:
            print(f"problem: {message}")
        return 1 if problems else 0

    report = write_new_assignments(args.review_dir, datasets, args.reviewers_per_case)
    for message in report["registry_problems"] + report["existing_problems"] + report.get("plan_problems", []):
        print(f"problem: {message}")
    if report.get("blocked"):
        print(f"blocked: {report['blocked']}")
        return 2
    print(f"wrote {report['new_rows']} new assignment row(s)" if report["wrote"] else "no new assignment rows needed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
