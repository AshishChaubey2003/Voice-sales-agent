from functools import lru_cache

from fastembed import TextEmbedding

from api.config import get_settings


@lru_cache
def get_embedding_model() -> TextEmbedding:
    settings = get_settings()
    return TextEmbedding(
        model_name=settings.embedding_model,
        cache_dir=settings.embedding_cache_dir,
    )


def embed_passages(texts: list[str]) -> list[list[float]]:
    model = get_embedding_model()
    embed = getattr(model, "passage_embed", model.embed)
    return [vector.tolist() for vector in embed(texts)]


def embed_query(text: str) -> list[float]:
    model = get_embedding_model()
    embed = getattr(model, "query_embed", model.embed)
    return next(iter(embed([text]))).tolist()


def to_pgvector(values: list[float]) -> str:
    return "[" + ",".join(f"{value:.6f}" for value in values) + "]"


class EmbeddingDimensionError(RuntimeError):
    """The configured embedding model's output size does not match the fixed pgvector
    column dimension (DB-04). Without this check, the mismatch would otherwise only
    surface deep inside an ingestion insert, as an opaque database error."""


def check_embedding_dimension() -> int:
    """Fail fast, with a clear message, if EMBEDDING_MODEL does not emit vectors of
    exactly api.models.EMBEDDING_DIM length. Returns the actual dimension on success.

    Call this once before any embed_passages/embed_query call that will be written to
    the knowledge_chunks.embedding column - today that is scripts/ingest_knowledge.py.
    """
    from api.models import EMBEDDING_DIM

    actual = len(embed_query("dimension check"))
    if actual != EMBEDDING_DIM:
        raise EmbeddingDimensionError(
            f"The configured EMBEDDING_MODEL produces {actual}-dimensional "
            f"vectors, but the knowledge_chunks.embedding column is fixed at "
            f"{EMBEDDING_DIM} dimensions (api/models.py EMBEDDING_DIM). Either set "
            f"EMBEDDING_MODEL back to a {EMBEDDING_DIM}-dimensional model, or change "
            f"EMBEDDING_DIM and run a migration to match - inserting now would fail at "
            f"the database layer with a much less clear error."
        )
    return actual