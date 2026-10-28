import json

import pytest

from evals import baseline, run as cli, store
from evals.schema import load_dataset

GOLDEN = "evals/datasets/nimbuscrm_golden_v1.jsonl"


def test_full_mode_and_judge_refuse_to_run_without_explicit_consent(capsys):
    assert cli.main(["--mode", "full", "--dry-run"]) == cli.EXIT_USAGE
    assert "--allow-llm" in capsys.readouterr().err
    assert cli.main(["--mode", "replay", "--replay", "x", "--judge"]) == cli.EXIT_USAGE
    assert cli.main(["--mode", "retrieval", "--judge", "--allow-llm"]) == cli.EXIT_USAGE
    assert cli.main(["--mode", "replay"]) == cli.EXIT_USAGE


def test_dry_run_prints_the_cost_plan_and_executes_nothing(capsys):
    assert cli.main(["--mode", "full", "--allow-llm", "--judge", "--dry-run", "--max-llm-calls", "10"]) == 0
    out = capsys.readouterr().out
    assert "Estimated LLM calls" in out and "hard cap 10" in out and "stop as 'partial'" in out
    assert "Dry run" in out


def test_retrieval_mode_estimates_zero_llm_calls():
    dataset = load_dataset(GOLDEN)
    plan = cli.estimate_llm_calls(dataset, "retrieval", judge=False, limit=None)
    assert plan["total"] == 0 and plan["cases_to_run"] > 0
    full = cli.estimate_llm_calls(dataset, "full", judge=True, limit=None)
    assert full["total"] > full["chat_calls"] > 0


def test_bad_dataset_exits_with_a_usage_error(tmp_path, capsys):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"id": "x"}\n')
    assert cli.main(["--dataset", str(path), "--dry-run"]) == cli.EXIT_USAGE
    assert "dataset error" in capsys.readouterr().err


def test_policy_snapshot_reports_every_decision_open_when_no_approvals_file_exists(tmp_path):
    """EVAL-01: a run's policy snapshot must never fabricate an approval - with no
    policy_approvals.csv present (the real state throughout Phases 4J-4N), every decision
    id must come back 'open', matching evals.review_policy.status() exactly."""
    from evals import review_policy

    snapshot = cli.policy_snapshot(tmp_path)

    assert snapshot == review_policy.status(tmp_path)
    assert snapshot["file_present"] is False
    assert set(snapshot["open"]) == set(review_policy.DECISIONS)


def test_policy_snapshot_reflects_a_real_recorded_approval(tmp_path):
    """A genuinely recorded approval (never one this code invents) is reflected in the
    snapshot exactly as review_policy.status() reports it."""
    import csv

    from evals import review_policy

    with (tmp_path / review_policy.APPROVALS_FILE).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(review_policy.COLUMNS)
        writer.writerow(["C3", "owner", "2026-11-01", "doc-link"])

    snapshot = cli.policy_snapshot(tmp_path)

    assert "C3" not in snapshot["open"]
    c3 = next(d for d in snapshot["decisions"] if d["id"] == "C3")
    assert c3["status"] == "approved"


def test_prompt_fingerprint_is_stable_and_changes_with_content():
    assert cli.prompt_fingerprint("a", "b") == cli.prompt_fingerprint("a", "b")
    assert cli.prompt_fingerprint("a", "b") != cli.prompt_fingerprint("a", "c")
    assert len(cli.prompt_fingerprint("a")) == 12


def _write_run(results_dir, run_id, pass_rate_value):
    run = {
        "run_id": run_id, "status": "completed", "mode": "full", "organization_id": "o",
        "dataset": {"sha256": "s"}, "model": {"name": "m"}, "judge": {"enabled": False},
        "metrics": {"run.pass_rate": {"value": pass_rate_value, "n": 50, "direction": "higher", "unit": "ratio"}},
        "latency": {}, "counts": {},
    }
    store.write_run(results_dir, run, [], "")


def test_baseline_cli_set_compare_and_exit_codes(tmp_path, capsys):
    results, baselines = str(tmp_path / "r"), str(tmp_path / "b")
    _write_run(results, "20260101T000000Z-aaaaaaaa", 0.9)
    _write_run(results, "20260102T000000Z-bbbbbbbb", 0.6)
    common = ["--results-dir", results, "--baselines-dir", baselines]

    assert baseline.main([*common, "set", "20260101T000000Z-aaaaaaaa", "main"]) == 0
    assert baseline.main([*common, "list"]) == 0 and "main" in capsys.readouterr().out
    assert baseline.main([*common, "compare", "20260102T000000Z-bbbbbbbb", "main"]) == 1
    assert "run.pass_rate" in capsys.readouterr().out
    assert baseline.main([*common, "compare", "20260101T000000Z-aaaaaaaa", "main"]) == 0
    assert baseline.main([*common, "compare", "20260101T000000Z-aaaaaaaa", "nope"]) == 2
    assert baseline.main([*common, "set", "20260101T000000Z-aaaaaaaa", "../x"]) == 2
    assert baseline.main([*common, "set", "20991231T000000Z-ffffffff", "x"]) == 2


def test_tracing_status_reflects_what_was_actually_active(monkeypatch):
    from api import tracing
    from evals import run as run_module

    class Settings:
        langsmith_project = "voice-sales-agent-evals"

    monkeypatch.setattr(tracing, "_resolve", lambda: False)
    assert run_module._tracing_status(Settings()) == {"enabled": False, "project": None, "content": False}
    monkeypatch.setattr(tracing, "_resolve", lambda: True)
    monkeypatch.setattr(tracing, "_trace_content", False)
    assert run_module._tracing_status(Settings()) == {"enabled": True, "project": "voice-sales-agent-evals", "content": False}
