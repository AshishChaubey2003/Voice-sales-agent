"""Optional LangSmith integration for the evaluation harness.

Nothing here runs unless the caller passes --langsmith AND LANGSMITH_API_KEY is
available. The local runner never depends on this module succeeding.

What it does
------------
sync_dataset       Creates/updates a LangSmith dataset from the golden dataset.
                   Example ids are derived from (dataset name, case id), so
                   re-running updates examples instead of duplicating them.
upload_experiment  Publishes a finished LOCAL run as a LangSmith experiment. The
                   target simply returns the outputs the local run already
                   produced and the evaluators return the scores it already
                   computed, so no model is called a second time and the
                   LangSmith numbers are identical to the local report.

Verified against langsmith 0.14.12 by reading the installed SDK. Not verified
against a live workspace in this repository's history: see docs/LANGSMITH.md.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass

from evals.schema import Dataset

_NAMESPACE = uuid.UUID("6f1d3a2e-7c55-4b0a-9a4e-0e3f5d1c2b77")

# Scores worth plotting per experiment. Everything else stays in the local report.
_SCORE_KEYS = (
    "recall_at_1",
    "recall_at_3",
    "recall_at_4",
    "precision_at_1",
    "precision_at_3",
    "precision_at_4",
    "mrr",
    "required_facts",
    "forbidden_clear",
    "abstained",
    "plain_text",
    "within_length",
    "gate_correct",
    "gate_false_save",
    "lead_outcome_correct",
    "premature_tool_call",
    "false_lead_save",
    "missed_lead_save",
)
_AT_K = re.compile(r"^(recall|precision)_at_\d+$")


class LangSmithUnavailable(RuntimeError):
    """LangSmith cannot be used. The message never contains credentials."""


def example_id(dataset_name: str, case_id: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"{dataset_name}:{case_id}"))


def build_examples(dataset: Dataset, dataset_name: str, case_ids=None) -> list[dict]:
    """Examples for the dataset, or only for `case_ids` (the cases a limited run executed)."""
    examples = []
    for case in dataset.cases:
        if case_ids is not None and case.id not in case_ids:
            continue
        examples.append(
            {
                "id": example_id(dataset_name, case.id),
                "inputs": {
                    "case_id": case.id,
                    "kind": case.kind,
                    "category": case.category,
                    "messages": case.messages,
                },
                "outputs": {
                    "expected_sources": case.expected_sources,
                    "required_facts": case.required_facts,
                    "expect_abstain": case.expect_abstain,
                    "expected_gate": case.expected_gate,
                    "expected_lead_saved": case.expected_lead_saved,
                },
                "metadata": {
                    "review_status": case.review_status,
                    "dataset_sha256": dataset.sha256,
                },
            }
        )
    return examples


def make_client(api_key: str | None = None, endpoint: str | None = None):
    """Builds a client, or raises LangSmithUnavailable. The key is never echoed."""
    key = api_key or os.environ.get("LANGSMITH_API_KEY")
    if not key:
        raise LangSmithUnavailable("LANGSMITH_API_KEY is not set; skipping LangSmith")
    try:
        from langsmith import Client
    except ImportError:
        raise LangSmithUnavailable(
            "the 'langsmith' package is not installed (pip install -r requirements-eval.txt)"
        ) from None
    kwargs = {"api_key": key}
    if endpoint or os.environ.get("LANGSMITH_ENDPOINT"):
        kwargs["api_url"] = endpoint or os.environ["LANGSMITH_ENDPOINT"]
    return Client(**kwargs)


@dataclass
class SyncResult:
    dataset_name: str
    examples: int
    created_dataset: bool


def sync_dataset(client, dataset: Dataset, dataset_name: str, case_ids=None) -> SyncResult:
    created = False
    if not client.has_dataset(dataset_name=dataset_name):
        client.create_dataset(
            dataset_name,
            description=(
                f"Golden dataset {dataset.name} (sha256 {dataset.sha256[:12]}). "
                f"{dataset.reviewed_count} of {len(dataset.cases)} cases human-reviewed."
            ),
        )
        created = True
    examples = build_examples(dataset, dataset_name, case_ids)
    client.create_examples(dataset_name=dataset_name, examples=examples)
    return SyncResult(dataset_name, len(examples), created)


def _scores_for(case_result: dict) -> list[dict]:
    results = []
    scores = case_result.get("scores") or {}
    keys = [k for k in _SCORE_KEYS if k in scores] + sorted(
        k for k in scores if _AT_K.match(k) and k not in _SCORE_KEYS)
    for key in keys:
        value = scores.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            results.append({"key": key, "score": float(value)})
    verdict = case_result.get("judge")
    if verdict and verdict.get("ok"):
        for key, value in verdict["scores"].items():
            results.append({"key": f"judge_{key}", "score": float(value)})
    results.extend(_evaluator_feedback(case_result.get("evaluators") or []))
    if case_result.get("passed") is not None:
        results.append({"key": "case_passed", "score": 1.0 if case_result["passed"] else 0.0})
    return results


def _comment(item: dict, text: str) -> str:
    """Evaluator version and model travel with every explanation. Text is already scrubbed upstream."""
    return f"{item['evaluator']} v{item.get('version')} [{item.get('model')}]: {text}"[:400]


def _evaluator_feedback(evaluators: list[dict]) -> list[dict]:
    """One feedback entry per evaluator outcome that carries information.

    scored            -> key eval_<name>, numeric score, reason as the comment
    insufficient      -> key eval_<name>, value 'insufficient_evidence' (no score)
    error             -> key eval_<name>_error, value 'error' (a failure is visible, never a score)
    skipped (missing) -> nothing is uploaded; the local report lists it
    """
    out = []
    for item in evaluators:
        key = f"eval_{item['evaluator']}"
        status = item.get("status")
        if status == "error":
            out.append({"key": f"{key}_error", "value": "error", "comment": _comment(item, item.get("error") or "error")})
        elif status != "ok":
            continue
        elif item.get("verdict") == "insufficient_evidence" or item.get("score") is None:
            out.append({"key": key, "value": "insufficient_evidence", "comment": _comment(item, item.get("reason") or "")})
        else:
            out.append({"key": key, "score": float(item["score"]), "comment": _comment(item, item.get("reason") or "")})
    return out


def build_replay_target(cases: list[dict]):
    """A target that returns what the local run produced for each case id."""
    by_id = {c["case_id"]: c for c in cases if c.get("status") == "ok"}

    def target(inputs: dict) -> dict:
        record = by_id.get(inputs.get("case_id"))
        if record is None:
            return {"skipped": True}
        output = dict(record.get("output") or {})
        return {k: v for k, v in output.items() if k != "context"}

    def local_scores(inputs: dict, outputs: dict) -> dict:
        record = by_id.get(inputs.get("case_id"))
        return {"results": _scores_for(record) if record else []}

    return target, local_scores, by_id


def upload_experiment(
    client,
    run: dict,
    cases: list[dict],
    dataset_name: str,
    *,
    experiment_prefix: str = "voice-agent-eval",
    example_ids: list[str] | None = None,
) -> dict:
    """Publishes a finished local run. Returns {'experiment_name', 'url'} (url may be None)."""
    try:
        from langsmith import evaluate
    except ImportError:
        raise LangSmithUnavailable("the 'langsmith' package is not installed") from None

    target, local_scores, by_id = build_replay_target(cases)
    # Only the examples this run executed, so a --limit run does not score the rest as "skipped".
    data = list(client.list_examples(example_ids=example_ids)) if example_ids else dataset_name
    results = evaluate(
        target,
        data=data,
        evaluators=[local_scores],
        client=client,
        experiment_prefix=experiment_prefix,
        description=f"Local run {run['run_id']} ({run.get('mode')} mode), replayed into LangSmith.",
        metadata={
            "local_run_id": run["run_id"],
            "dataset_sha256": run.get("dataset", {}).get("sha256"),
            "model": run.get("model", {}).get("name"),
            "mode": run.get("mode"),
            "prompt_fingerprint": run.get("prompt_fingerprint"),
        },
        max_concurrency=0,
        blocking=True,
    )
    return {
        "experiment_name": getattr(results, "experiment_name", None),
        "url": getattr(results, "url", None),
    }


def verify_publish(client, dataset_name: str, experiment_name: str | None, expected_examples: int,
                   *, wait_seconds: float = 20.0, sleep=None, clock=None) -> dict:
    """Reads back what LangSmith actually holds, polling because runs arrive asynchronously.

    Never raises; counts and ids only.
    """
    import time

    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    deadline = clock() + wait_seconds
    while True:
        report = _verify_once(client, dataset_name, experiment_name, expected_examples)
        if report["verified"] or clock() >= deadline:
            return report
        sleep(2.0)


def _verify_once(client, dataset_name: str, experiment_name: str | None, expected_examples: int) -> dict:
    report = {"dataset_found": False, "dataset_id": None, "examples_remote": None,
              "experiment_found": False, "experiment_runs": None, "verified": False}
    try:
        dataset = client.read_dataset(dataset_name=dataset_name)
        report["dataset_found"] = True
        report["dataset_id"] = str(dataset.id)
        report["examples_remote"] = sum(1 for _ in client.list_examples(dataset_id=dataset.id))
    except Exception as error:
        report["dataset_error"] = type(error).__name__
    if experiment_name:
        try:
            project = client.read_project(project_name=experiment_name)
            report["experiment_found"] = True
            report["experiment_id"] = str(project.id)
            report["experiment_runs"] = sum(1 for _ in client.list_runs(project_name=experiment_name, is_root=True))
        except Exception as error:
            report["experiment_error"] = type(error).__name__
    report["verified"] = bool(
        report["dataset_found"]
        and (report["examples_remote"] or 0) >= expected_examples
        and report["experiment_found"]
        and (report["experiment_runs"] or 0) >= expected_examples
    )
    return report


def expected_feedback(cases: list[dict], dataset_name: str) -> dict[str, set[str]]:
    """example id -> feedback keys the local run says should exist on that example's run."""
    return {
        example_id(dataset_name, c["case_id"]): {r["key"] for r in _scores_for(c)}
        for c in cases
        if c.get("status") == "ok"
    }


