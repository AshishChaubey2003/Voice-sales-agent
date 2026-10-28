"""Read-only API behind the evaluation dashboard.

Every data route requires an organization API key (get_current_org) and returns
only that organization's evaluation runs. A run belonging to another
organization is reported as 404, exactly like a run that does not exist.

Runs are files written by `python -m evals.run` (see evals/store.py); nothing here
writes, and no secrets are read or returned. Run ids are validated against a
strict pattern before touching the filesystem.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.deps import get_current_org, get_session
from api.models import Conversation, Message, Organization
from evals import compare, registry, store
from evals.voice_latency import summarize_voice_latency

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/evals", tags=["evaluations"])
page_router = APIRouter(tags=["evaluations"])

_DASHBOARD_HTML = Path(__file__).parent / "static" / "eval_dashboard.html"
_VOICE_ROWS_CAP = 20_000


def _results_dir() -> str:
    return get_settings().eval_results_dir


def _baselines_dir() -> str:
    # Baselines live beside the code, not with the (git-ignored) results.
    return str(Path(__file__).resolve().parent.parent / "evals" / "baselines")


def _own_run(run_id: str, org: Organization) -> dict:
    run = store.load_run(_results_dir(), run_id)
    if run is None or run.get("organization_id") != str(org.id):
        raise HTTPException(status_code=404, detail="Run not found")
    return run


def _run_summary(run: dict) -> dict:
    metrics = run.get("metrics", {})

    def value(name: str):
        metric = metrics.get(name)
        return metric["value"] if metric else None

    return {
        "run_id": run["run_id"],
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "status": run.get("status"),
        "mode": run.get("mode"),
        "dataset": run.get("dataset"),
        "model": run.get("model"),
        "judge": run.get("judge"),
        "code_version": run.get("code_version"),
        "prompt_fingerprint": run.get("prompt_fingerprint"),
        "counts": run.get("counts"),
        "langsmith": run.get("langsmith"),
        "comparison": run.get("comparison"),
        "headline": {
            "pass_rate": value("run.pass_rate"),
            "error_rate": value("run.error_rate"),
            "recall_at_4": value("retrieval.recall_at_4"),
            "mrr": value("retrieval.mrr"),
            "required_facts": value("answer.required_facts"),
            "faithfulness": value("judge.faithfulness"),
            "relevance": value("judge.relevance"),
            "gate_false_save": value("lead.gate_false_save"),
            "eval_pii_leak": value("eval.pii_leak"),
            "eval_faithfulness": value("eval.faithfulness"),
            "eval_relevance": value("eval.relevance"),
            "eval_hallucination": value("eval.hallucination"),
        },
        "judge_suite": run.get("judge_suite"),
    }


def _evaluator_issue(case: dict) -> bool:
    return any(
        e.get("status") == "error" or e.get("verdict") == "fail"
        for e in case.get("evaluators") or []
    )


@router.get("/runs")
async def list_runs(
    limit: int = Query(20, ge=1, le=100),
    org: Organization = Depends(get_current_org),
):
    runs = store.list_runs(_results_dir(), str(org.id))[:limit]
    return {"runs": [_run_summary(run) for run in runs]}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, org: Organization = Depends(get_current_org)):
    return _own_run(run_id, org)


@router.get("/runs/{run_id}/cases")
async def get_cases(
    run_id: str,
    failed_only: bool = True,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    org: Organization = Depends(get_current_org),
):
    _own_run(run_id, org)
    cases = store.load_cases(_results_dir(), run_id)
    if failed_only:
        # Cases that need attention: failed, errored, or flagged by an evaluator (a judge that
        # failed the reply, or an evaluator that errored). A passing case is still listed when
        # an evaluator disagrees, because the evaluators never change pass/fail themselves.
        cases = [
            c for c in cases
            if c.get("status") in ("ok", "error")
            and (c.get("passed") is not True or _evaluator_issue(c))
        ]
    return {"total": len(cases), "cases": cases[offset : offset + limit]}


@router.get("/registry")
async def get_registry(org: Organization = Depends(get_current_org)):
    """Static metric and evaluator catalogue: definitions and honest implementation status."""
    return registry.catalogue()


@router.get("/runs/{run_id}/metrics")
async def get_run_metrics(run_id: str, org: Organization = Depends(get_current_org)):
    """Registry metrics resolved against this run, with numerators, denominators and intervals."""
    return {"metrics": registry.resolve_metrics(_own_run(run_id, org))}


@router.get("/monitoring")
async def get_monitoring(org: Organization = Depends(get_current_org)):
    """In-process online-monitoring snapshot. Honest about being off or empty."""
    from api.monitoring import get_monitor

    monitor = get_monitor()
    if monitor is None:
        return {"enabled": False, "note": "ONLINE_MONITORING is off; no live monitoring is active."}
    return {"enabled": True, **monitor.org_snapshot(str(org.id))}


@router.get("/baselines")
async def list_baselines(org: Organization = Depends(get_current_org)):
    root = Path(_baselines_dir())
    names = []
    if root.is_dir():
        for path in sorted(root.glob("*.json")):
            baseline = store.load_baseline(root, path.stem)
            if baseline and baseline.get("organization_id") == str(org.id):
                names.append(path.stem)
    return {"baselines": names}


@router.get("/runs/{run_id}/compare")
async def compare_with_baseline(
    run_id: str,
    baseline: str = Query(..., min_length=1, max_length=64),
    org: Organization = Depends(get_current_org),
):
    run = _own_run(run_id, org)
    try:
        saved = store.load_baseline(_baselines_dir(), baseline)
    except ValueError:
        saved = None
    # A baseline from another organization is indistinguishable from a missing one.
    if saved is None or saved.get("organization_id") != str(org.id):
        raise HTTPException(status_code=404, detail="Baseline not found")
    return compare.compare_runs(run, saved)


@router.get("/voice-latency")
async def voice_latency(
    days: int = Query(30, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
    org: Organization = Depends(get_current_org),
):
    """Percentiles of real voice calls for this organization, from stored telemetry."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    result = await session.execute(
        select(Message.role, Message.latency_ms)
        .join(
            Conversation,
            (Conversation.id == Message.conversation_id)
            & (Conversation.organization_id == Message.organization_id),
        )
        .where(
            Message.organization_id == org.id,
            Conversation.channel == "voice",
            Message.latency_ms.is_not(None),
            Message.created_at >= since,
        )
        .order_by(Message.created_at.desc())
        .limit(_VOICE_ROWS_CAP)
    )
    rows = result.all()
    summary = summarize_voice_latency(rows)
    summary["window_days"] = days
    summary["truncated"] = len(rows) >= _VOICE_ROWS_CAP
    return summary


@page_router.get("/dashboard/evals", response_class=HTMLResponse, include_in_schema=False)
async def evals_dashboard():
    """The page itself carries no data; it calls the authenticated routes above."""
    nonce = secrets.token_urlsafe(16)
    html = _DASHBOARD_HTML.read_text(encoding="utf-8").replace("__NONCE__", nonce)
    headers = {
        "Content-Security-Policy": (
            "default-src 'none'; "
            f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        ),
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
    return HTMLResponse(html, headers=headers)
