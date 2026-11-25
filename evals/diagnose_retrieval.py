"""Read-only diagnosis of empty retrievals.

Why this exists: the live path (api.knowledge.search_knowledge) applies the
distance gate *before* returning anything, so a question whose nearest chunk sits
just outside the gate returns nothing and the nearest candidates are invisible.
This tool runs the same query embedding (api.embeddings.embed_query) and the same
vector SQL (api.knowledge.VECTOR_SEARCH) but keeps every candidate and reports
each distance next to the configured gate.

Direction matters, and is easy to get backwards:

    pgvector `<=>` returns cosine DISTANCE  = 1 - cosine SIMILARITY
    distance : LOWER is closer.  A chunk passes the gate when distance <= gate.
    similarity: HIGHER is closer.  similarity = 1 - distance.

Only SELECT statements are issued. Nothing is written, no LLM is called, no
configuration value other than the gate and the embedding model name is printed.

    python -m evals.diagnose_retrieval --run evals\\results\\<run_id>
    python -m evals.diagnose_retrieval --case-id fact-import --case-id fact-export
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

DEFAULT_DATASET = "evals/datasets/nimbuscrm_golden_v1.jsonl"
CANDIDATE_LIMIT = 200  # effectively "every chunk" for a demo-sized knowledge base


# --- pure logic (unit-tested) ------------------------------------------------


def similarity(distance: float) -> float:
    """Cosine similarity from pgvector cosine distance."""
    return 1.0 - distance


def gate_status(distance: float, gate: float | None) -> str:
    if gate is None:
        return "no-gate"
    return "within" if distance <= gate else "outside"


def failed_empty_case_ids(cases_jsonl: str) -> list[str]:
    """Ids of answerable cases that ran, failed, and retrieved nothing."""
    ids = []
    for line in cases_jsonl.splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        scores = case.get("scores") or {}
        if (
            case.get("status") == "ok"
            and case.get("passed") is False
            and scores.get("empty_retrieval") == 1.0
        ):
            ids.append(case["case_id"])
    return ids


def analyze(candidates: list[tuple[str, float]], expected: list[str], gate: float | None) -> dict:
    """candidates: (heading, distance) pairs. Sorted here so input order never matters."""
    ranked = sorted(candidates, key=lambda item: item[1])
    result: dict = {"gate": gate, "candidates": [], "expected": []}
    for rank, (heading, distance) in enumerate(ranked, start=1):
        result["candidates"].append(
            {
                "rank": rank,
                "heading": heading,
                "distance": distance,
                "similarity": similarity(distance),
                "gate": gate_status(distance, gate),
                "expected": heading in expected,
            }
        )
    by_heading = {c["heading"]: c for c in result["candidates"]}
    for heading in expected:
        hit = by_heading.get(heading)
        if hit is None:
            result["expected"].append({"heading": heading, "found": False})
            continue
        gap = None if gate is None else hit["distance"] - gate
        result["expected"].append(
            {
                "heading": heading,
                "found": True,
                "rank": hit["rank"],
                "distance": hit["distance"],
                "gate": hit["gate"],
                # Positive = how far beyond the gate (in distance units); <= 0 = inside.
                "distance_over_gate": gap,
            }
        )
    top = result["candidates"][0] if result["candidates"] else None
    result["top"] = top
    result["within_gate_count"] = sum(1 for c in result["candidates"] if c["gate"] == "within")
    return result


def render_table(rows: list[dict], gate: float | None) -> str:
    header = (
        f"Gate: distance <= {gate}  (cosine DISTANCE, lower = closer; similarity = 1 - distance)\n"
        f"{'case':<24}{'top dist':>9}{'top sim':>9} {'gate':<8}{'expected section':<52}"
        f"{'exp rank':>9}{'exp dist':>9}{'over gate':>10}"
    )
    lines = [header]
    for row in rows:
        top = row["analysis"]["top"]
        expected = row["analysis"]["expected"][0] if row["analysis"]["expected"] else {}
        if top is None:
            lines.append(f"{row['case_id']:<24}  (no candidates)")
            continue
        exp_rank = expected.get("rank", "-")
        exp_dist = f"{expected['distance']:.3f}" if expected.get("found") else "-"
        over = (
            f"{expected['distance_over_gate']:+.3f}"
            if expected.get("found") and expected.get("distance_over_gate") is not None
            else "-"
        )
        lines.append(
            f"{row['case_id']:<24}{top['distance']:>9.3f}{top['similarity']:>9.3f} {top['gate']:<8}"
            f"{(expected.get('heading') or '-')[:50]:<52}{exp_rank:>9}{exp_dist:>9}{over:>10}"
        )
    return "\n".join(lines)


def render_detail(row: dict, top_n: int) -> str:
    analysis = row["analysis"]
    out = [f"\n{row['case_id']}: {row['query']}"]
    out.append(f"  expected: {', '.join(row['expected']) or '-'}")
    for cand in analysis["candidates"][:top_n]:
        marker = "*" if cand["expected"] else " "
        out.append(
            f"  {marker}[{cand['rank']}] dist {cand['distance']:.3f}  sim {cand['similarity']:.3f}  "
            f"{cand['gate']:<7} {cand['heading']}"
        )
    kw = row.get("keyword_headings")
    if kw is not None:
        out.append(f"  keyword matches (ungated): {', '.join(kw) if kw else 'none'}")
    return "\n".join(out)


# --- database part (read-only) ------------------------------------------------


async def fetch(case_queries: dict[str, str], org_slug: str):
    from sqlalchemy import select

    from api.config import get_settings
    from api.db import build_engine, build_session_factory
    from api.embeddings import embed_query, to_pgvector
    from api.knowledge import KEYWORD_SEARCH, VECTOR_SEARCH
    from api.models import Organization

    settings = get_settings()
    engine = build_engine()
    out: dict[str, dict] = {}
    try:
        async with build_session_factory(engine)() as session:
            org = await session.scalar(select(Organization).where(Organization.slug == org_slug))
            if org is None:
                raise SystemExit(f"Organization '{org_slug}' not found")
            for case_id, query in case_queries.items():
                vector = await asyncio.to_thread(embed_query, query)
                base = {"organization_id": org.id, "limit": CANDIDATE_LIMIT}
                vrows = (
                    await session.execute(VECTOR_SEARCH, {**base, "query_vector": to_pgvector(vector)})
                ).mappings().all()
                krows = (await session.execute(KEYWORD_SEARCH, {**base, "query": query})).mappings().all()
                out[case_id] = {
                    "candidates": [(r["heading"], float(r["distance"])) for r in vrows],
                    "keyword": [r["heading"] for r in krows],
                }
    finally:
        await engine.dispose()
    return settings, out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show distances behind empty retrievals (read-only).")
    parser.add_argument("--run", help="results directory of an evaluation run; selects its empty-retrieval failures")
    parser.add_argument("--case-id", action="append", default=[], help="explicit case id (repeatable)")
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--org", default="nimbus-crm-demo")
    parser.add_argument("--top", type=int, default=4, help="candidates to list per question")
    parser.add_argument("--json", dest="json_out", help="optionally write the raw findings to this new file")
    args = parser.parse_args(argv)

    from evals.schema import load_dataset

    ids = list(args.case_id)
    if args.run:
        ids += failed_empty_case_ids((Path(args.run) / "cases.jsonl").read_text(encoding="utf-8"))
    ids = list(dict.fromkeys(ids))
    if not ids:
        print("No cases selected: pass --run <results dir> or --case-id <id>.", file=sys.stderr)
        return 2

    dataset = load_dataset(args.dataset)
    by_id = {case.id: case for case in dataset.cases}
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        print(f"Unknown case id(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    settings, fetched = asyncio.run(fetch({i: by_id[i].query for i in ids}, args.org))
    gate = settings.rag_max_distance
    rows = []
    for case_id in ids:
        case = by_id[case_id]
        rows.append(
            {
                "case_id": case_id,
                "query": case.query,
                "expected": case.expected_sources,
                "analysis": analyze(fetched[case_id]["candidates"], case.expected_sources, gate),
                "keyword_headings": fetched[case_id]["keyword"],
            }
        )

    print(f"Embedding model: {settings.embedding_model}   configured gate: {gate}   (read-only, no LLM calls)")
    print(render_table(rows, gate))
    for row in rows:
        print(render_detail(row, args.top))
    if args.json_out:
        target = Path(args.json_out)
        if target.exists():
            print(f"Refusing to overwrite {target}", file=sys.stderr)
            return 2
        target.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