def verify_feedback(client, experiment_name: str | None, expected: dict[str, set[str]],
                    *, wait_seconds: float = 30.0, sleep=None, clock=None) -> dict:
    """Reads the feedback back from LangSmith and compares it with what the local run produced.

    Matches each experiment run to its example (reference_example_id), so a score attached to
    the wrong example is reported as missing, not accepted. Counts and key names only; never raises.
    """
    import time

    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    deadline = clock() + wait_seconds
    while True:
        report = _verify_feedback_once(client, experiment_name, expected)
        if report["verified"] or "error" in report or clock() >= deadline:
            return report
        sleep(2.0)


def _verify_feedback_once(client, experiment_name, expected: dict[str, set[str]]) -> dict:
    report = {"runs": 0, "examples_expected": len(expected), "examples_matched": 0,
              "feedback_expected": sum(len(v) for v in expected.values()), "feedback_found": 0,
              "missing": {}, "duplicates": {}, "unexpected_examples": 0, "verified": False}
    if not experiment_name:
        report["error"] = "no experiment name"
        return report
    try:
        runs = list(client.list_runs(project_name=experiment_name, is_root=True))
        report["runs"] = len(runs)
        by_example = {}
        for run in runs:
            ref = str(run.reference_example_id) if getattr(run, "reference_example_id", None) else None
            by_example[ref] = run
        report["unexpected_examples"] = sum(1 for ref in by_example if ref not in expected)
        feedback_by_run: dict[str, set[str]] = {}
        run_ids = [r.id for r in runs]
        if run_ids:
            seen: dict[tuple[str, str], int] = {}
            for fb in client.list_feedback(run_ids=run_ids):
                feedback_by_run.setdefault(str(fb.run_id), set()).add(fb.key)
                seen[(str(fb.run_id), fb.key)] = seen.get((str(fb.run_id), fb.key), 0) + 1
            for (run_id, key), count in seen.items():
                if count > 1:
                    report["duplicates"][f"{run_id[:8]}:{key}"] = count
        for ref, keys in expected.items():
            run = by_example.get(ref)
            if run is None:
                report["missing"][ref[:8]] = sorted(keys)
                continue
            report["examples_matched"] += 1
            found = feedback_by_run.get(str(run.id), set())
            report["feedback_found"] += len(keys & found)
            gone = sorted(keys - found)
            if gone:
                report["missing"][ref[:8]] = gone
    except Exception as error:
        report["error"] = type(error).__name__
        return report
    report["verified"] = (bool(expected) and not report["missing"] and not report["duplicates"]
                          and report["examples_matched"] == len(expected))
    return report
