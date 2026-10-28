"""Pure metric functions. No I/O, no project imports except voice.stats.

Retrieval ground truth is a list of chunk headings (for example
"NimbusCRM Pricing > Pro plan"). Headings are stable across re-ingestion;
chunk UUIDs are not.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

# Nearest-rank percentile, shared with scripts/latency_report.py.
from voice.stats import percentile

# Below these observation counts a percentile is not reported at all, because it
# would be one or two samples dressed up as a statistic.
MIN_N_FOR_P90 = 10
MIN_N_FOR_P95 = 20
MIN_N_FOR_P99 = 100


def recall_at_k(retrieved: Sequence[str], relevant: Sequence[str], k: int) -> float | None:
    """Share of relevant headings found in the first k retrieved. None if nothing is relevant."""
    relevant_set = set(relevant)
    if not relevant_set:
        return None
    hits = relevant_set.intersection(retrieved[:k])
    return len(hits) / len(relevant_set)


def precision_at_k(retrieved: Sequence[str], relevant: Sequence[str], k: int) -> float | None:
    """Share of the returned top-k that is relevant.

    The denominator is the number actually returned (at most k), not k itself.
    The retriever applies a distance gate and may legitimately return fewer than k
    chunks; dividing by k would punish that. None when nothing was returned or
    nothing is relevant.
    """
    relevant_set = set(relevant)
    top = list(retrieved[:k])
    if not relevant_set or not top:
        return None
    hits = sum(1 for heading in top if heading in relevant_set)
    return hits / len(top)


def reciprocal_rank(retrieved: Sequence[str], relevant: Sequence[str]) -> float | None:
    """1 / rank of the first relevant heading, 0.0 if none was retrieved."""
    relevant_set = set(relevant)
    if not relevant_set:
        return None
    for position, heading in enumerate(retrieved, start=1):
        if heading in relevant_set:
            return 1.0 / position
    return 0.0


def mean(values: Sequence[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return sum(present) / len(present)


def rate(flags: Sequence[bool | None]) -> float | None:
    """Share of True among non-None flags."""
    present = [f for f in flags if f is not None]
    if not present:
        return None
    return sum(1 for f in present if f) / len(present)


def latency_summary(values: Sequence[float]) -> dict:
    """Percentiles that are only reported when enough observations exist."""
    clean = [float(v) for v in values if v is not None and not math.isnan(float(v))]
    n = len(clean)
    summary: dict = {"n": n, "p50": None, "p90": None, "p95": None, "p99": None, "max": None}
    if n == 0:
        return summary
    summary["p50"] = percentile(clean, 50)
    summary["max"] = max(clean)
    if n >= MIN_N_FOR_P90:
        summary["p90"] = percentile(clean, 90)
    if n >= MIN_N_FOR_P95:
        summary["p95"] = percentile(clean, 95)
    if n >= MIN_N_FOR_P99:
        summary["p99"] = percentile(clean, 99)
    return summary
