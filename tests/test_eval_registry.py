from pathlib import Path

import pytest

from evals import aggregate, judge_suite, registry as r
from evals.schema import parse_case


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
    assert statuses["Faithfulness / hallucination"] == "implemented_opt_in"
    assert statuses["Audio quality / interruptions / vocal affect"] == "not_implemented"
    assert statuses["PII leakage"] == "implemented"  # now scored inside every run
    assert statuses["Tone and language"] == "implemented_opt_in"
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


# ---- registry is backed by the implementation ----

def test_every_metric_the_aggregator_can_emit_is_registered_and_documented():
    registered = {m["name"]: m for m in r.METRICS}
    emitted = r.emitted_metrics()
    assert set(emitted) <= set(registered)
    undocumented = sorted(n for n in emitted if not registered[n]["documented"])
    assert undocumented == [], f"emitted metrics without a curated definition: {undocumented}"


def test_no_registered_metric_is_stale_and_directions_come_from_the_aggregator():
    emitted = r.emitted_metrics()
    for spec in r.METRICS:
        assert spec["name"] in emitted, f"{spec['name']} is registered but nothing emits it"
        assert spec["direction"] == emitted[spec["name"]]
        if spec["threshold"]:
            assert spec["threshold"]["direction"] == spec["direction"], spec["name"]
    for name, direction in aggregate._SCORE_METRICS.values():
        assert emitted[name] == direction


def test_a_new_emitted_metric_without_a_curated_spec_is_appended_not_lost(monkeypatch):
    monkeypatch.setitem(aggregate._SCORE_METRICS, "brand_new", ("answer.brand_new", "higher"))
    monkeypatch.setattr(r, "METRICS", [s for s in r.METRICS if s["name"] != "answer.brand_new"])
    r._build_registry()
    added = [s for s in r.METRICS if s["name"] == "answer.brand_new"]
    assert len(added) == 1 and added[0]["documented"] is False and added[0]["direction"] == "higher"
    assert added[0]["n_unit"] in r.N_UNITS


def test_retrieval_k_variants_follow_the_configured_ks():
    names = {m["name"] for m in r.METRICS}
    from evals.evaluators import RETRIEVAL_KS

    for k in RETRIEVAL_KS:
        assert {f"retrieval.recall_at_{k}", f"retrieval.precision_at_{k}"} <= names


def test_deterministic_evaluator_list_matches_what_the_suite_really_emits():
    case = parse_case({"id": "c1", "kind": "rag_answer", "category": "factual",
                       "messages": [{"role": "user", "content": "How much is Pro?"}],
                       "expected_sources": ["P"], "required_facts": ["29"]})
    produced = [o["evaluator"] for o in judge_suite.deterministic_outcomes(case, {"reply": "Pro costs 29 dollars."})]
    assert sorted(produced) == sorted(r.DETERMINISTIC_EVALUATORS)
    for name in judge_suite.LLM_JUDGES:
        assert f"eval.{name}" in r.emitted_metrics() and f"eval.{name}" in r._NEEDS_JUDGE


# ---- every n has a unit ----

def test_every_metric_has_a_valid_sample_size_unit():
    for spec in r.METRICS:
        assert spec["n_unit"] in r.N_UNITS, spec["name"]
    by_name = {m["name"]: m["n_unit"] for m in r.METRICS}
    assert by_name["retrieval.recall_at_4"] == by_name["retrieval.mrr"] == "queries"
    assert by_name["lead.gate_false_save"] == "gate_decisions"
    assert by_name["lead.false_save"] == "tool_decisions"
    assert by_name["answer.required_facts"] == "replies"
    assert by_name["run.pass_rate"] == "cases"
    assert len(set(by_name.values())) > 1  # units are not collapsed into one generic label


def test_resolved_rows_carry_unit_and_direction_and_show_unregistered_recorded_metrics():
    metrics = {"retrieval.mrr": {"value": 0.5, "n": 38, "direction": "higher"},
               "retrieval.recall_at_2": {"value": 0.7, "n": 38, "direction": "higher"},
               "answer.brand_new": {"value": 0.9, "n": 5, "direction": "higher"}}
    rows = {x["name"]: x for x in r.resolve_metrics(run(metrics))}
    assert rows["retrieval.mrr"]["n_unit"] == "queries" and rows["retrieval.mrr"]["direction"] == "higher"
    # --ks can record K values the registry does not list: shown with the generated K definition
    assert rows["retrieval.recall_at_2"]["n_unit"] == "queries" and rows["retrieval.recall_at_2"]["documented"] is True
    extra = rows["answer.brand_new"]
    assert extra["status"] == "provisional" and extra["documented"] is False and "no curated" in extra["why_provisional"]
    unavailable = rows["retrieval.recall_at_1"]
    assert unavailable["status"] == "unavailable" and unavailable["value"] is None and unavailable["n_unit"] == "queries"


# ---- evaluator and unavailable-metric honesty ----

def test_evaluator_statuses_are_valid_and_nothing_claims_it_was_never_run():
    cat = r.catalogue()
    assert set(cat["evaluator_statuses"]) == set(r.EVALUATOR_STATUSES)
    for e in cat["evaluators"]:
        assert e["status"] in r.EVALUATOR_STATUSES, e["name"]
        assert e["runs_with"], e["name"]
        text = " ".join(str(v) for v in e.values()).lower()
        assert "never run live" not in text and "implemented_unrun" not in text, e["name"]
        if e["status"] in ("implemented", "implemented_opt_in"):
            assert e["runs_with"] != "-", e["name"]


def test_opt_in_evaluators_name_a_cli_flag_that_exists():
    source = (Path(r.__file__).parent / "run.py").read_text(encoding="utf-8")
    opt_in = [e for e in r.catalogue()["evaluators"] if e["status"] == "implemented_opt_in"]
    assert opt_in
    for e in opt_in:
        flags = [f for f in ("--judge-suite", "--judge") if f in e["runs_with"]]
        assert flags, e["name"]
        for flag in flags:
            assert f'"{flag}"' in source, f"{flag} is not a real evals.run option"
        assert "--allow-llm" in e["runs_with"]  # paid calls need explicit permission


def test_judge_backed_evaluators_are_opt_in_and_deterministic_ones_are_not():
    by_name = {e["name"]: e for e in r.catalogue()["evaluators"]}
    for name, e in by_name.items():
        if e["kind"] == "LLM judge" and e["status"] != "not_implemented":
            assert e["status"] == "implemented_opt_in", name
        if e["kind"] == "Deterministic" and e["status"] == "implemented":
            assert "--allow-llm" not in e["runs_with"], name


def test_unavailable_metrics_stay_unavailable_with_a_reason_and_are_not_emitted():
    names = [m["name"] for m in r.UNAVAILABLE_METRICS]
    assert len(names) == len(set(names))
    emitted = set(r.emitted_metrics())
    for m in r.UNAVAILABLE_METRICS:
        assert m["reason"].strip(), m["name"]
        assert m["name"] not in emitted, f"{m['name']} is emitted now; remove it from UNAVAILABLE_METRICS"
    assert {"retrieval.ndcg_at_k", "retrieval.context_recall", "operational.token_usage_cost"} <= set(names)
