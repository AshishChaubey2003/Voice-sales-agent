"""Baseline comparison and regression flags.

The default tolerances are PROVISIONAL. They are conservative starting points
for a small dataset, not calibrated thresholds. Calibrate them from repeated
real runs (see docs/EVALUATION_PLAN.md, "Calibrating thresholds").
"""

from __future__ import annotations

DEFAULT_RATE_TOLERANCE = 0.05  # absolute change on a 0..1 metric
DEFAULT_LATENCY_TOLERANCE = 0.25  # relative increase
MIN_N_TO_COMPARE = 5  # fewer observations than this on either side: not compared

_LATENCY_STATS = ("p50", "p90", "p95", "p99")


def _classify(
    direction: str, baseline: float, current: float, tolerance: float, relative: bool
) -> str:
    delta = current - baseline
    if relative:
        if baseline == 0:
            change = 0.0 if delta == 0 else float("inf")
        else:
            change = delta / baseline
    else:
        change = delta
    worse = change > tolerance if direction == "lower" else change < -tolerance
    better = change < -tolerance if direction == "lower" else change > tolerance
    return "regression" if worse else "improvement" if better else "unchanged"


def compare_runs(
    current: dict,
    baseline: dict,
    *,
    rate_tolerance: float = DEFAULT_RATE_TOLERANCE,
    latency_tolerance: float = DEFAULT_LATENCY_TOLERANCE,
) -> dict:
    warnings: list[str] = []
    if current.get("dataset", {}).get("sha256") != baseline.get("dataset", {}).get("sha256"):
        warnings.append("dataset differs from the baseline's; deltas mix data and behaviour changes")
    if current.get("mode") != baseline.get("mode"):
        warnings.append(
            f"mode differs (baseline {baseline.get('mode')}, current {current.get('mode')})"
        )
    if current.get("model", {}).get("name") != baseline.get("model", {}).get("name"):
        warnings.append("model differs from the baseline's")
    if bool(current.get("judge", {}).get("enabled")) != bool(baseline.get("judge", {}).get("enabled")):
        warnings.append("judge was enabled in only one of the two runs")

    rows: list[dict] = []
    base_metrics = baseline.get("metrics", {})
    for name, cur in sorted(current.get("metrics", {}).items()):
        base = base_metrics.get(name)
        row = {
            "name": name,
            "direction": cur["direction"],
            "unit": cur.get("unit", "ratio"),
            "baseline": base["value"] if base else None,
            "current": cur["value"],
            "baseline_n": base["n"] if base else 0,
            "current_n": cur["n"],
            "delta": None,
            "status": "insufficient_data",
        }
        if (
            base
            and base["value"] is not None
            and cur["value"] is not None
            and base["n"] >= MIN_N_TO_COMPARE
            and cur["n"] >= MIN_N_TO_COMPARE
        ):
            row["delta"] = cur["value"] - base["value"]
            row["status"] = _classify(
                cur["direction"], base["value"], cur["value"], rate_tolerance, relative=False
            )
        rows.append(row)

    base_latency = baseline.get("latency", {})
    for series, cur_stats in sorted(current.get("latency", {}).items()):
        base_stats = base_latency.get(series, {})
        for stat in _LATENCY_STATS:
            cur_value, base_value = cur_stats.get(stat), base_stats.get(stat)
            if cur_value is None and base_value is None:
                continue
            row = {
                "name": f"latency.{series}.{stat}",
                "direction": "lower",
                "unit": "ms",
                "baseline": base_value,
                "current": cur_value,
                "baseline_n": base_stats.get("n", 0),
                "current_n": cur_stats.get("n", 0),
                "delta": None,
                "status": "insufficient_data",
            }
            if cur_value is not None and base_value is not None:
                row["delta"] = cur_value - base_value
                row["status"] = _classify(
                    "lower", base_value, cur_value, latency_tolerance, relative=True
                )
            rows.append(row)

    return {
        "baseline_run_id": baseline.get("run_id"),
        "baseline_name": baseline.get("baseline_name"),
        "current_run_id": current.get("run_id"),
        "tolerances": {
            "rate_absolute": rate_tolerance,
            "latency_relative": latency_tolerance,
            "min_n": MIN_N_TO_COMPARE,
            "calibrated": False,
        },
        "warnings": warnings,
        "metrics": rows,
        "regressions": [r["name"] for r in rows if r["status"] == "regression"],
        "improvements": [r["name"] for r in rows if r["status"] == "improvement"],
    }
