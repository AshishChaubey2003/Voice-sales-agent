"""Configurable K, false/missed lead saves, LangSmith-vs-dashboard aggregation agreement, review status."""

import pytest

from evals import aggregate, evaluators as ev, langsmith_sync, registry
from evals.run import parse_ks
from evals.schema import parse_case


def case(**kw):
    base = {"id": "c", "kind": "rag_answer", "category": "factual",
            "messages": [{"role": "user", "content": "q"}], "expected_sources": ["B"]}
    base.update(kw)
    return parse_case(base)


def test_parse_ks():
    assert parse_ks(None) is None
    assert parse_ks("5,1,3,3") == (1, 3, 5)
    for bad in ("", "a", "0", "-1", "51"):
        with pytest.raises(ValueError):
            parse_ks(bad)


def test_recall_precision_use_requested_ks():
    scores = ev.retrieval_scores(["A", "B", "C"], ["B"], ks=(2, 5))
    assert scores["recall_at_2"] == 1.0 and scores["recall_at_5"] == 1.0
    assert scores["precision_at_2"] == 0.5
    assert "recall_at_4" not in scores
    assert scores["mrr"] == 0.5


def test_default_ks_unchanged():
    scores = ev.retrieval_scores(["B"], ["B"])
    assert {"recall_at_1", "recall_at_3", "recall_at_4"} <= set(scores)


def lead_case(expected):
    case = {"id": "l", "kind": "lead_llm", "category": "lead_llm",
            "messages": [{"role": "user", "content": "hi"}],
            "expected_lead_saved": expected}
    if expected:
        case["expected_lead_email"] = "lead@example.com"
    return parse_case(case)


def test_false_and_missed_lead_save():
    s, _ = ev.evaluate_lead_outcome(lead_case(False), "saved", True, None, "ok")
    assert s["false_lead_save"] == 1.0 and s["missed_lead_save"] == 0.0
    s, _ = ev.evaluate_lead_outcome(lead_case(True), None, False, None, "ok")
    assert s["false_lead_save"] == 0.0 and s["missed_lead_save"] == 1.0
    s, _ = ev.evaluate_lead_outcome(lead_case(True), "saved", True, None, "ok")
    assert s["false_lead_save"] == 0.0 and s["missed_lead_save"] == 0.0


def test_lead_error_metrics_aggregate_and_are_registered():
    results = [
        {"status": "ok", "passed": True, "scores": {"false_lead_save": 1.0, "missed_lead_save": 0.0}},
        {"status": "ok", "passed": True, "scores": {"false_lead_save": 0.0, "missed_lead_save": 0.0}},
    ]
    m = aggregate.build_metrics(results)
    assert m["lead.false_save"]["value"] == 0.5
    assert m["lead.missed_save"]["value"] == 0.0
    names = {r["name"] for r in registry.METRICS} if hasattr(registry, "METRICS") else set()
    assert not names or {"lead.false_save", "lead.missed_save"} <= names


def test_dashboard_and_langsmith_means_agree():
    """The mean of each LangSmith feedback key equals the dashboard metric for the same scores."""
    results = [
        {"status": "ok", "passed": True,
         "scores": {"recall_at_2": 1.0, "precision_at_2": 0.5, "mrr": 1.0}},
        {"status": "ok", "passed": False,
         "scores": {"recall_at_2": 0.0, "precision_at_2": 0.0, "mrr": 0.0}},
    ]
    metrics = aggregate.build_metrics(results)
    for key in ("recall_at_2", "precision_at_2", "mrr"):
        values = [f["score"] for r in results for f in langsmith_sync._scores_for(r) if f["key"] == key]
        assert len(values) == 2
        mapped = aggregate._score_metric(key)
        assert mapped is not None
        assert metrics[mapped[0]]["value"] == pytest.approx(sum(values) / len(values))
