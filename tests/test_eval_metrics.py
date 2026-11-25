import pytest

from evals import metrics as m


RETRIEVED = ["A", "B", "C", "D"]


def test_recall_at_k_counts_relevant_found_in_top_k():
    assert m.recall_at_k(RETRIEVED, ["A", "C"], 1) == 0.5
    assert m.recall_at_k(RETRIEVED, ["A", "C"], 3) == 1.0
    assert m.recall_at_k(RETRIEVED, ["Z"], 4) == 0.0


def test_recall_is_undefined_when_nothing_is_relevant():
    assert m.recall_at_k(RETRIEVED, [], 4) is None
    assert m.reciprocal_rank(RETRIEVED, []) is None
    assert m.precision_at_k(RETRIEVED, [], 4) is None


def test_precision_divides_by_the_number_returned_not_by_k():
    # The retriever may return fewer than k chunks on purpose (distance gate).
    assert m.precision_at_k(["A"], ["A"], 4) == 1.0
    assert m.precision_at_k(["A", "X"], ["A"], 4) == 0.5
    assert m.precision_at_k(RETRIEVED, ["A", "B"], 2) == 1.0


def test_precision_is_undefined_when_nothing_was_returned():
    assert m.precision_at_k([], ["A"], 4) is None


def test_reciprocal_rank_uses_first_relevant_position():
    assert m.reciprocal_rank(RETRIEVED, ["A"]) == 1.0
    assert m.reciprocal_rank(RETRIEVED, ["C", "D"]) == pytest.approx(1 / 3)
    assert m.reciprocal_rank(RETRIEVED, ["Z"]) == 0.0
    assert m.reciprocal_rank([], ["A"]) == 0.0


def test_mean_and_rate_ignore_none():
    assert m.mean([1.0, None, 0.0]) == 0.5
    assert m.mean([None]) is None
    assert m.rate([True, False, None, True]) == pytest.approx(2 / 3)
    assert m.rate([]) is None


def test_latency_percentiles_appear_only_with_enough_observations():
    few = m.latency_summary([100, 200, 300])
    assert few["p50"] == 200 and few["p90"] is None and few["p95"] is None and few["p99"] is None

    ten = m.latency_summary(list(range(1, 11)))
    assert ten["p90"] == 9 and ten["p95"] is None

    twenty = m.latency_summary(list(range(1, 21)))
    assert twenty["p95"] == 19 and twenty["p99"] is None

    hundred = m.latency_summary(list(range(1, 101)))
    assert hundred["p99"] == 99 and hundred["n"] == 100


def test_latency_summary_of_nothing_is_all_none():
    assert m.latency_summary([]) == {"n": 0, "p50": None, "p90": None, "p95": None, "p99": None, "max": None}
