"""Offline comparison of retrieval accept/reject rules on saved diagnostic data.

Read-only. It makes no database, embedding or LLM call, reads no configuration,
and changes neither retrieval nor the 0.30 gate. Inputs:

* diagnostic rows: the `--json` output of `python -m evals.diagnose_retrieval`, or
  the transcribed fixture evals/fixtures/retrieval_diagnostic_20261026.json
* the dataset, which is the only source of labels. A case with expected_sources
  is "answerable"; one without is "unanswerable". Labels are never edited here.

Question each rule answers: "does retrieval have usable context for this query?"
Positive = answerable (should accept). TP answerable accepted, FN answerable
rejected, FP unanswerable accepted, TN unanswerable rejected.

Limits (printed in the report too): 19 cases, none human-reviewed, the groups were
formed after the distances were seen, only the top 3 vector candidates were saved,
and the "exploratory" rules are in-sample. Nothing here is generalizable
performance, and no rule here is a recommendation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_INPUT = "evals/fixtures/retrieval_diagnostic_20261026.json"
DEFAULT_DATASET = "evals/datasets/nimbuscrm_golden_v1.jsonl"
DEFAULT_GATE = 0.30
# Label is the dataset author's guess and the right behaviour is unclear
# (an injection attempt whose question still touches the Discounts section).
UNCERTAIN_LABELS = frozenset({"inj-discount"})
# Cases the user asked to watch as false-accept risks.
WATCHLIST = ("unans-enterprise", "inj-free-pro", "unans-api", "unans-ceo", "unans-salesforce")
EXPLORATORY_CUTOFFS = (0.35, 0.40, 0.45)


# --- inputs ----------------------------------------------------------------


def load_rows(path: str | Path) -> tuple[list[dict], dict]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        return raw["rows"], raw.get("provenance", {})
    return raw, {}


def to_case(row: dict) -> dict:
    """The few signals the rules may use, taken from a diagnostic row."""
    candidates = sorted(row["analysis"]["candidates"], key=lambda c: c["distance"])
    keyword = list(row.get("keyword_headings") or [])
    return {
        "case_id": row["case_id"],
        "top_distance": candidates[0]["distance"] if candidates else None,
        "vector_top": [c["heading"] for c in candidates[:3]],
        "keyword_headings": keyword,
        "keyword_count": len(keyword),
    }


def load_labels(dataset_path: str | Path) -> dict[str, dict]:
    from evals.schema import load_dataset

    return {
        case.id: {"answerable": bool(case.expected_sources), "category": case.category}
        for case in load_dataset(dataset_path).cases
    }


# --- rules (each returns True = accept) --------------------------------------


def distance_ok(case: dict, gate: float) -> bool:
    return case["top_distance"] is not None and case["top_distance"] <= gate


def keyword_ok(case: dict) -> bool:
    return case["keyword_count"] >= 1


def keyword_top_in_vector_top(case: dict) -> bool:
    """Exploratory: the best keyword hit is also among the 3 nearest vector chunks."""
    return bool(case["keyword_headings"]) and case["keyword_headings"][0] in case["vector_top"]


def build_rules(gate: float) -> list[dict]:
    rules = [
        {"name": "1 distance only (<= gate)", "exploratory": False, "fn": lambda c: distance_ok(c, gate)},
        {"name": "2 keyword only (>=1 match)", "exploratory": False, "fn": keyword_ok},
        {"name": "3 distance AND keyword", "exploratory": False, "fn": lambda c: distance_ok(c, gate) and keyword_ok(c)},
        {"name": "4 distance OR keyword", "exploratory": False, "fn": lambda c: distance_ok(c, gate) or keyword_ok(c)},
        {"name": "5a keyword top hit in vector top-3", "exploratory": True, "fn": keyword_top_in_vector_top},
    ]
    for cutoff in EXPLORATORY_CUTOFFS:
        rules.append(
            {
                "name": f"5b distance <= {cutoff:.2f} only",
                "exploratory": True,
                "fn": lambda c, x=cutoff: distance_ok(c, x),
            }
        )
        rules.append(
            {
                "name": f"5c distance <= {cutoff:.2f} AND keyword",
                "exploratory": True,
                "fn": lambda c, x=cutoff: distance_ok(c, x) and keyword_ok(c),
            }
        )
    return rules


# --- metrics -----------------------------------------------------------------


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def confusion(decisions: dict[str, bool], labels: dict[str, bool]) -> dict:
    tp = sum(1 for k, accepted in decisions.items() if accepted and labels[k])
    fn = sum(1 for k, accepted in decisions.items() if not accepted and labels[k])
    fp = sum(1 for k, accepted in decisions.items() if accepted and not labels[k])
    tn = sum(1 for k, accepted in decisions.items() if not accepted and not labels[k])
    precision, recall, specificity = _ratio(tp, tp + fp), _ratio(tp, tp + fn), _ratio(tn, tn + fp)
    balanced = None if recall is None or specificity is None else (recall + specificity) / 2
    return {
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "precision": precision, "recall": recall, "specificity": specificity,
        "balanced_accuracy": balanced,
    }


def analyze(cases: list[dict], labels: dict[str, dict], gate: float) -> dict:
    missing = [c["case_id"] for c in cases if c["case_id"] not in labels]
    if missing:
        raise ValueError(f"cases not in the dataset (no label): {', '.join(missing)}")
    answerable = {c["case_id"]: labels[c["case_id"]]["answerable"] for c in cases}
    out = {"gate": gate, "n_cases": len(cases), "rules": []}
    for rule in build_rules(gate):
        decisions = {c["case_id"]: bool(rule["fn"](c)) for c in cases}
        certain = {k: v for k, v in decisions.items() if k not in UNCERTAIN_LABELS}
        out["rules"].append(
            {
                "id": rule["name"].split()[0] + (f".{round(float(rule['name'].split('<= ')[1].split()[0]) * 100)}" if "<= 0." in rule["name"] and not rule["name"].startswith("1") else ""),
                "name": rule["name"],
                "exploratory": rule["exploratory"],
                "decisions": decisions,
                "all_cases": confusion(decisions, answerable),
                "without_uncertain": confusion(certain, {k: answerable[k] for k in certain}),
                "false_accepts": sorted(k for k, d in decisions.items() if d and not answerable[k]),
                "false_rejects": sorted(k for k, d in decisions.items() if not d and answerable[k]),
            }
        )
    return out


# --- rendering ---------------------------------------------------------------


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def render(result: dict, cases: list[dict], labels: dict[str, dict], provenance: dict) -> str:
    gate = result["gate"]
    n_ans = sum(1 for c in cases if labels[c["case_id"]]["answerable"])
    lines = [
        f"Offline rule comparison: {result['n_cases']} cases ({n_ans} answerable, {result['n_cases'] - n_ans} unanswerable/off-topic). Gate = {gate} (cosine DISTANCE, lower = closer).",
        "Positive = answerable. Read-only; no retrieval, LLM or database call. NOT generalizable: unreviewed labels, in-sample, tiny n.",
    ]
    if provenance.get("source"):
        lines.append(f"Data: {provenance['source']}")
    for title, key in (("ALL CASES", "all_cases"), ("EXCLUDING UNCERTAIN LABEL(S): " + ", ".join(sorted(UNCERTAIN_LABELS)), "without_uncertain")):
        lines += ["", title, f"{'rule':<38}{'TP':>4}{'FP':>4}{'TN':>4}{'FN':>4}{'prec':>7}{'recall':>8}{'spec':>7}{'bal.acc':>9}"]
        for exploratory in (False, True):
            if exploratory:
                lines.append("  -- exploratory (in-sample, not recommendations) --")
            for rule in result["rules"]:
                if rule["exploratory"] != exploratory:
                    continue
                m = rule[key]
                lines.append(
                    f"{rule['name']:<38}{m['tp']:>4}{m['fp']:>4}{m['tn']:>4}{m['fn']:>4}"
                    f"{_fmt(m['precision']):>7}{_fmt(m['recall']):>8}{_fmt(m['specificity']):>7}{_fmt(m['balanced_accuracy']):>9}"
                )
    lines += ["", "PER-CASE ERRORS (all cases)"]
    for rule in result["rules"]:
        tag = " [exploratory]" if rule["exploratory"] else ""
        lines.append(f"{rule['name']}{tag}")
        lines.append(f"   false accepts: {', '.join(rule['false_accepts']) or 'none'}")
        lines.append(f"   false rejects: {', '.join(rule['false_rejects']) or 'none'}")
    lines += ["", "WATCHLIST: accepted (A) / rejected (R) per rule, for the main false-accept risks"]
    header = f"{'case':<20}{'top dist':>9}{'kw n':>6}  " + " ".join(f"{r['id']:>6}" for r in result["rules"])
    lines.append(header)
    by_id = {c["case_id"]: c for c in cases}
    for case_id in WATCHLIST:
        c = by_id.get(case_id)
        if c is None:
            lines.append(f"{case_id:<20}  (not in input)")
            continue
        marks = " ".join(f"{('A' if r['decisions'][case_id] else 'R'):>6}" for r in result["rules"])
        lines.append(f"{case_id:<20}{c['top_distance']:>9.3f}{c['keyword_count']:>6}  {marks}")
    lines += [
        "",
        "LIMITS",
        "- Labels come from the dataset and no case is human-reviewed. inj-discount is reported with and without.",
        "- Groups were formed after seeing these distances; rules 1-4 are fixed in advance, 5x are illustrative sweeps on the same cases.",
        "- Only the top-3 vector candidates and the keyword-hit lists were saved, so nothing deeper (e.g. score margins past rank 3) is analysed.",
        "- A keyword rule rejects valid questions phrased with synonyms (e.g. 'Outlook' for email); none of these cases test that.",
        "- A stricter rule lowers unsupported answers but raises valid rejections; this data cannot price that trade-off.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare retrieval accept/reject rules on saved diagnostic data (read-only).")
    parser.add_argument(
        "--input", action="append",
        help="diagnose_retrieval --json output or the transcribed fixture; repeat to combine files (default: the fixture)",
    )
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--gate", type=float, default=None, help="defaults to the gate recorded in the input, else 0.30")
    parser.add_argument("--json-out", help="write the results to this NEW file")
    args = parser.parse_args(argv)

    rows, provenance = [], {}
    for path in args.input or [DEFAULT_INPUT]:
        part, part_provenance = load_rows(path)
        rows += part
        provenance = provenance or part_provenance
    ids = [row["case_id"] for row in rows]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    if duplicated:
        print(f"case(s) appear in more than one input: {', '.join(duplicated)}", file=sys.stderr)
        return 2
    if not provenance:
        provenance = {"source": "diagnose_retrieval --json output: " + "; ".join(args.input or [])}
    gate = args.gate
    if gate is None:
        gate = provenance.get("gate") or (rows[0]["analysis"].get("gate") if rows else None) or DEFAULT_GATE
    cases = [to_case(row) for row in rows]
    labels = load_labels(args.dataset)
    try:
        result = analyze(cases, labels, float(gate))
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    print(render(result, cases, labels, provenance))
    if args.json_out:
        target = Path(args.json_out)
        if target.exists():
            print(f"Refusing to overwrite {target}", file=sys.stderr)
            return 2
        target.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
