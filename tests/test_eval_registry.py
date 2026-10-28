import pytest

from evals import registry as r


def test_wilson_interval_known_values_and_bounds():
    low, high = r.wilson_interval(0, 100)
    assert low == 0.0 and 0.03 < high < 0.04
    low, high = r.wilson_interval(50, 100)
    assert 0.40 < low < 0.41 and 0.59 < high < 0.60
    assert r.wilson_interval(0, 0) is None
    low, high = r.wilson_interval(10, 10)
    assert high == 1.0 and low > 0.6


def test_describe_rate_shows_numerator_denominator_and_small_sample_warning():
    d = r.describe_rate(0, 5)
    assert (d["numerator"], d["denominator"]) == (0, 5)
    assert "not proof" not in (d["note"] or "") and "compatible" in d["note"] and "60%" in d["note"]
    assert r.describe_rate(2, 100)["note"] is None
    empty = r.describe_rate(0, 0)
    assert empty["rate"] is None and empty["denominator"] == 0


def run(metrics, reviewed=0, ready=False):
    dataset = {"reviewed_cases": reviewed}
    if ready:
        dataset["review"] = {"ready_for_validated_metrics": True, "state": "ready"}
    return {"metrics": metrics, "dataset": dataset}


def test_resolve_reports_unavailable_never_zero():
    rows = {x["name"]: x for x in r.resolve_metrics(run({}))}
    assert rows["retrieval.mrr"]["status"] == "unavailable" and rows["retrieval.mrr"]["value"] is None
    rows = {x["name"]: x for x in r.resolve_metrics(run({"retrieval.mrr": {"value": None, "n": 0}}))}
    assert rows["retrieval.mrr"]["status"] == "unavailable"


def test_resolve_labels_unreviewed_as_provisional_and_adds_counts_for_binary_metrics():
    metrics = {"lead.gate_false_save": {"value": 0.0, "n": 8}, "retrieval.mrr": {"value": 0.4, "n": 10}}
    rows = {x["name"]: x for x in r.resolve_metrics(run(metrics))}
    save = rows["lead.gate_false_save"]
    assert save["status"] == "provisional" and save["rate"]["numerator"] == 0 and save["rate"]["denominator"] == 8
    assert save["level"] == "ok"
    assert rows["retrieval.mrr"]["level"] == "critical" and "rate" not in rows["retrieval.mrr"]
    # A reviewed flag in the dataset file (or a partly reviewed dataset) is NOT enough to call a metric measured.
    partly = {x["name"]: x for x in r.resolve_metrics(run(metrics, reviewed=10))}
    assert partly["retrieval.mrr"]["status"] == "provisional"
    ready = {x["name"]: x for x in r.resolve_metrics(run(metrics, reviewed=10, ready=True))}
    assert ready["retrieval.mrr"]["status"] == "measured"


def test_threshold_levels_respect_direction():
    t = r._t(0.9, 0.7, "higher")
    assert [r._level(v, t) for v in (0.95, 0.8, 0.5)] == ["ok", "warn", "critical"]
    t = r._t(0.02, 0.05, "lower")
    assert [r._level(v, t) for v in (0.0, 0.03, 0.1)] == ["ok", "warn", "critical"]
    assert r._level(0.5, None) is None


def test_every_threshold_is_marked_provisional_and_catalogue_is_honest():
    assert all(m["threshold"] is None or m["threshold"]["status"] == "provisional" for m in r.METRICS)
    cat = r.catalogue()
    statuses = {e["name"]: e["status"] for e in cat["evaluators"]}
    assert statuses["Faithfulness / hallucination"] == "implemented_unrun"
    assert statuses["Audio quality / interruptions / vocal affect"] == "not_implemented"
    assert statuses["PII leakage"] == "implemented"  # now scored inside every run
    assert statuses["Tone and language"] == "implemented_unrun"
    assert statuses["Toxicity"] == "not_implemented"
    assert "provisional" in cat["thresholds_note"]
    assert len({m["name"] for m in r.METRICS}) == len(r.METRICS)


def test_fact_and_judge_metrics_never_become_measured_from_dataset_readiness_alone():
    names = ("retrieval.mrr", "answer.required_facts", "run.pass_rate", "judge.correctness", "eval.correctness",
             "eval.relevance", "eval.pii_leak")
    metrics = {n: {"value": 0.9, "n": 20} for n in names}
    ready = run(metrics, reviewed=20, ready=True)
    rows = {x["name"]: x for x in r.resolve_metrics(ready)}
    assert rows["retrieval.mrr"]["status"] == "measured" and rows["retrieval.mrr"]["why_provisional"] is None
    assert rows["eval.pii_leak"]["status"] == "measured"
    for name in ("answer.required_facts", "run.pass_rate", "judge.correctness", "eval.correctness", "eval.relevance"):
        assert rows[name]["status"] == "provisional" and rows[name]["why_provisional"]
    # even if a future run reported validated facts, a judge score alone stays provisional
    ready["dataset"]["review"]["facts_validated"] = True
    again = {x["name"]: x for x in r.resolve_metrics(ready)}
    assert again["answer.required_facts"]["status"] == "measured"
    assert again["eval.correctness"]["status"] == "provisional" and "human reviewers" in again["eval.correctness"]["why_provisional"]
    assert again["eval.relevance"]["status"] == "provisional"


def test_provisional_rows_say_why_and_old_runs_without_a_review_block_say_so():
    metrics = {"retrieval.mrr": {"value": 0.5, "n": 4}}
    row = {x["name"]: x for x in r.resolve_metrics(run(metrics))}["retrieval.mrr"]
    assert row["status"] == "provisional" and "no reviewer evidence" in row["why_provisional"]
    partial = {"metrics": metrics, "dataset": {"review": {"ready_for_validated_metrics": False, "state": "in_progress"}}}
    row = {x["name"]: x for x in r.resolve_metrics(partial)}["retrieval.mrr"]
    assert "in_progress" in row["why_provisional"]
    unavailable = {x["name"]: x for x in r.resolve_metrics(run({}))}["retrieval.mrr"]
    assert unavailable["status"] == "unavailable" and "why_provisional" not in unavailable


def test_every_metric_depending_on_facts_or_a_judge_is_declared_so():
    by_name = {m["name"]: m for m in r.METRICS}
    assert "facts" in by_name["judge.correctness"]["needs"] and "judge" in by_name["judge.correctness"]["needs"]
    assert by_name["answer.required_facts"]["needs"] == ["facts"]
    assert by_name["retrieval.recall_at_4"]["needs"] == []
    for name in r._NEEDS_FACTS | r._NEEDS_JUDGE:
        assert name in by_name, name
    # every judge-produced metric is covered; only the deterministic eval.* checks are exempt
    deterministic = {"eval.pii_leak", "eval.tool_selection", "eval.conciseness"}
    judge_metrics = {m["name"] for m in r.METRICS if m["name"].startswith(("judge.", "eval."))} - deterministic
    assert judge_metrics == r._NEEDS_JUDGE
