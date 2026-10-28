"""Turns per-case results into run-level metrics.

A metric is reported as {"value", "n", "direction", "unit"}. `n` is the number of
cases the value was computed from; a metric with n == 0 has value None and is
never shown as zero.
"""

from __future__ import annotations

import re
from collections import defaultdict

from evals import metrics as m

# score key -> (reported name, direction)
_SCORE_METRICS = {
    "recall_at_1": ("retrieval.recall_at_1", "higher"),
    "recall_at_3": ("retrieval.recall_at_3", "higher"),
    "recall_at_4": ("retrieval.recall_at_4", "higher"),
    "precision_at_1": ("retrieval.precision_at_1", "higher"),
    "precision_at_3": ("retrieval.precision_at_3", "higher"),
    "precision_at_4": ("retrieval.precision_at_4", "higher"),
    "mrr": ("retrieval.mrr", "higher"),
    "empty_retrieval": ("retrieval.empty_rate", "lower"),
    "empty_when_unanswerable": ("retrieval.empty_when_unanswerable", "higher"),
    "required_facts": ("answer.required_facts", "higher"),
    "forbidden_clear": ("answer.forbidden_clear", "higher"),
    "abstained": ("answer.abstains_when_unknown", "higher"),
    "plain_text": ("answer.plain_text", "higher"),
    "within_length": ("answer.within_prompt_length", "higher"),
    "gate_correct": ("lead.gate_correct", "higher"),
    "gate_false_save": ("lead.gate_false_save", "lower"),
    "gate_false_reject": ("lead.gate_false_reject", "lower"),
    "lead_outcome_correct": ("lead.llm_outcome_correct", "higher"),
    "premature_tool_call": ("lead.premature_tool_call", "lower"),
    "lead_email_correct": ("lead.email_correct", "higher"),
    "no_contact_promise": ("lead.no_contact_promise", "higher"),
    "false_lead_save": ("lead.false_save", "lower"),
    "missed_lead_save": ("lead.missed_save", "lower"),
}
_AT_K = re.compile(r"^(recall|precision)_at_(\d+)$")


def _score_metric(key: str):
    """(reported name, direction) for a score key; Recall@K / Precision@K exist for any configured K."""
    if key in _SCORE_METRICS:
        return _SCORE_METRICS[key]
    if _AT_K.match(key):
        return f"retrieval.{key}", "higher"
    return None


_JUDGE_METRICS = {
    "faithfulness": "judge.faithfulness",
    "relevance": "judge.relevance",
    "correctness": "judge.correctness",
}
_LATENCY_FIELDS = ("wall_ms", "llm_ms", "retrieval_ms")


def _metric(values: list, direction: str, unit: str = "ratio") -> dict:
    present = [v for v in values if v is not None]
    return {"value": m.mean(present), "n": len(present), "direction": direction, "unit": unit}


def build_metrics(results: list[dict]) -> dict:
    scored = [r for r in results if r.get("status") == "ok"]
    collected: dict[str, list] = defaultdict(list)
    directions: dict[str, str] = {}

    for result in scored:
        for key, value in (result.get("scores") or {}).items():
            mapped = _score_metric(key)
            if mapped:
                name, direction = mapped
                collected[name].append(value)
                directions[name] = direction
        verdict = result.get("judge")
        if verdict and verdict.get("ok"):
            for key, name in _JUDGE_METRICS.items():
                if key not in verdict["scores"]:
                    continue  # e.g. correctness skipped because the case has no reviewed reference
                collected[name].append(verdict["scores"][key])
                directions[name] = "higher"

    out = {name: _metric(values, directions[name]) for name, values in sorted(collected.items())}

    ran = [r for r in results if r.get("status") in ("ok", "error")]
    errors = sum(1 for r in ran if r["status"] == "error")
    passed = [r for r in ran if r.get("passed") is True]
    out["run.error_rate"] = {
        "value": errors / len(ran) if ran else None,
        "n": len(ran),
        "direction": "lower",
        "unit": "ratio",
    }
    out["run.pass_rate"] = {
        "value": len(passed) / len(ran) if ran else None,
        "n": len(ran),
        "direction": "higher",
        "unit": "ratio",
    }
    return out


def build_latency(results: list[dict]) -> dict:
    latency = {}
    for field_name in _LATENCY_FIELDS:
        values = [
            r["output"][field_name]
            for r in results
            if r.get("status") == "ok"
            and isinstance(r.get("output"), dict)
            and isinstance(r["output"].get(field_name), (int, float))
        ]
        latency[field_name] = m.latency_summary(values)
    return latency


