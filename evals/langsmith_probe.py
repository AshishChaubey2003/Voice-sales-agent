"""Minimal, non-LLM LangSmith connectivity probe.

Why: an environment variable or a log line is not proof that LangSmith accepts
traces. This submits ONE tiny run (no prompt, no visitor text, no model call) to
the configured project and then reads it back from LangSmith. It reports success
only if the read-back returns the same run id.

It makes a network call to your LangSmith workspace, so it does nothing unless
you pass --confirm. The API key is read from the project settings (.env or the
process environment) and is never printed.

    python -m evals.langsmith_probe                    # shows what it would do
    python -m evals.langsmith_probe --confirm          # submits and reads back one run
    python -m evals.langsmith_probe --confirm --project voice-sales-agent-evals
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
import warnings
from datetime import datetime, timezone

DEFAULT_WAIT_SECONDS = 30.0
POLL_SECONDS = 2.0


def make_client(settings):
    """Real client from project settings. Imported lazily; the SDK is optional."""
    from langsmith import Client

    kwargs = {"api_key": settings.langsmith_api_key.get_secret_value()}
    if settings.langsmith_endpoint:
        kwargs["api_url"] = settings.langsmith_endpoint
    return Client(**kwargs)


def _read_run(client, run_id):
    """read_run is deprecated (removal announced for 2027); fall back to runs.retrieve if it goes away."""
    reader = getattr(client, "read_run", None)
    if reader is not None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            return reader(run_id)
    return client.runs.retrieve(run_id)


def recent_runs(client, project: str, limit: int = 10) -> list[dict]:
    """Newest runs in a project: ids, names, types, status and times only. No inputs or outputs."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        runs = client.list_runs(project_name=project, limit=limit)
    out = []
    for run in runs:
        out.append(
            {
                "id": str(getattr(run, "id", "")),
                "name": getattr(run, "name", None),
                "run_type": getattr(run, "run_type", None),
                "status": getattr(run, "status", None),
                "start_time": str(getattr(run, "start_time", "")),
                "has_error": bool(getattr(run, "error", None)),
            }
        )
    return out


def run_probe(client, project: str, *, wait_seconds: float = DEFAULT_WAIT_SECONDS, sleep=time.sleep, clock=time.monotonic) -> dict:
    """Submit one run and read it back. Returns a result dict; never raises on API errors."""
    run_id = uuid.uuid4()
    result = {"project": project, "run_id": str(run_id), "submitted": False, "verified": False, "error": None}
    try:
        client.create_run(
            id=run_id,
            name="vsa-connectivity-probe",
            run_type="chain",
            inputs={"probe": True},
            project_name=project,
            start_time=datetime.now(timezone.utc),
            tags=["probe"],
        )
        client.update_run(run_id, outputs={"ok": True}, end_time=datetime.now(timezone.utc))
        flush = getattr(client, "flush", None)
        if callable(flush):
            flush()
        result["submitted"] = True
    except Exception as error:  # report the type only: messages can echo request details
        result["error"] = type(error).__name__
        return result

    deadline = clock() + wait_seconds
    while True:
        try:
            found = _read_run(client, run_id)
            if found is not None and str(getattr(found, "id", "")) == str(run_id):
                result["verified"] = True
                result["error"] = None  # earlier "not found yet" errors are no longer relevant
                return result
        except Exception as error:
            result["error"] = type(error).__name__  # not found yet is normal while ingesting
        if clock() >= deadline:
            return result
        sleep(POLL_SECONDS)


def verify_turn(client, project: str, trace_id: str, session_id: str, expected_children: int = 1,
                *, wait_seconds: float = DEFAULT_WAIT_SECONDS, sleep=time.sleep, clock=time.monotonic) -> dict:
    """Reads a trace back: one root carrying session_id, with children nested under it. Ids only."""
    deadline = clock() + wait_seconds
    report = {"trace_id": trace_id, "root_found": False, "root_session_id_ok": False,
              "children": 0, "children_nested": False, "verified": False}
    while True:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                runs = list(client.list_runs(project_name=project, trace_id=trace_id))
            roots = [r for r in runs if getattr(r, "parent_run_id", None) is None]
            kids = [r for r in runs if getattr(r, "parent_run_id", None) is not None]
            report["root_found"] = bool(roots)
            if roots:
                metadata = ((getattr(roots[0], "extra", None) or {}).get("metadata")) or {}
                report["root_session_id_ok"] = metadata.get("session_id") == session_id
                report["root_name"] = getattr(roots[0], "name", None)
                report["children"] = len(kids)
                report["children_nested"] = bool(kids) and all(
                    str(k.parent_run_id) == str(roots[0].id) for k in kids
                )
        except Exception as error:
            report["error"] = type(error).__name__
        report["verified"] = bool(
            report["root_found"] and report["root_session_id_ok"]
            and report["children"] >= expected_children and report["children_nested"]
        )
        if report["verified"] or clock() >= deadline:
            return report
        sleep(POLL_SECONDS)


