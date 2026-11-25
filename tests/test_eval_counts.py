"""Run-level counts, pass rate and category breakdowns.

Statuses are mutually exclusive: passed + failed + ungraded + errors + skipped == total.
Pass rate is passed / graded; ungraded cases (no blocking check), errors and skipped cases are
excluded from both numerator and denominator and are reported separately.
"""

import pytest

from evals import aggregate


def ok(passed, category="factual", **extra):
    return {"status": "ok", "passed": passed, "category": category, "kind": "rag_answer", **extra}


def error(category="factual"):
    # The runner stores an errored case with passed=False; it must not also count as "failed".
    return {"status": "error", "passed": False, "category": category, "kind": "rag_answer"}


def skipped(category="factual"):
    return {"status": "skipped", "passed": None, "category": category, "kind": "rag_answer"}


MIXED = [ok(True), ok(True), ok(False), ok(None), ok(None), error(), skipped(), skipped()]


def test_counts_are_mutually_exclusive_and_reconcile_with_total():
    counts = aggregate.build_counts(MIXED)
    assert counts["total"] == 8
    assert (counts["passed"], counts["failed"], counts["ungraded"], counts["errors"], counts["skipped"]) == (2, 1, 2, 1, 2)
    assert counts["graded"] == 3
    assert counts["passed"] + counts["failed"] + counts["ungraded"] + counts["errors"] + counts["skipped"] == counts["total"]
    assert counts["unaccounted"] == 0 and counts["counts_consistent"] is True
    assert counts["ran"] == 6  # ok + error, unchanged meaning


def test_an_errored_case_is_not_also_counted_as_failed():
    counts = aggregate.build_counts([error(), ok(True)])
    assert counts["errors"] == 1 and counts["failed"] == 0 and counts["graded"] == 1


def test_pass_rate_uses_graded_cases_only():
    metric = aggregate.build_metrics(MIXED)["run.pass_rate"]
    assert metric["value"] == pytest.approx(2 / 3)
    assert metric["n"] == 3  # graded cases, not ran (6) and not total (8)


def test_ungraded_cases_are_neither_passes_nor_failures():
    results = [ok(True)] * 45 + [ok(False)] * 11 + [ok(None)] * 10
    counts = aggregate.build_counts(results)
    assert counts["ungraded"] == 10 and counts["graded"] == 56
    assert aggregate.build_metrics(results)["run.pass_rate"]["value"] == pytest.approx(45 / 56)


def test_all_failed_run_reports_zero_but_all_ungraded_run_reports_none():
    assert aggregate.build_metrics([ok(False), ok(False)])["run.pass_rate"]["value"] == 0.0
    ungraded = aggregate.build_metrics([ok(None), ok(None)])["run.pass_rate"]
    assert ungraded["value"] is None and ungraded["n"] == 0


def test_all_errored_run_has_no_pass_rate_but_a_full_error_rate():
    metrics = aggregate.build_metrics([error(), error()])
    assert metrics["run.pass_rate"]["value"] is None
    assert metrics["run.error_rate"]["value"] == 1.0


def test_skipped_cases_never_reach_the_pass_rate_or_error_rate():
    metrics = aggregate.build_metrics([ok(True), skipped(), skipped()])
    assert metrics["run.pass_rate"]["value"] == 1.0 and metrics["run.pass_rate"]["n"] == 1
    assert metrics["run.error_rate"]["n"] == 1


def test_empty_run():
    counts = aggregate.build_counts([])
    assert counts["total"] == counts["graded"] == counts["ungraded"] == counts["unaccounted"] == 0
    assert counts["counts_consistent"] is True
    metrics = aggregate.build_metrics([])
    assert metrics["run.pass_rate"]["value"] is None and metrics["run.error_rate"]["value"] is None
    assert aggregate.build_breakdowns([]) == {"by_category": {}, "by_kind": {}}


def test_unknown_status_is_surfaced_not_hidden():
    counts = aggregate.build_counts([ok(True), {"status": "weird", "passed": None}, ok("yes")])
    assert counts["unaccounted"] == 2 and counts["counts_consistent"] is False


def test_known_gap_failures_ignore_errors_and_ungraded():
    results = [ok(False, known_gap=True), ok(None, known_gap=True), {**error(), "known_gap": True}]
    assert aggregate.build_counts(results)["known_gap_failures"] == 1


def test_category_pass_rate_uses_graded_denominator():
    results = [
        ok(True, "factual"), ok(False, "factual"), ok(None, "factual"), error("factual"), skipped("factual"),
        ok(None, "lead_gate"), ok(None, "lead_gate"),
        ok(True, "edge"),
    ]
    cats = aggregate.build_breakdowns(results)["by_category"]
    f = cats["factual"]
    assert (f["n"], f["graded"], f["passed"], f["failed"], f["ungraded"], f["errors"], f["skipped"]) == (4, 2, 1, 1, 1, 1, 1)
    assert f["pass_rate"] == 0.5
    assert cats["lead_gate"]["pass_rate"] is None and cats["lead_gate"]["graded"] == 0  # never 0.0
    assert cats["edge"]["pass_rate"] == 1.0


def test_category_buckets_reconcile_with_run_counts():
    cats = aggregate.build_breakdowns(MIXED)["by_category"]
    counts = aggregate.build_counts(MIXED)
    for key in ("passed", "failed", "ungraded", "errors", "skipped"):
        assert sum(c[key] for c in cats.values()) == counts[key]


def test_category_with_only_skipped_cases_is_not_listed():
    assert aggregate.build_breakdowns([skipped("x")])["by_category"] == {}
