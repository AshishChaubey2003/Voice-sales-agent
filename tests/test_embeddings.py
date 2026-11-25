"""Regression tests for DB-04: EMBEDDING_MODEL must match the fixed pgvector column
dimension, and a mismatch must fail fast and clearly rather than surface as an opaque
database error. These tests never load a real embedding model (no network needed here):
embed_query is monkeypatched to return vectors of a chosen length."""

import pytest

import api.embeddings as embeddings_module
from api.embeddings import EmbeddingDimensionError, check_embedding_dimension
from api.models import EMBEDDING_DIM


def test_matching_dimension_passes_and_returns_the_actual_size(monkeypatch):
    monkeypatch.setattr(embeddings_module, "embed_query", lambda text: [0.0] * EMBEDDING_DIM)

    assert check_embedding_dimension() == EMBEDDING_DIM


def test_mismatched_dimension_fails_fast_with_a_clear_message(monkeypatch):
    wrong_dim = EMBEDDING_DIM + 1
    monkeypatch.setattr(embeddings_module, "embed_query", lambda text: [0.0] * wrong_dim)

    with pytest.raises(EmbeddingDimensionError) as exc_info:
        check_embedding_dimension()

    message = str(exc_info.value)
    assert str(wrong_dim) in message
    assert str(EMBEDDING_DIM) in message
    assert "EMBEDDING_MODEL" in message
    assert "migration" in message
