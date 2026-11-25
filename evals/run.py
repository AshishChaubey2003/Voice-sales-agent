"""Command line entry point:  python -m evals.run --help

Modes
-----
retrieval  Real retriever against the organization's ingested knowledge. Needs
           Postgres and the local embedding model. No LLM, no cost.
full       Real routes in-process with the real LLM. Costs provider calls, so it
           needs --allow-llm and is capped by --max-llm-calls.
replay     Re-scores outputs from an earlier run's cases.jsonl. Needs nothing.

Consent-gate cases run in every mode (pure code).

The runner refuses to write conversations when APP_ENV=production.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from evals import compare, evaluators, review_policy, review_status, runner, store
from evals.report import render_summary
from evals.schema import DatasetError, load_dataset

DEFAULT_DATASET = "evals/datasets/nimbuscrm_golden_v1.jsonl"
DEFAULT_BASELINES = "evals/baselines"
DEFAULT_MAX_LLM_CALLS = 150
EXIT_OK, EXIT_PROBLEM, EXIT_USAGE = 0, 1, 2


def estimate_llm_calls(dataset, mode: str, judge: bool, limit: int | None, suite: bool = False,
                       replay_outputs: dict | None = None) -> dict:
    """Upper-bound estimate of provider calls for the plan shown before a paid run.

    In replay mode pass the recorded outputs: only cases with a recorded reply run, and the judge
    count is computed from what each recording actually contains.
    """
    from evals import judge_suite

    cases = dataset.cases[:limit] if limit else dataset.cases
    chat = judge_calls = suite_calls = 0
    runnable = gates = 0
    skipped: dict[str, int] = {}
    for case in cases:
        recorded = set(replay_outputs) if (mode == "replay" and replay_outputs is not None) else None
        reason = runner.skip_reason(case, mode, recorded)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        if case.kind == "lead_gate":
            gates += 1  # pure code: runs in every mode, never calls a model
            continue
        runnable += 1
        if suite and mode == "replay" and replay_outputs is not None:
            # A recording carries no retrieved text, so the context judges are skipped, not called.
            suite_calls += judge_suite.call_count(case, replay_outputs.get(case.id, {}))
        elif suite and mode in ("full", "replay"):
            suite_calls += judge_suite.estimate_calls(case, has_context=(mode == "full"))
        if mode != "full":
            continue
        if case.kind == "rag_answer":
            chat += 2 if case.category == "follow_up" else 1  # +1 query rewrite
            judge_calls += 1 if judge else 0
        elif case.kind == "lead_llm":
            chat += 2  # decision, then the reply after the tool result
    return {"cases_to_run": runnable + gates, "model_cases": runnable, "free_gate_cases": gates,
            "skipped": sum(skipped.values()), "skipped_reasons": skipped, "chat_calls": chat, "judge_calls": judge_calls,
            "suite_calls": suite_calls, "total": chat + judge_calls + suite_calls}


def prompt_fingerprint(*parts: str) -> str:
    digest = hashlib.sha256("\n\x1f\n".join(parts).encode("utf-8")).hexdigest()
    return digest[:12]


def policy_snapshot(review_dir) -> dict:
    """The exact policy-decision state (open/approved, per evals/review_policy.py) stamped
    into a run's meta - see EVAL-01 in the master audit. Never fabricates an approval: when
    no policy_approvals.csv exists, every decision is reported "open", exactly as
    review_policy.status() itself would report it."""
    return review_policy.status(review_dir)


def code_version() -> str | None:
    """Short commit hash, '+dirty' when the tree has uncommitted changes. Best effort."""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5
        )
        if head.returncode != 0:
            return None
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, timeout=5,
        )
        suffix = "+dirty" if dirty.returncode == 0 and dirty.stdout.strip() else ""
        return head.stdout.strip() + suffix
    except (OSError, subprocess.SubprocessError):
        return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.run", description="Run the voice sales agent evaluation suite."
    )
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--mode", choices=runner.MODES, default="retrieval")
    parser.add_argument("--org", default="nimbus-crm-demo", help="organization slug to evaluate")
    parser.add_argument("--agent-name", default="Nimbus Sales Assistant")
    parser.add_argument("--replay", metavar="CASES_JSONL", help="cases.jsonl of an earlier run (replay mode)")
    parser.add_argument("--allow-llm", action="store_true", help="permit paid LLM calls (full mode / judge)")
    parser.add_argument("--max-llm-calls", type=int, default=DEFAULT_MAX_LLM_CALLS)
    parser.add_argument("--judge", action="store_true", help="add the LLM judge (needs --allow-llm)")
    parser.add_argument("--judge-suite", action="store_true",
                        help="add the per-criterion LLM judges (faithfulness, hallucination, relevance, tone, safety, "
                             "correctness, task completion); needs --allow-llm; deterministic checks always run")
    parser.add_argument("--judge-model", help="model name for the judge (default: the provider's configured model)")
    parser.add_argument("--judge-provider", choices=("groq", "gemini"), default="groq",
                        help="which provider runs the judge (gemini needs GEMINI_API_KEY and GEMINI_JUDGE_MODEL)")
    parser.add_argument("--ks", default=None, metavar="K,K,...",
                        help="K values for Recall@K and Precision@K (default 1,3,4). The retriever returns at most "
                             "RAG_TOP_K chunks, so a K above that equals the value at RAG_TOP_K")
    parser.add_argument("--limit", type=int, help="run only the first N cases")
    parser.add_argument("--case-id", action="append", default=[], metavar="ID",
                        help="run only this case (repeatable; exact ids; combined with --limit it applies to the selection)")
    parser.add_argument("--results-dir", help="default: EVAL_RESULTS_DIR or evals/results")
    parser.add_argument("--baselines-dir", default=DEFAULT_BASELINES)
    parser.add_argument("--baseline", metavar="NAME", help="compare against this saved baseline")
    parser.add_argument("--fail-on-regression", action="store_true")
    parser.add_argument("--langsmith", action="store_true", help="also publish to LangSmith (opt in)")
    parser.add_argument("--langsmith-dataset", help="LangSmith dataset name (default: dataset file name)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and stop")
    return parser.parse_args(argv)


def parse_ks(text: str | None) -> tuple[int, ...] | None:
    """'1,3,5' -> (1, 3, 5). None when unset. Raises ValueError with a readable message."""
    if text is None:
        return None
    try:
        values = sorted({int(part) for part in text.split(",") if part.strip()})
    except ValueError:
        raise ValueError("--ks must be a comma-separated list of integers, for example 1,3,4") from None
    if not values or values[0] < 1 or values[-1] > 50:
        raise ValueError("--ks values must be between 1 and 50")
    return tuple(values)


def _validate(args, dataset) -> str | None:
    try:
        args.ks_parsed = parse_ks(args.ks)
    except ValueError as error:
        return str(error)
    if args.mode == "replay" and not args.replay:
        return "--mode replay needs --replay <cases.jsonl>"
    if args.replay and args.mode != "replay":
        return "--replay is only valid with --mode replay"
    if (args.judge or args.judge_suite) and not args.allow_llm:
        return "--judge/--judge-suite make paid LLM calls; add --allow-llm to confirm"
    if args.judge_suite and args.mode == "retrieval":
        return "--judge-suite needs replies; use --mode full or --mode replay"
    if args.mode == "full" and not args.allow_llm:
        return "--mode full makes paid LLM calls; add --allow-llm to confirm"
    if args.judge and args.mode == "retrieval":
        return "--judge needs replies; use --mode full or --mode replay"
    if args.max_llm_calls < 1:
        return "--max-llm-calls must be at least 1"
    return None


def _tracing_status(settings) -> dict:
    """Whether application tracing was actually active for this run (not just configured)."""
    from api import tracing

    enabled = tracing.tracing_enabled()
    return {
        "enabled": enabled,
        "project": settings.langsmith_project if enabled else None,
        "content": tracing.content_allowed(),
    }


async def _execute(args, dataset, plan) -> int:
    from api.config import get_settings

    settings = get_settings()
    if (args.judge or args.judge_suite) and args.judge_provider == "gemini":
        from evals.gemini import SETUP_HINT, model_id_problem

        missing = [name for name, value in (("GEMINI_API_KEY", settings.gemini_api_key),
                                            ("GEMINI_JUDGE_MODEL", args.judge_model or settings.gemini_judge_model)) if not value]
        if missing:
            print(f"error: --judge-provider gemini needs {', '.join(missing)} (set in .env or the environment); no call was made", file=sys.stderr)
            return 2
        problem = model_id_problem(args.judge_model or settings.gemini_judge_model)
        if problem:
            print(f"error: {problem}; {SETUP_HINT}. No call was made.", file=sys.stderr)
            return 2
    top_k = settings.rag_top_k
    too_big = [k for k in (args.ks_parsed or ()) if k > top_k]
    if too_big:
        print(f"note: RAG_TOP_K is {top_k}; Recall@K for K in {too_big} equals the value at K={top_k} "
              "because the retriever never returns more chunks")
    results_dir = args.results_dir or settings.eval_results_dir
    review_dir = Path(dataset.path).resolve().parent.parent / "review"
    started = datetime.now(timezone.utc)
    meta: dict = {
        "run_id": store.new_run_id(started),
        "started_at": started.isoformat(),
        "mode": args.mode,
        "dataset": {**dataset.describe(), "review": review_status.dataset_review_summary(
            dataset.path, review_dir)},
        # EVAL-01: a snapshot of each policy decision's state (open/approved - never
        # invented) at the moment this run happened, so a later policy change can be traced
        # back to which runs it should invalidate. Recording this never affects readiness or
        # scoring; see evals/review_policy.py's own docstring on why approving a decision
        # changes nothing here by itself.
        "policy": policy_snapshot(review_dir),
        "code_version": code_version(),
        "organization_id": None,
        "org_slug": args.org,
        "llm_call_cap": args.max_llm_calls if args.mode == "full" or args.judge or args.judge_suite else 0,
        "case_ids": list(args.case_id) or None,
        "retrieval_ks": list(args.ks_parsed or evaluators.RETRIEVAL_KS),
        "judge": {"enabled": bool(args.judge), "model": None, "provider": args.judge_provider if args.judge else None},
        "judge_suite": {"enabled": bool(args.judge_suite), "model": None,
                        "provider": args.judge_provider if args.judge_suite else None},
        "langsmith": {"status": "off"},
        "tracing": _tracing_status(settings),
        "model": {
            "provider": "groq",
            "name": settings.groq_model,
            "reasoning_effort": settings.llm_reasoning_effort,
            "temperature": 0.3,
            "max_output_tokens": settings.llm_max_output_tokens,
        },
        "rag": {
            "top_k": settings.rag_top_k,
            "max_distance": settings.rag_max_distance,
            "embedding_model": settings.embedding_model,
        },
    }

    judge = None
    judge_llm = None
    results: list[dict]
    status: str

    if args.mode == "replay":
        from evals.targets import ReplayTarget

        target = ReplayTarget(args.replay)
        source_meta = store.load_run(Path(args.replay).parent.parent, Path(args.replay).parent.name)
        if source_meta:
            meta["organization_id"] = source_meta.get("organization_id")
            meta["org_slug"] = source_meta.get("org_slug", args.org)
            meta["replayed_from"] = source_meta.get("run_id")
            meta["model"] = source_meta.get("model", meta["model"])
            meta["rag"] = source_meta.get("rag", meta["rag"])
            meta["prompt_fingerprint"] = source_meta.get("prompt_fingerprint")
        real_judge_llm = None
        if args.judge:
            inner, judge_llm = _build_judge(settings, args)
            judge = _BudgetJudge(inner, judge_llm)
            real_judge_llm = judge_llm
            meta["judge"]["model"] = judge.model_label
        suite = None
        suite_llm = None
        if args.judge_suite:
            suite, suite_llm = _build_suite(settings, args)
            meta["judge_suite"]["model"] = suite.model_label
        try:
            results, status = await runner.run_suite(
                dataset, target, mode="replay", judge=judge, suite=suite, ks=args.ks_parsed, limit=args.limit,
                replay_ids=target.case_ids, on_progress=_progress,
            )
            if suite_llm is not None:
                meta["llm_calls_used"] = suite_llm.calls
            meta["usage"] = _usage_block(judge=judge_llm, judge_suite=suite_llm)
        finally:
            if real_judge_llm is not None:
                await real_judge_llm._inner.close()
            if suite_llm is not None:
                await suite_llm._inner.close()
    else:
        results, status = await _run_live(args, dataset, settings, meta, plan)

    meta["finished_at"] = datetime.now(timezone.utc).isoformat()
    meta["status"] = status
    run = runner.finalize_run(meta, results)

    comparison = None
    if args.baseline:
        baseline = store.load_baseline(args.baselines_dir, args.baseline)
        if baseline is None:
            print(f"warning: baseline '{args.baseline}' not found in {args.baselines_dir}; no comparison")
        else:
            comparison = compare.compare_runs(run, baseline)
            run["comparison"] = {
                "baseline_name": args.baseline,
                "regressions": comparison["regressions"],
                "improvements": comparison["improvements"],
                "warnings": comparison["warnings"],
            }

    if args.langsmith:
        run["langsmith"] = _publish_to_langsmith(args, dataset, run, results, settings)

    directory = store.write_run(
        results_dir, run, results, render_summary(run, results, comparison)
    )
    _print_outcome(run, comparison, directory)

    if status == "failed":
        return EXIT_PROBLEM
    if args.fail_on_regression and comparison and comparison["regressions"]:
        return EXIT_PROBLEM
    return EXIT_OK


def _progress(index: int, total: int, result: dict) -> None:
    mark = {True: "pass", False: "FAIL", None: "n/a"}[result.get("passed")]
    if result["status"] == "error":
        mark = "ERROR"
    print(f"[{index:>3}/{total}] {mark:<5} {result['case_id']}")


def _build_judge(settings, args):
    from api.llm import GroqLLM
    from evals.judge import Judge
    from evals.targets import CountingLLM

    if args.judge_provider == "gemini":
        from evals.gemini import build_gemini_judge_llm

        llm = CountingLLM(build_gemini_judge_llm(settings, args.judge_model), max_calls=args.max_llm_calls)
        return Judge(llm, model_label=f"gemini/{llm._inner.model}"), llm

    model = args.judge_model or settings.groq_model
    llm = CountingLLM(
        GroqLLM(
            api_key=settings.groq_api_key.get_secret_value(),
            model=model,
            timeout_seconds=settings.llm_timeout_seconds,
            max_output_tokens=400,
            reasoning_effort=settings.llm_reasoning_effort,
        ),
        max_calls=args.max_llm_calls,
    )
    return Judge(llm, model_label=f"groq/{model}"), llm


def _provider_llm(settings, args):
    """A call-capped client for the judge provider (Groq or Gemini) and its model label."""
    from api.llm import GroqLLM
    from evals.targets import CountingLLM

    if args.judge_provider == "gemini":
        from evals.gemini import build_gemini_judge_llm

        llm = CountingLLM(build_gemini_judge_llm(settings, args.judge_model), max_calls=args.max_llm_calls)
        return llm, f"gemini/{llm._inner.model}"
    model = args.judge_model or settings.groq_model
    llm = CountingLLM(
        GroqLLM(
            api_key=settings.groq_api_key.get_secret_value(), model=model,
            timeout_seconds=settings.llm_timeout_seconds, max_output_tokens=400,
            reasoning_effort=settings.llm_reasoning_effort,
        ),
        max_calls=args.max_llm_calls,
    )
    return llm, f"groq/{model}"


def _usage_block(**clients) -> dict:
    """Provider-reported token totals per client. No pricing is configured, so cost is never computed."""
    block = {name: dict(client.usage) for name, client in clients.items() if client is not None}
    block["cost"] = None
    block["cost_note"] = "not computed: no pricing is configured in this project"
    return block


def _build_suite(settings, args):
    from evals.judge_suite import JudgeSuite

    llm, label = _provider_llm(settings, args)
    return JudgeSuite(llm, label), llm


async def _run_live(args, dataset, settings, meta, plan) -> tuple[list[dict], str]:
    from sqlalchemy import func, select

    from api.db import build_engine, build_session_factory
    from api.knowledge import GROUNDING_RULES, QUERY_REWRITE_PROMPT
    from api.leads import LEAD_CAPTURE_RULES
    from api.models import Agent, KnowledgeChunk, Organization

    if args.mode == "full" and settings.app_env.strip().lower() == "production":
        raise SystemExit("Refusing to run --mode full with APP_ENV=production: it writes conversations.")

    engine = build_engine()
    session_factory = build_session_factory(engine)
    try:
        async with session_factory() as session:
            org = await session.scalar(select(Organization).where(Organization.slug == args.org))
            if org is None:
                raise SystemExit(f"Organization '{args.org}' not found. Run: python -m scripts.seed_demo")
            chunks = await session.scalar(
                select(func.count()).select_from(KnowledgeChunk).where(KnowledgeChunk.organization_id == org.id)
            )
            if not chunks:
                raise SystemExit(
                    f"Organization '{args.org}' has no knowledge. "
                    "Run: python -m scripts.ingest_knowledge --org " + args.org
                )
            agent = await session.scalar(
                select(Agent).where(
                    Agent.organization_id == org.id, Agent.status == "active", Agent.name == args.agent_name
                )
            ) or await session.scalar(
                select(Agent).where(Agent.organization_id == org.id, Agent.status == "active").limit(1)
            )
            meta["organization_id"] = str(org.id)
            meta["knowledge_chunks"] = int(chunks)
            if agent is not None:
                meta["prompt_fingerprint"] = prompt_fingerprint(
                    agent.system_prompt, GROUNDING_RULES, LEAD_CAPTURE_RULES, QUERY_REWRITE_PROMPT
                )

        if args.mode == "retrieval":
            from evals.targets import RetrievalTarget

            target = RetrievalTarget(
                session_factory=session_factory, organization_id=org.id, settings=settings
            )
            return await runner.run_suite(
                dataset, target, mode="retrieval", ks=args.ks_parsed, limit=args.limit, on_progress=_progress
            )

        if agent is None:
            raise SystemExit(f"Organization '{args.org}' has no active agent.")

        from api.llm import GroqLLM
        from api.main import app
        from evals.judge import Judge
        from evals.targets import ApiTarget, CountingLLM

        real = GroqLLM(
            api_key=settings.groq_api_key.get_secret_value(),
            model=settings.groq_model,
            timeout_seconds=settings.llm_timeout_seconds,
            max_output_tokens=settings.llm_max_output_tokens,
            reasoning_effort=settings.llm_reasoning_effort,
        )
        counting = CountingLLM(real, max_calls=args.max_llm_calls)
        judge = None
        if args.judge:
            judge = Judge(counting, model_label=f"groq/{settings.groq_model}")
            meta["judge"]["model"] = judge.model_label
        suite = None
        suite_llm = None
        if args.judge_suite:
            from evals.judge_suite import JudgeSuite

            if args.judge_provider == "gemini":
                suite, suite_llm = _build_suite(settings, args)  # its own cap, its own counter
            else:
                suite = JudgeSuite(counting, f"groq/{settings.groq_model}")  # shares the one agent cap
            meta["judge_suite"]["model"] = suite.model_label
        try:
            async with ApiTarget(
                app=app, session_factory=session_factory, organization=org,
                agent_id=agent.id, llm=counting,
            ) as target:
                results, status = await runner.run_suite(
                    dataset, target, mode="full", judge=_BudgetJudge(judge, counting) if judge else None,
                    suite=suite, ks=args.ks_parsed, limit=args.limit, on_progress=_progress,
                )
            meta["llm_calls_used"] = counting.calls
            if suite_llm is not None:
                meta["judge_suite_calls_used"] = suite_llm.calls
            meta["usage"] = _usage_block(agent=counting, judge_suite=suite_llm)
            return results, status
        finally:
            await real.close()
            if suite_llm is not None:
                await suite_llm._inner.close()
    finally:
        await engine.dispose()


class _BudgetJudge:
    """Exposes the shared call budget to the runner without changing the Judge."""

    def __init__(self, judge, counting) -> None:
        self._judge = judge
        self._counting = counting
        self.model_label = judge.model_label

    @property
    def budget_exhausted(self) -> bool:
        return self._counting.budget_exhausted

    async def judge(self, *args, **kwargs):
        return await self._judge.judge(*args, **kwargs)


def _scrub(text: str) -> str:
    import re

    return re.sub(r"(lsv2_\w+|Bearer\s+\S+)", "[redacted]", text)[:200]


def _publish_to_langsmith(args, dataset, run, results, settings) -> dict:
    from evals import langsmith_sync as ls

    name = args.langsmith_dataset or dataset.name
    try:
        client = ls.make_client(
            settings.langsmith_api_key.get_secret_value() if settings.langsmith_api_key else None,
            settings.langsmith_endpoint,
        )
        ran = {r["case_id"] for r in results if r.get("status") == "ok"}
        synced = ls.sync_dataset(client, dataset, name, ran)
        experiment = ls.upload_experiment(
            client, run, results, name, example_ids=[ls.example_id(name, c) for c in sorted(ran)]
        )
        check = ls.verify_publish(client, name, experiment["experiment_name"], synced.examples)
        feedback = ls.verify_feedback(
            client, experiment["experiment_name"], ls.expected_feedback(results, name)
        )
        verified = check["verified"] and feedback["verified"]
        return {
            "status": "uploaded and verified" if verified
            else ("uploaded; examples/runs verified but feedback NOT verified" if check["verified"]
                  else "uploaded but NOT verified"),
            "feedback": feedback,
            "dataset": name,
            "examples": synced.examples,
            "experiment_name": experiment["experiment_name"],
            "url": experiment["url"],
            "remote": check,
        }
    except ls.LangSmithUnavailable as error:
        return {"status": f"skipped: {error}"}
    except Exception as error:  # network/auth errors must not lose the local run
        return {"status": f"failed: {type(error).__name__}: {_scrub(str(error))}"}


def _print_outcome(run: dict, comparison: dict | None, directory: Path) -> None:
    counts = run["counts"]
    print()
    print(f"Run {run['run_id']}: {run['status']}")
    print(
        f"  ran {counts['ran']}/{counts['total']}  passed {counts['passed']}  "
        f"failed {counts['failed']}  errors {counts['errors']}  skipped {counts['skipped']}"
    )
    at_k = sorted((n for n in run["metrics"] if n.startswith("retrieval.recall_at_")),
                  key=lambda n: int(n.rsplit("_", 1)[1]))
    for name in ("run.pass_rate", *at_k, "retrieval.mrr", "lead.gate_false_save"):
        metric = run["metrics"].get(name)
        if metric and metric["value"] is not None:
            print(f"  {name}: {metric['value']:.3f} (n={metric['n']})")
    if comparison:
        regressions = comparison["regressions"]
        print("  regressions vs baseline: " + (", ".join(regressions) if regressions else "none"))
    notice = review_status.run_banner(run["dataset"])
    if notice:
        print(f"  note: {notice}")
    published = run.get("langsmith") or {}
    if published.get("status", "off") != "off":
        print(f"  LangSmith: {published['status']}"
              + (f" (dataset {published['dataset']}, experiment {published.get('experiment_name')})" if published.get("dataset") else ""))
    print(f"  results: {directory}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        dataset = load_dataset(args.dataset)
    except (DatasetError, OSError) as error:
        print(f"dataset error: {error}", file=sys.stderr)
        return EXIT_USAGE

    if args.case_id:
        known = {case.id for case in dataset.cases}
        unknown = [c for c in args.case_id if c not in known]
        if unknown:
            print(f"error: unknown --case-id: {', '.join(unknown)}", file=sys.stderr)
            return EXIT_USAGE
        wanted = set(args.case_id)
        # Same file (and sha), fewer cases: the run record shows n_cases for the selection.
        dataset = dataclasses.replace(dataset, cases=[c for c in dataset.cases if c.id in wanted])

    problem = _validate(args, dataset)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_USAGE

    replay_outputs = None
    if args.mode == "replay" and args.replay:
        from evals.targets import ReplayTarget

        try:
            replay_outputs = ReplayTarget(args.replay).outputs
        except (OSError, ValueError) as error:
            print(f"error: cannot read the replay file ({type(error).__name__})", file=sys.stderr)
            return EXIT_USAGE
    plan = estimate_llm_calls(dataset, args.mode, args.judge, args.limit, args.judge_suite, replay_outputs)
    print(f"Dataset {dataset.name}: {len(dataset.cases)} cases, {dataset.reviewed_count} human-reviewed"
          + (f", {dataset.excluded_count} excluded by reviewers" if dataset.excluded_count else ""))
    print(f"Mode {args.mode}: {plan['cases_to_run']} case(s) will run = "
          f"{plan['model_cases']} answer/lead case(s) + {plan['free_gate_cases']} free consent-gate case(s) (no model call)")
    if plan["skipped"]:
        reasons = "; ".join(f"{count} x {why}" for why, count in sorted(plan["skipped_reasons"].items()))
        print(f"Skipped: {plan['skipped']} ({reasons})")
    if args.mode == "full" or args.judge or args.judge_suite:
        extra = f" + {plan['suite_calls']} judge-suite" if args.judge_suite else ""
        print(
            f"Estimated LLM calls (upper bound): {plan['total']} "
            f"({plan['chat_calls']} agent + {plan['judge_calls']} judge{extra}); hard cap {args.max_llm_calls}"
        )
        if plan["total"] > args.max_llm_calls:
            print("warning: the estimate exceeds the cap; the run will stop as 'partial'")
    if args.dry_run:
        print("Dry run: nothing executed.")
        return EXIT_OK

    try:
        return asyncio.run(_execute(args, dataset, plan))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_PROBLEM


if __name__ == "__main__":
    raise SystemExit(main())
