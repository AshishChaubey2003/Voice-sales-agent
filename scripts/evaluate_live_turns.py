"""Evaluate already-persisted assistant turns and store the results (idempotent).

    python -m scripts.evaluate_live_turns --org <organization-slug> [--days 30] [--conversation-id UUID] [--force]

Deterministic checks only: no LLM call and no external request. Re-running is safe because
(message, evaluator version) is unique in the database. --force replaces the stored result for the same
evaluator version (use it after the reference dataset has been reviewed). Needs the turn_evaluations
migration: alembic upgrade head.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

from sqlalchemy import select

from api.db import build_engine, build_session_factory
from api.live_eval import MAX_BATCH, evaluate_turns, summarize_live
from api.models import Organization


async def main_async(args) -> int:
    engine = build_engine()
    try:
        factory = build_session_factory(engine)
        async with factory() as session:
            org = await session.scalar(select(Organization).where(Organization.slug == args.org))
            if org is None:
                print(f"No organization with slug {args.org!r}.", file=sys.stderr)
                return 2
            totals = {"selected": 0, "written": 0, "by_status": {}}
            while True:
                batch = await evaluate_turns(
                    session, org, conversation_id=args.conversation_id, days=args.days,
                    force=args.force, limit=args.batch_size,
                )
                totals["selected"] += batch["selected"]
                totals["written"] += batch["written"]
                for status, n in batch["by_status"].items():
                    totals["by_status"][status] = totals["by_status"].get(status, 0) + n
                # force would reselect the same rows forever, so it runs a single batch per call
                if args.force or batch["selected"] == 0 or batch["written"] == 0 or batch["remaining_pending"] == 0:
                    break
            summary = await summarize_live(session, org, days=args.days or 30, conversation_id=args.conversation_id)
            print(json.dumps({"this_run": totals, "pending_after": summary["pending"],
                              "counts": summary["counts"], "pass_rate": summary["pass_rate"]}, indent=2))
            return 0
    finally:
        await engine.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--org", required=True, help="organization slug")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--conversation-id", type=uuid.UUID)
    parser.add_argument("--batch-size", type=int, default=MAX_BATCH)
    parser.add_argument("--force", action="store_true", help="re-evaluate turns that already have a result")
    return asyncio.run(main_async(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
