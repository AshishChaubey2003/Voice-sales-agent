import os
import sys
from unittest.mock import MagicMock

import pytest

from evals import langsmith_sync as ls
from evals.schema import Dataset, parse_case


def dataset():
    case = parse_case({
        "id": "c1", "kind": "rag_answer", "category": "factual",
        "messages": [{"role": "user", "content": "How much is Pro?"}],
        "expected_sources": ["P"], "required_facts": ["29"],
    })
    return Dataset("golden", "golden.jsonl", "a" * 64, [case])


def test_example_ids_are_deterministic_and_dataset_scoped():
    assert ls.example_id("d", "c1") == ls.example_id("d", "c1")
    assert ls.example_id("d", "c1") != ls.example_id("d", "c2")
    assert ls.example_id("d", "c1") != ls.example_id("e", "c1")


def test_examples_carry_inputs_expectations_and_review_status():
    (example,) = ls.build_examples(dataset(), "golden")
    assert example["inputs"]["messages"][0]["content"] == "How much is Pro?"
    assert example["outputs"]["required_facts"] == ["29"]
    assert example["metadata"] == {"review_status": "unreviewed", "dataset_sha256": "a" * 64}


def test_make_client_without_a_key_is_a_clean_error_that_never_prints_a_key(monkeypatch):
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    with pytest.raises(ls.LangSmithUnavailable, match="LANGSMITH_API_KEY is not set"):
        ls.make_client()


def test_make_client_without_the_package_explains_how_to_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "langsmith", None)
    with pytest.raises(ls.LangSmithUnavailable, match="requirements-eval.txt") as info:
        ls.make_client("lsv2_pt_supersecretvalue")
    assert "supersecret" not in str(info.value)


def test_sync_creates_the_dataset_once_and_upserts_examples():
    client = MagicMock()
    client.has_dataset.return_value = False
    result = ls.sync_dataset(client, dataset(), "golden")
    assert result.created_dataset and result.examples == 1
    client.create_dataset.assert_called_once()
    assert "0 of 1 cases human-reviewed" in client.create_dataset.call_args.kwargs["description"]
    client.create_examples.assert_called_once()
    assert client.create_examples.call_args.kwargs["dataset_name"] == "golden"

    existing = MagicMock()
    existing.has_dataset.return_value = True
    assert ls.sync_dataset(existing, dataset(), "golden").created_dataset is False
    existing.create_dataset.assert_not_called()


def test_replay_target_and_evaluator_return_the_local_results():
    cases = [{"case_id": "c1", "status": "ok", "passed": False,
              "output": {"reply": "no", "context": ["x"]}, "scores": {"mrr": 0.5, "required_facts": 0.0},
              "judge": {"ok": True, "scores": {"faithfulness": 1.0}}}]
    target, evaluator, _ = ls.build_replay_target(cases)
    assert target({"case_id": "c1"}) == {"reply": "no"}
    assert target({"case_id": "other"}) == {"skipped": True}
    keys = {r["key"]: r["score"] for r in evaluator({"case_id": "c1"}, {})["results"]}
    assert keys == {"mrr": 0.5, "required_facts": 0.0, "judge_faithfulness": 1.0, "case_passed": 0.0}
    assert evaluator({"case_id": "other"}, {}) == {"results": []}


def test_upload_experiment_passes_local_results_and_reports_the_url(monkeypatch):
    seen = {}

    def fake_evaluate(target, **kwargs):
        seen.update(kwargs)
        return type("R", (), {"experiment_name": "exp-1", "url": "https://smith.langchain.com/x"})()

    module = type(sys)("langsmith")
    module.evaluate = fake_evaluate
    monkeypatch.setitem(sys.modules, "langsmith", module)
    run = {"run_id": "r1", "mode": "full", "dataset": {"sha256": "s"}, "model": {"name": "m"}}
    info = ls.upload_experiment(MagicMock(), run, [], "golden")
    assert info == {"experiment_name": "exp-1", "url": "https://smith.langchain.com/x"}
    assert seen["data"] == "golden" and seen["metadata"]["local_run_id"] == "r1"


