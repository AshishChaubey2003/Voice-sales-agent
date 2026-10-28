"""Create a BLANK reviewer sheet for a dataset (stable case ids, no expected labels shown).

    python -m evals.review_sheets --dataset evals/datasets/nimbuscrm_golden_v1.jsonl \\
        --out evals/review/golden_review_sheet_blank.csv

The sheet shows the conversation (and, for consent-gate cases, the scripted save request) but hides the
author's expected sources, facts and outcomes, so a reviewer's labels are independent. The dataset is
only read. An existing file is never overwritten.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from evals.review_status import load_dataset

COLUMNS = ["case_id", "kind", "category", "conversation", "proposed_tool_call",
           "answerable_from_kb (yes/no/partly)", "evidence_sections (separate with ;)",
           "expected_behavior (answer/correct_premise/decline_or_abstain/redirect_to_sales/other)",
           "should_save (yes/no)", "ambiguous_or_exclude (keep/ambiguous/exclude)", "comment"]


def _conversation(case: dict) -> str:
    return " || ".join(f"{m['role']}: {m['content']}" for m in case["messages"])


def build_rows(cases: list[dict]) -> list[list[str]]:
    rows = []
    for case in cases:
        tool = case.get("proposed_tool_call")
        rows.append([case["id"], case["kind"], case.get("category", ""), _conversation(case),
                     json.dumps(tool, ensure_ascii=False) if tool else "", "", "", "", "", "", ""])
    return rows


def write_blank(dataset: Path, target: Path) -> int:
    if target.exists():
        raise FileExistsError(f"{target} already exists; choose another name (existing files are never overwritten)")
    rows = build_rows(load_dataset(dataset))
    with target.open("x", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        writer.writerows(rows)
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    dataset = Path(args.dataset)
    if not dataset.exists():
        print(f"error: dataset not found: {dataset}", file=sys.stderr)
        return 2
    try:
        count = write_blank(dataset, Path(args.out))
    except (FileExistsError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"wrote {count} blank row(s) to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
