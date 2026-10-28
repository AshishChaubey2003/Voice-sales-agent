"""Baseline management.

    python -m evals.baseline set <run_id> <name>      save a run's metrics as a baseline
    python -m evals.baseline compare <run_id> <name>  compare a run with a baseline
    python -m evals.baseline list                     list saved baselines

A baseline is only a copy of a run's metadata and aggregate metrics (no case text).
Choose it deliberately: an unreviewed first run is a starting point, not a standard.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from evals import compare, store

DEFAULT_RESULTS = "evals/results"
DEFAULT_BASELINES = "evals/baselines"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.baseline")
    parser.add_argument("--results-dir", default=DEFAULT_RESULTS)
    parser.add_argument("--baselines-dir", default=DEFAULT_BASELINES)
    sub = parser.add_subparsers(dest="command", required=True)
    set_cmd = sub.add_parser("set")
    set_cmd.add_argument("run_id")
    set_cmd.add_argument("name")
    cmp_cmd = sub.add_parser("compare")
    cmp_cmd.add_argument("run_id")
    cmp_cmd.add_argument("name")
    cmp_cmd.add_argument("--rate-tolerance", type=float, default=compare.DEFAULT_RATE_TOLERANCE)
    cmp_cmd.add_argument("--latency-tolerance", type=float, default=compare.DEFAULT_LATENCY_TOLERANCE)
    sub.add_parser("list")
    args = parser.parse_args(argv)

    if args.command == "list":
        root = Path(args.baselines_dir)
        names = sorted(p.stem for p in root.glob("*.json")) if root.is_dir() else []
        print("\n".join(names) if names else "No baselines saved.")
        return 0

    run = store.load_run(args.results_dir, args.run_id)
    if run is None:
        print(f"error: run '{args.run_id}' not found in {args.results_dir}", file=sys.stderr)
        return 2
    try:
        if args.command == "set":
            if run.get("status") != "completed":
                print(f"error: run status is '{run.get('status')}'; only completed runs can be baselines", file=sys.stderr)
                return 2
            path = store.save_baseline(args.baselines_dir, args.name, run)
            print(f"Saved baseline '{args.name}' from run {args.run_id}: {path}")
            return 0
        baseline = store.load_baseline(args.baselines_dir, args.name)
        if baseline is None:
            print(f"error: baseline '{args.name}' not found", file=sys.stderr)
            return 2
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    result = compare.compare_runs(
        run, baseline, rate_tolerance=args.rate_tolerance, latency_tolerance=args.latency_tolerance
    )
    for warning in result["warnings"]:
        print(f"warning: {warning}")
    for row in result["metrics"]:
        if row["status"] in ("regression", "improvement"):
            print(f"{row['status']:<12} {row['name']}: {row['baseline']} -> {row['current']}")
    print("Regressions: " + (", ".join(result["regressions"]) or "none"))
    return 1 if result["regressions"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