@pytest.mark.skipif(
    not os.environ.get("EVAL_TEST_LANGSMITH_SDK"),
    reason="opt-in: the SDK probes api.smith.langchain.com even with upload_results=False",
)
def test_real_sdk_accepts_our_target_and_evaluator_offline():
    """Runs langsmith.evaluate with upload_results=False and no API key (no upload).

    Opt in with EVAL_TEST_LANGSMITH_SDK=1 and `pip install -r requirements-eval.txt`.
    """
    pytest.importorskip("langsmith")
    import uuid
    from datetime import datetime, timezone

    from langsmith import evaluate
    from langsmith.schemas import Example

    cases = [{"case_id": "c1", "status": "ok", "passed": True, "output": {"reply": "It is $29."},
              "scores": {"mrr": 1.0, "required_facts": 1.0}}]
    target, evaluator, _ = ls.build_replay_target(cases)
    example = Example(id=uuid.uuid4(), dataset_id=uuid.uuid4(), created_at=datetime.now(timezone.utc),
                      inputs={"case_id": "c1"}, outputs={})
    results = evaluate(target, data=[example], evaluators=[evaluator], upload_results=False,
                       max_concurrency=0, blocking=True)
    scored = {}
    for row in results:
        for result in row["evaluation_results"]["results"]:
            scored[result.key] = result.score
    assert scored == {"mrr": 1.0, "required_facts": 1.0, "case_passed": 1.0}


def test_a_limited_run_syncs_only_the_cases_it_executed():
    two = dataset()
    two.cases.append(parse_case({
        "id": "c2", "kind": "rag_answer", "category": "factual",
        "messages": [{"role": "user", "content": "Is there a free plan?"}], "expected_sources": ["P"], "required_facts": ["free"],
    }))
    assert [e["inputs"]["case_id"] for e in ls.build_examples(two, "golden", {"c2"})] == ["c2"]
    assert len(ls.build_examples(two, "golden")) == 2
    client = MagicMock()
    client.has_dataset.return_value = True
    assert ls.sync_dataset(client, two, "golden", {"c1"}).examples == 1


def test_upload_experiment_scores_only_the_requested_examples(monkeypatch):
    seen = {}
    module = type(sys)("langsmith")
    module.evaluate = lambda target, **kw: seen.update(kw) or type("R", (), {"experiment_name": "e", "url": None})()
    monkeypatch.setitem(sys.modules, "langsmith", module)
    client = MagicMock()
    client.list_examples.return_value = iter(["ex1"])
    ls.upload_experiment(client, {"run_id": "r"}, [], "golden", example_ids=["id1"])
    client.list_examples.assert_called_once_with(example_ids=["id1"])
    assert seen["data"] == ["ex1"]


def test_verify_publish_reads_back_counts_and_never_raises():
    client = MagicMock()
    client.read_dataset.return_value.id = "d-id"
    client.list_examples.return_value = iter([1, 2])
    client.read_project.return_value.id = "p-id"
    client.list_runs.return_value = iter([1, 2])
    report = ls.verify_publish(client, "golden", "exp", 2, wait_seconds=0)
    assert report["verified"] and report["examples_remote"] == 2 and report["experiment_runs"] == 2

    broken = MagicMock()
    broken.read_dataset.side_effect = RuntimeError("boom")
    broken.read_project.side_effect = RuntimeError("boom")
    report = ls.verify_publish(broken, "golden", "exp", 2, wait_seconds=0)
    assert report["verified"] is False and report["dataset_error"] == "RuntimeError"

    short = MagicMock()
    short.read_dataset.return_value.id = "d"
    short.list_examples.return_value = iter([1])
    short.read_project.return_value.id = "p"
    short.list_runs.return_value = iter([1, 2])
    assert ls.verify_publish(short, "golden", "exp", 2, wait_seconds=0)["verified"] is False


def test_verify_publish_waits_for_runs_that_arrive_late():
    client = MagicMock()
    client.read_dataset.return_value.id = "d"
    client.read_project.return_value.id = "p"
    client.list_examples.side_effect = lambda **kw: iter([1, 2])
    counts = iter([0, 0, 2])
    client.list_runs.side_effect = lambda **kw: iter(range(next(counts)))
    ticks = iter(range(100))
    report = ls.verify_publish(client, "g", "e", 2, wait_seconds=30, sleep=lambda s: None, clock=lambda: next(ticks))
    assert report["verified"] and report["experiment_runs"] == 2


def test_verify_publish_gives_up_after_the_wait():
    client = MagicMock()
    client.read_dataset.return_value.id = "d"
    client.read_project.return_value.id = "p"
    client.list_examples.side_effect = lambda **kw: iter([1, 2])
    client.list_runs.side_effect = lambda **kw: iter([])
    ticks = iter(range(0, 1000, 5))
    report = ls.verify_publish(client, "g", "e", 2, wait_seconds=10, sleep=lambda s: None, clock=lambda: next(ticks))
    assert report["verified"] is False and report["experiment_runs"] == 0
