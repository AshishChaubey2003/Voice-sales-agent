import argparse
import asyncio
import hashlib
import uuid
from pathlib import Path

from sqlalchemy import select, text

from api.chunking import split_markdown
from api.db import build_engine, build_session_factory
from api.embeddings import check_embedding_dimension, embed_passages, to_pgvector
from api.models import KnowledgeDocument, Organization

INSERT_CHUNK = text(
    """
    INSERT INTO knowledge_chunks
        (id, organization_id, document_id, chunk_index, heading, content, embedding)
    VALUES
        (:id, :organization_id, :document_id, :chunk_index, :heading, :content,
         CAST(CAST(:embedding AS text) AS vector))
    """
)


def read_title(path: Path, markdown: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem.replace("-", " ").title()


async def ingest(folder: Path, org_slug: str) -> None:
    files = sorted(folder.glob("*.md"))
    if not files:
        raise SystemExit(f"No .md files found in {folder}")

    # DB-04: fail fast and clearly if EMBEDDING_MODEL no longer matches the fixed
    # pgvector column dimension, rather than letting the mismatch surface as an opaque
    # database error partway through inserting chunks.
    check_embedding_dimension()

    engine = build_engine()
    session_factory = build_session_factory(engine)
    try:
        async with session_factory() as session:
            org = await session.scalar(
                select(Organization).where(Organization.slug == org_slug)
            )
            if org is None:
                raise SystemExit(
                    f"Organization '{org_slug}' not found. Run: python -m scripts.seed_demo"
                )

            for path in files:
                markdown = path.read_text(encoding="utf-8")
                content_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
                source = path.as_posix()

                existing = await session.scalar(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.organization_id == org.id,
                        KnowledgeDocument.source == source,
                    )
                )
                if existing and existing.content_hash == content_hash:
                    print(f"unchanged  {source}")
                    continue
                if existing:
                    await session.delete(existing)
                    await session.flush()

                title = read_title(path, markdown)
                chunks = split_markdown(title, markdown)
                if not chunks:
                    print(f"skipped    {source} (no content)")
                    continue

                document = KnowledgeDocument(
                    organization_id=org.id,
                    title=title,
                    source=source,
                    content_hash=content_hash,
                )
                session.add(document)
                await session.flush()

                vectors = await asyncio.to_thread(
                    embed_passages, [f"{chunk.heading}\n{chunk.content}" for chunk in chunks]
                )
                for index, (chunk, vector) in enumerate(zip(chunks, vectors)):
                    await session.execute(
                        INSERT_CHUNK,
                        {
                            "id": uuid.uuid4(),
                            "organization_id": org.id,
                            "document_id": document.id,
                            "chunk_index": index,
                            "heading": chunk.heading,
                            "content": chunk.content,
                            "embedding": to_pgvector(vector),
                        },
                    )
                print(f"ingested   {source} ({len(chunks)} chunks)")

            await session.commit()
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Load markdown documents into the knowledge base.")
    parser.add_argument("--folder", default="knowledge/nimbuscrm")
    parser.add_argument("--org", default="nimbus-crm-demo")
    args = parser.parse_args()
    asyncio.run(ingest(Path(args.folder), args.org))


if __name__ == "__main__":
    main()