def verify_chat_traces(client, project: str, conversations: list[dict], sensitive: list[str], *,
                       wait_seconds: float = DEFAULT_WAIT_SECONDS, sleep=time.sleep, clock=time.monotonic) -> dict:
    """Reads real chat traces back by conversation id.

    `conversations`: [{"conversation_id", "expect_tool": bool}]. `sensitive`: strings (visitor
    messages, emails) that must NOT appear anywhere in any run's inputs or outputs. Only booleans,
    counts and ids are reported; the sensitive strings are never printed.
    """
    import json as _json
    from collections import Counter

    needles = [x.lower() for x in sensitive if isinstance(x, str) and len(x) >= 6]
    deadline = clock() + wait_seconds
    while True:
        rows = []
        for conv in conversations:
            cid = conv["conversation_id"]
            row = {"conversation_id": cid, "root_found": False, "children": {}, "all_nested": False,
                   "has_error": None, "root_ms": None, "token_usage_recorded": False,
                   "leak_found": None, "root_input_keys": [], "expected_spans_present": False}
            try:
                query = f'and(eq(metadata_key, "session_id"), eq(metadata_value, "{cid}"))'
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    roots = [r for r in client.list_runs(project_name=project, is_root=True, filter=query)
                             if getattr(r, "name", None) == "chat_turn"]
                    if roots:
                        root = roots[0]
                        runs = list(client.list_runs(project_name=project, trace_id=str(root.trace_id)))
                        kids = [r for r in runs if str(r.id) != str(root.id)]
                        ids = {str(r.id) for r in runs}
                        row.update(
                            root_found=True,
                            trace_id=str(root.trace_id),
                            children=dict(Counter(r.name for r in kids)),
                            all_nested=bool(kids) and all(str(r.parent_run_id) in ids for r in kids),
                            has_error=any(getattr(r, "error", None) for r in runs),
                            root_input_keys=sorted((root.inputs or {}).keys()),
                        )
                        if getattr(root, "start_time", None) and getattr(root, "end_time", None):
                            row["root_ms"] = int((root.end_time - root.start_time).total_seconds() * 1000)
                        row["token_usage_recorded"] = any(
                            r.name == "llm_call" and isinstance((r.outputs or {}).get("usage"), dict) and (r.outputs or {}).get("usage")
                            for r in kids
                        )
                        blob = _json.dumps([{"i": r.inputs, "o": r.outputs} for r in runs], default=str).lower()
                        row["leak_found"] = any(n in blob for n in needles)
                        need = {"retrieval", "llm_call"} | ({"tool_save_lead"} if conv.get("expect_tool") else set())
                        row["expected_spans_present"] = need <= set(row["children"])
            except Exception as error:
                row["error"] = type(error).__name__
            rows.append(row)
        ok = bool(rows) and all(
            r["root_found"] and r["all_nested"] and r["expected_spans_present"] and r["leak_found"] is False
            for r in rows
        )
        if ok or clock() >= deadline:
            return {"verified": ok, "conversations": rows}
        sleep(POLL_SECONDS)