def build_breakdowns(results: list[dict]) -> dict:
    def group(key: str) -> dict:
        buckets: dict[str, list[dict]] = defaultdict(list)
        for result in results:
            if result.get("status") in ("ok", "error"):
                buckets[result[key]].append(result)
        return {
            name: {
                "n": len(items),
                "passed": sum(1 for r in items if r.get("passed") is True),
                "errors": sum(1 for r in items if r["status"] == "error"),
                "pass_rate": sum(1 for r in items if r.get("passed") is True) / len(items),
            }
            for name, items in sorted(buckets.items())
        }

    return {"by_category": group("category"), "by_kind": group("kind")}


def build_counts(results: list[dict]) -> dict:
    ran = [r for r in results if r.get("status") in ("ok", "error")]
    return {
        "total": len(results),
        "ran": len(ran),
        "passed": sum(1 for r in ran if r.get("passed") is True),
        "failed": sum(1 for r in ran if r.get("passed") is False),
        "errors": sum(1 for r in ran if r["status"] == "error"),
        "skipped": sum(1 for r in results if r.get("status") == "skipped"),
        "known_gap_failures": sum(
            1 for r in ran if r.get("known_gap") and r.get("passed") is False
        ),
        # Cases with at least one failed judge call: the legacy --judge or any --judge-suite
        # evaluator that ended in status "error". Skipped evaluators are not errors.
        "judge_errors": sum(
            1 for r in ran
            if (r.get("judge") and not r["judge"].get("ok"))
            or any(e.get("status") == "error" for e in r.get("evaluators") or [])
        ),
        "evaluator_errors": sum(
            1 for r in ran for e in r.get("evaluators") or [] if e.get("status") == "error"
        ),
        # Real scores only (status ok with a numeric score), split by who produced them, so a
        # deterministic check is never reported as an LLM-judge result. Skips/errors are separate.
        "evaluator_scores_deterministic": _count_scores(ran, deterministic=True),
        "evaluator_scores_llm_judge": _count_scores(ran, deterministic=False),
        "evaluator_skipped": sum(
            1 for r in ran for e in r.get("evaluators") or [] if e.get("status") == "skipped_missing_input"
        ),
    }


def _count_scores(ran: list[dict], *, deterministic: bool) -> int:
    return sum(
        1 for r in ran for e in r.get("evaluators") or []
        if e.get("status") == "ok" and isinstance(e.get("score"), (int, float))
        and (e.get("model") == "deterministic") == deterministic
    )


def build_evaluator_summary(results: list[dict]) -> dict:
    """Per-evaluator counts across the run: judged, passed, insufficient, errors, skipped.

    A skipped or errored evaluation is counted separately and never enters the mean.
    """
    from evals.judges import PASS_THRESHOLD

    summary: dict[str, dict] = {}
    for result in results:
        if result.get("status") != "ok":
            continue
        for item in result.get("evaluators") or []:
            row = summary.setdefault(
                item["evaluator"],
                {"version": item.get("version"), "models": set(), "n_scored": 0, "n_passed": 0,
                 "n_insufficient": 0, "n_error": 0, "n_skipped": 0, "errors": set(),
                 "missing_inputs": set(), "unverified_evidence": 0, "_scores": []},
            )
            row["models"].add(item.get("model"))
            status = item.get("status")
            if status == "error":
                row["n_error"] += 1
                row["errors"].add(item.get("error") or "unknown")
            elif status == "skipped_missing_input":
                row["n_skipped"] += 1
                row["missing_inputs"].update(item.get("missing") or [])
            elif item.get("verdict") == "insufficient_evidence" or item.get("score") is None:
                row["n_insufficient"] += 1
            else:
                row["n_scored"] += 1
                row["_scores"].append(item["score"])
                deterministic = item.get("model") == "deterministic"
                if item.get("verdict") == "pass" and (deterministic or item["score"] >= PASS_THRESHOLD):
                    row["n_passed"] += 1
                row["unverified_evidence"] += item.get("evidence_unverified") or 0

    out = {}
    for name, row in sorted(summary.items()):
        scores = row.pop("_scores")
        row["mean_score"] = m.mean(scores) if scores else None
        row["models"] = sorted(x for x in row["models"] if x)
        row["errors"] = sorted(row["errors"])[:3]
        row["missing_inputs"] = sorted(row["missing_inputs"])
        row["kind"] = "deterministic" if row["models"] == ["deterministic"] else "llm_judge"
        out[name] = row
    return out


def build_evaluator_metrics(summary: dict) -> dict:
    """`eval.<name>` metrics: mean score over the cases actually scored (n shown, never padded)."""
    metrics = {}
    for name, row in summary.items():
        if row["n_scored"]:
            metrics[f"eval.{name}"] = {
                "value": row["mean_score"], "n": row["n_scored"], "direction": "higher", "unit": "ratio",
            }
    return metrics
