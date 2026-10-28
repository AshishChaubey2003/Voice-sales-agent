"""Runs a dataset against a target and scores the output.

The runner knows nothing about databases, HTTP or LangSmith. A target is any
object with `async run_case(case) -> dict` (and optionally `budget_exhausted`).
That keeps the scoring path testable with fakes and lets the same code score a
live system, a recorded run (replay) or a unit-test double.

Target output keys (all optional except where a kind needs them):
    reply, retrieved_sources, context, retrieval_ms, llm_ms, wall_ms,
    lead_status, lead_row (reduced to lead_saved / lead_email_ok), retrieval_rewritten_query
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from evals import EVAL_SCHEMA_VERSION
from evals import aggregate, judge_suite
from evals.evaluators import (
    Check,
    evaluate_lead_gate,
    evaluate_lead_outcome,
    evaluate_reply,
    evaluate_retrieval,
)
from evals.schema import Dataset, EvalCase

MODES = ("retrieval", "full", "replay")


class Target(Protocol):
    async def run_case(self, case: EvalCase) -> dict: ...


EXCLUDED_REASON = "excluded by reviewers (not a metric example)"


def skip_reason(case: EvalCase, mode: str, replay_ids: set[str] | None = None) -> str | None:
    """Why a case cannot run in this mode, or None when it can."""
    if case.review_status == "excluded":
        # Reviewers deliberately removed this case from metric calculations. It never runs, so it can
        # never enter a numerator or denominator; it stays in the dataset for audit.
        return EXCLUDED_REASON
    if case.kind == "lead_gate":
        return None  # pure code: runs in every mode
    if mode == "retrieval":
        if case.kind == "lead_llm":
            return "needs a live LLM (use --mode full)"
        if case.category == "follow_up":
            return "query rewriting needs a live LLM (use --mode full)"
        return None
    if mode == "replay":
        if replay_ids is not None and case.id not in replay_ids:
            return "no recorded response for this case"
        return None
    return None


def _expected_summary(case: EvalCase) -> dict:
    expected: dict = {}
    if case.expected_sources:
        expected["sources"] = case.expected_sources
    if case.required_facts:
        expected["required_facts"] = case.required_facts
    if case.forbidden_facts or case.forbidden_regex:
        expected["forbidden"] = [*case.forbidden_facts, *[f"/{p}/" for p in case.forbidden_regex]]
    if case.expect_abstain:
        expected["abstain"] = True
    if case.expected_gate:
        expected["gate"] = case.expected_gate
    if case.expected_lead_saved is not None:
        expected["lead_saved"] = case.expected_lead_saved
    return expected


def _base_result(case: EvalCase) -> dict:
    return {
        "case_id": case.id,
        "kind": case.kind,
        "category": case.category,
        "review_status": case.review_status,
        "known_gap": case.known_gap,
        "question": case.query,
        "expected": _expected_summary(case),
        "status": "ok",
        "error": None,
        "output": {},
        "scores": {},
        "checks": [],
        "passed": None,
        "failure_reasons": [],
        "judge": None,
        "evaluators": [],
    }


def score_output(case: EvalCase, output: dict, ks=None) -> tuple[dict, list[Check]]:
    scores: dict = {}
    checks: list[Check] = []
    if case.kind == "rag_answer":
        retrieved = output.get("retrieved_sources")
        if retrieved is not None:
            s, c = evaluate_retrieval(case, list(retrieved), ks)
            scores.update(s)
            checks.extend(c)
        reply = output.get("reply")
        if reply is not None:
            s, c = evaluate_reply(case, reply)
            scores.update(s)
            checks.extend(c)
    elif case.kind == "lead_llm":
        s, c = evaluate_lead_outcome(
            case,
            output.get("lead_status"),
            bool(output.get("lead_saved")),
            output.get("lead_email_ok"),
            output.get("reply") or "",
        )
        scores.update(s)
        checks.extend(c)
    return scores, checks


def _finish(result: dict, checks: list[Check]) -> None:
    result["checks"] = [c.to_dict() for c in checks]
    blocking = [c for c in checks if c.blocking and c.passed is not None]
    result["failure_reasons"] = [f"{c.name}: {c.detail}" for c in blocking if not c.passed]
    result["passed"] = bool(blocking) and all(c.passed for c in blocking)
    if not blocking:
        result["passed"] = None


async def run_case(case: EvalCase, target: Target | None, judge=None, suite=None, ks=None) -> dict:
    result = _base_result(case)

    if case.kind == "lead_gate":
        scores, checks, output = evaluate_lead_gate(case)
        result.update(scores=scores, output=output)
        _finish(result, checks)
        return result

    started = time.perf_counter()
    try:
        output = await target.run_case(case)
    except Exception as error:
        # Type name only: provider and driver messages can carry connection
        # details, and the result file must stay safe to share.
        result.update(status="error", error=type(error).__name__, passed=False)
        result["failure_reasons"] = [f"target raised {type(error).__name__}"]
        return result
    output = dict(output)
    output.setdefault("wall_ms", int((time.perf_counter() - started) * 1000))

    # The stored lead row is reduced to two booleans: no lead data in result files.
    lead_row = output.pop("lead_row", None)
    if lead_row is not None or "lead_saved" not in output:
        output["lead_saved"] = lead_row is not None
        expected_email = (case.expected_lead_email or "").lower()
        output["lead_email_ok"] = (
            (lead_row or {}).get("email") == expected_email if expected_email and lead_row else None
        )
    scores, checks = score_output(case, output, ks)
    context = output.get("context") or []

    result.update(scores=scores, output={k: v for k, v in output.items() if k != "context"})
    result["output"]["context_chunks"] = len(context)
    _finish(result, checks)

    reply = output.get("reply")
    if judge is not None and case.kind == "rag_answer" and reply:
        verdict = await judge.judge(
            case.query, context, case.required_facts, case.expect_abstain, reply
        )
        result["judge"] = verdict.to_dict()
        if not judge_suite.has_reviewed_reference(case):
            # Correctness is judged against the author's required facts / abstain expectation. Until a human
            # has reviewed those facts it must not be scored, exactly like the --judge-suite correctness judge.
            result["judge"]["scores"].pop("correctness", None)
            result["judge"]["raw_scores"].pop("correctness", None)
            result["judge"]["correctness_skipped"] = "no human-reviewed reference facts for this case"

    # Evidence next to the verdict, never part of it: pass/fail above is already final.
    result["evaluators"] = judge_suite.deterministic_outcomes(case, output)
    if suite is not None:
        result["evaluators"].extend(await suite.evaluate(case, output))
    return result


async def run_suite(
    dataset: Dataset,
    target: Target | None,
    *,
    mode: str,
    judge=None,
    suite=None,
    ks=None,
    limit: int | None = None,
    replay_ids: set[str] | None = None,
    on_progress: Callable[[int, int, dict], None] | None = None,
) -> tuple[list[dict], str]:
    """Returns (results, status). Status is completed, partial or failed."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")

    cases = dataset.cases[:limit] if limit else dataset.cases
    results: list[dict] = []
    status = "completed"

    for index, case in enumerate(cases, start=1):
        reason = skip_reason(case, mode, replay_ids)
        if reason:
            skipped = _base_result(case)
            skipped.update(status="skipped", error=reason)
            results.append(skipped)
            continue

        if getattr(target, "budget_exhausted", False) or getattr(judge, "budget_exhausted", False) or getattr(suite, "budget_exhausted", False):
            status = "partial"
            break

        result = await run_case(case, target, judge, suite, ks)
        results.append(result)
        if on_progress:
            on_progress(index, len(cases), result)

    ran = [r for r in results if r["status"] in ("ok", "error")]
    if not ran:
        status = "failed"
    elif all(r["status"] == "error" for r in ran):
        status = "failed"
    return results, status


def finalize_run(meta: dict, results: list[dict]) -> dict:
    """Adds aggregate sections to the run metadata."""
    run = {"schema_version": EVAL_SCHEMA_VERSION, **meta}
    run["counts"] = aggregate.build_counts(results)
    run["metrics"] = aggregate.build_metrics(results)
    run["latency"] = aggregate.build_latency(results)
    run.update(aggregate.build_breakdowns(results))
    run["evaluators"] = aggregate.build_evaluator_summary(results)
    run["metrics"].update(aggregate.build_evaluator_metrics(run["evaluators"]))
    return run