def emit_turn_probe() -> tuple[str, str]:
    """Emits one synthetic chat turn with a retrieval child through the REAL tracing code.

    Synthetic text only; no model call, no database, no visitor data.
    """
    import asyncio
    from types import SimpleNamespace

    from api import tracing

    session_id = str(uuid.uuid4())

    @tracing.traced("retrieval", "retriever", process_inputs=tracing.retrieval_inputs,
                    process_outputs=tracing.retrieval_outputs)
    async def retrieve(query, top_k=4, max_distance=0.3):
        return SimpleNamespace(chunks=[], elapsed_ms=1)

    async def go():
        with tracing.turn_trace("chat_turn", session_id=session_id, channel="probe",
                                inputs=tracing.turn_inputs("synthetic probe message")) as turn:
            await retrieve("synthetic probe message")
            turn.set_outputs(n_sources=0, probe=True)
            return turn

    turn = asyncio.run(go())
    if not turn.active:
        raise RuntimeError("tracing is not enabled (needs LANGSMITH_TRACING=true, a key and the SDK)")
    trace_id = str(getattr(turn._run, "trace_id", None) or turn._run.id)
    try:
        from langsmith import wait_for_all_tracers

        wait_for_all_tracers()
    except Exception:
        pass
    return trace_id, session_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Submit one non-LLM test run to LangSmith and read it back.")
    parser.add_argument("--confirm", action="store_true", help="actually call LangSmith (otherwise only a plan is printed)")
    parser.add_argument("--project", help="project to write to (default: LANGSMITH_PROJECT setting)")
    parser.add_argument("--recent", type=int, metavar="N", help="list the N newest runs in the project (ids, names, status only); makes a read-only network call")
    parser.add_argument("--verify-chat-run", metavar="RUN_ID",
                        help="read back the traces of a real `evals.run --mode full` run (checks nesting and redaction)")
    parser.add_argument("--results-dir", default="evals/results")
    parser.add_argument("--turn-probe", action="store_true",
                        help="emit one synthetic chat_turn with a retrieval child and read the nesting back")
    parser.add_argument("--check-experiment", nargs=2, metavar=("DATASET", "EXPERIMENT"),
                        help="read-only: confirm a published dataset and experiment exist and count their rows")
    parser.add_argument("--expect", type=int, default=1, help="rows expected for --check-experiment")
    parser.add_argument("--wait", type=float, default=DEFAULT_WAIT_SECONDS, help="seconds to wait for read-back")
    args = parser.parse_args(argv)

    from api.config import get_settings

    settings = get_settings()
    project = args.project or settings.langsmith_project
    has_key = settings.langsmith_api_key is not None
    try:
        import langsmith  # noqa: F401

        has_sdk = True
    except ImportError:
        has_sdk = False

    print(f"langsmith SDK installed: {has_sdk}")
    print(f"API key configured:      {has_key}   (value never printed)")
    print(f"tracing flag in settings: {settings.langsmith_tracing}")
    print(f"target project:          {project}")
    print(f"custom endpoint:         {bool(settings.langsmith_endpoint)}")
    if not has_sdk:
        print("Cannot probe: install it with  pip install -r requirements-eval.txt", file=sys.stderr)
        return 2
    if not has_key:
        print("Cannot probe: set LANGSMITH_API_KEY in your .env (do not paste it into chat or code).", file=sys.stderr)
        return 2
    if args.verify_chat_run:
        import json as _json
        import re as _re

        from evals import store

        cases = [c for c in store.load_cases(args.results_dir, args.verify_chat_run) if c.get("status") == "ok"]
        conversations = [{"conversation_id": c["output"]["conversation_id"],
                          "expect_tool": c["output"].get("lead_status") == "saved"}
                         for c in cases if isinstance(c.get("output"), dict) and c["output"].get("conversation_id")]
        if not conversations:
            print("No conversation ids in that run (it must be a --mode full run made after this feature was added).", file=sys.stderr)
            return 2
        if not args.confirm:
            print(f"Plan only: would read {len(conversations)} chat trace(s) from {project} by conversation id. Add --confirm.")
            return 0
        from evals.schema import load_dataset
        from evals.run import DEFAULT_DATASET

        wanted = {c["case_id"] for c in cases}
        sensitive = []
        for case in load_dataset(DEFAULT_DATASET).cases:
            if case.id in wanted:
                for message in case.messages:
                    sensitive.append(message["content"])
                    sensitive += _re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", message["content"])
        report = verify_chat_traces(make_client(settings), project, conversations, sensitive, wait_seconds=args.wait)
        print(_json.dumps(report, indent=2))
        return 0 if report["verified"] else 1
    if args.turn_probe:
        if not args.confirm:
            print(f"Plan only: would emit one synthetic 'chat_turn' (+ one retrieval child, no model call) to {project} and read it back. Add --confirm.")
            return 0
        try:
            trace_id, session_id = emit_turn_probe()
        except Exception as error:
            print(f"Could not emit the probe turn: {type(error).__name__}: {str(error)[:150]}", file=sys.stderr)
            return 1
        report = verify_turn(make_client(settings), project, trace_id, session_id, wait_seconds=args.wait)
        print(f"  session_id: {session_id}")
        for key, value in report.items():
            print(f"  {key}: {value}")
        return 0 if report["verified"] else 1
    if args.check_experiment:
        if not args.confirm:
            print("Plan only: would read the named dataset and experiment (counts and ids only). Add --confirm.")
            return 0
        from evals.langsmith_sync import verify_publish

        report = verify_publish(make_client(settings), args.check_experiment[0], args.check_experiment[1],
                                args.expect, wait_seconds=args.wait)
        for key, value in report.items():
            print(f"  {key}: {value}")
        return 0 if report["verified"] else 1
    if args.recent:
        if not args.confirm:
            print(f"Plan only: would list the {args.recent} newest runs in project {project} (metadata only). Add --confirm.")
            return 0
        try:
            runs = recent_runs(make_client(settings), project, args.recent)
        except Exception as error:
            print(f"Could not list runs ({type(error).__name__}).", file=sys.stderr)
            return 1
        print(f"\n{len(runs)} run(s) in {project}:")
        for run in runs:
            print(f"  {run['start_time'][:19]}  {run['run_type'] or '-':<9} {run['name'] or '-':<28} {run['status'] or '-':<8} error={run['has_error']}  {run['id']}")
        return 0
    if not args.confirm:
        print("\nPlan only: would submit one run named 'vsa-connectivity-probe' with inputs {'probe': true}, then read it back.")
        print("Re-run with --confirm to make the network call.")
        return 0

    result = run_probe(make_client(settings), project, wait_seconds=args.wait)
    print(f"\nsubmitted: {result['submitted']}   verified by read-back: {result['verified']}")
    print(f"project:   {result['project']}")
    print(f"run id:    {result['run_id']}")
    if result["error"]:
        print(f"last error type: {result['error']}")
    if result["verified"]:
        print("LangSmith accepted the run and returned it. Check the project in the LangSmith UI to see it.")
        return 0
    print("NOT verified: do not treat LangSmith as connected.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
