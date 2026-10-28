"""Markdown summary of a run. Plain text only; safe to paste into a review."""

from __future__ import annotations

from evals import review_status


def _fmt(value, unit: str = "ratio") -> str:
    if value is None:
        return "n/a"
    if unit == "ms":
        return f"{value:.0f} ms"
    return f"{value:.3f}"


def render_summary(run: dict, cases: list[dict], comparison: dict | None = None) -> str:
    dataset = run.get("dataset", {})
    model = run.get("model", {})
    counts = run.get("counts", {})
    lines = [
        f"# Evaluation run {run['run_id']}",
        "",
        f"- Status: **{run.get('status')}**",
        f"- Mode: {run.get('mode')}",
        f"- Started: {run.get('started_at')}  Finished: {run.get('finished_at')}",
        f"- Dataset: {dataset.get('name')} (sha256 {str(dataset.get('sha256'))[:12]}), "
        f"{dataset.get('n_cases')} cases, {dataset.get('reviewed_cases')} human-reviewed"
        + (f", review state at run time: {dataset['review']['state']}" if dataset.get("review") else ""),
        f"- Model: {model.get('provider')} / {model.get('name')} "
        f"(reasoning effort: {model.get('reasoning_effort')})",
        f"- Judge: {'enabled, ' + str(run.get('judge', {}).get('model')) if run.get('judge', {}).get('enabled') else 'off'}",
        f"- Prompt/config fingerprint: {run.get('prompt_fingerprint')}",
        f"- Code version: {run.get('code_version') or 'unknown'}",
        f"- LangSmith publish: {run.get('langsmith', {}).get('status', 'off')}",
        _tracing_line(run),
        "",
        "## Counts",
        "",
        f"ran {counts.get('ran')} of {counts.get('total')}, passed {counts.get('passed')}, "
        f"failed {counts.get('failed')}, errors {counts.get('errors')}, skipped {counts.get('skipped')}, "
        f"judge errors {counts.get('judge_errors')} (failed evaluator calls {counts.get('evaluator_errors', 0)}), known-gap failures {counts.get('known_gap_failures')}",
        f"evaluator scores: {counts.get('evaluator_scores_deterministic', 0)} deterministic, "
        f"{counts.get('evaluator_scores_llm_judge', 0)} LLM-judge; skipped {counts.get('evaluator_skipped', 0)}",
        "",
    ]
    notice = review_status.run_banner(dataset)
    if notice:
        lines += [f"> {notice}", ""]

    lines += ["## Metrics", "", "| metric | value | n | better |", "|---|---|---|---|"]
    for name, metric in sorted(run.get("metrics", {}).items()):
        lines.append(
            f"| {name} | {_fmt(metric['value'], metric.get('unit', 'ratio'))} | {metric['n']} | {metric['direction']} |"
        )

    lines += ["", "## Latency (ms)", "", "| series | n | p50 | p90 | p95 | p99 | max |", "|---|---|---|---|---|---|---|"]
    for series, stats in run.get("latency", {}).items():
        cells = [("n/a" if stats[k] is None else f"{stats[k]:.0f}") for k in ("p50", "p90", "p95", "p99", "max")]
        lines.append(f"| {series} | {stats['n']} | " + " | ".join(cells) + " |")
    lines += ["", "Percentiles are shown only with enough observations (p90 >= 10, p95 >= 20, p99 >= 100).", ""]

    lines += ["## By category", "", "| category | cases | passed | errors |", "|---|---|---|---|"]
    for name, stats in run.get("by_category", {}).items():
        lines.append(f"| {name} | {stats['n']} | {stats['passed']} | {stats['errors']} |")

    if comparison:
        lines += ["", "## Against baseline", ""]
        lines.append(
            f"Baseline `{comparison.get('baseline_name')}` (run {comparison.get('baseline_run_id')}). "
            "Tolerances are provisional and uncalibrated."
        )
        for warning in comparison["warnings"]:
            lines.append(f"- Warning: {warning}")
        lines += ["", "| metric | baseline | current | delta | status |", "|---|---|---|---|---|"]
        for row in comparison["metrics"]:
            unit = row["unit"]
            delta = "n/a" if row["delta"] is None else (f"{row['delta']:+.0f} ms" if unit == "ms" else f"{row['delta']:+.3f}")
            lines.append(
                f"| {row['name']} | {_fmt(row['baseline'], unit)} | {_fmt(row['current'], unit)} | {delta} | {row['status']} |"
            )
        lines.append("")
        lines.append(
            "Regressions: " + (", ".join(comparison["regressions"]) if comparison["regressions"] else "none flagged")
        )

    lines += _judge_call_lines(cases)

    failed = [c for c in cases if c.get("status") in ("ok", "error") and c.get("passed") is not True]
    lines += ["", f"## Failed or errored cases ({len(failed)})", ""]
    for case in failed:
        lines.append(f"### {case['case_id']}  ({case['category']})")
        lines.append(f"- Question: {case['question']}")
        if case.get("error"):
            lines.append(f"- Error: {case['error']}")
        for reason in case.get("failure_reasons", []):
            lines.append(f"- {reason}")
        reply = case.get("output", {}).get("reply")
        if reply:
            lines.append(f"- Reply: {reply}")
        judge = case.get("judge")
        if judge and judge.get("ok") and judge.get("explanation"):
            lines.append(f"- Judge: {judge['explanation']}")
        lines.append("")
    if not failed:
        lines.append("None.")
    return "\n".join(lines).rstrip() + "\n"


def _latency_text(value) -> str:
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else "N/A"


def _judge_call_lines(cases: list[dict]) -> list[str]:
    """One line per model-judge call (scored or failed). Deterministic checks and skips make no call."""
    rows = []
    for case in cases:
        for item in case.get("evaluators") or []:
            if item.get("model") == "deterministic" or item.get("status") not in ("ok", "error"):
                continue
            outcome = (f"score={item['score']}" if item.get("status") == "ok" and item.get("score") is not None
                       else f"verdict={item.get('verdict')}" if item.get("status") == "ok"
                       else f"error={item.get('error')}")
            rows.append(f"- {case['case_id']} / {item['evaluator']} ({item.get('model')}): {outcome}, "
                        f"latency_ms={_latency_text(item.get('latency_ms'))}")
    if not rows:
        return []
    return ["", f"## LLM judge calls ({len(rows)})", "",
            "Latency is the provider-measured duration of that one call; N/A when no reply arrived (for example "
            "a timeout) or in runs saved before this field existed. It is not the experiment's total duration.",
            "", *rows]


def _tracing_line(run: dict) -> str:
    tracing = run.get("tracing")
    if not tracing:
        return "- Application tracing: not recorded"
    if not tracing.get("enabled"):
        return "- Application tracing: off"
    content = "with content" if tracing.get("content") else "metadata only"
    return f"- Application tracing: on (project {tracing.get('project')}, {content})"
