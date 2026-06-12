"""Semantic memory tests (slice H).

Covers:
- StubEmbedder determinism, dimensionality, and unit-norm.
- Different texts produce different vectors.
- make_embedder returns None for provider "none".
- memory_write / memory_search with embeddings (vector path, skipped when
  pgvector is absent).
- ILIKE fallback path (always exercised regardless of pgvector).
"""

from __future__ import annotations

import math
import pytest

from orchestrator.embeddings import StubEmbedder, make_embedder
from orchestrator.config import Settings
from orchestrator import repository as repo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (_norm(a) * _norm(b))


# ---------------------------------------------------------------------------
# StubEmbedder unit tests (no DB required)
# ---------------------------------------------------------------------------

def test_stub_embed_returns_256_floats():
    emb = StubEmbedder()
    vec = emb.embed("hello world")
    assert len(vec) == 256
    assert all(isinstance(v, float) for v in vec)


def test_stub_embed_is_deterministic():
    emb = StubEmbedder()
    text = "Postgres is the canonical state store"
    assert emb.embed(text) == emb.embed(text)


def test_stub_embed_unit_norm():
    emb = StubEmbedder()
    vec = emb.embed("unit norm check")
    assert abs(_norm(vec) - 1.0) < 1e-6


def test_stub_embed_different_texts_differ():
    emb = StubEmbedder()
    a = emb.embed("semantic memory with pgvector")
    b = emb.embed("completely unrelated banana pancakes")
    # They should not be identical.
    assert a != b


def test_stub_embed_similar_texts_higher_similarity():
    """Texts that share tokens should have higher cosine similarity than
    completely unrelated texts."""
    emb = StubEmbedder()
    base = emb.embed("memory store database")
    similar = emb.embed("memory store database retrieval")
    unrelated = emb.embed("banana pancake recipe flour")
    assert _cosine(base, similar) > _cosine(base, unrelated)


def test_stub_embed_empty_string():
    emb = StubEmbedder()
    vec = emb.embed("")
    assert len(vec) == 256
    assert abs(_norm(vec) - 1.0) < 1e-6


# ---------------------------------------------------------------------------
# make_embedder factory tests (no DB required)
# ---------------------------------------------------------------------------

def test_make_embedder_none_provider_returns_none():
    s = Settings(embed_provider="none")
    assert make_embedder(s) is None


def test_make_embedder_empty_provider_returns_none():
    s = Settings(embed_provider="")
    assert make_embedder(s) is None


def test_make_embedder_stub_returns_stub():
    s = Settings(embed_provider="stub")
    emb = make_embedder(s)
    assert emb is not None
    vec = emb.embed("test")
    assert len(vec) == 256


def test_make_embedder_unknown_raises():
    s = Settings(embed_provider="bogus_provider")
    with pytest.raises(ValueError, match="Unknown embed_provider"):
        make_embedder(s)


# ---------------------------------------------------------------------------
# DB-backed tests
# ---------------------------------------------------------------------------

def test_memory_write_and_ilike_search(pool):
    """ILIKE fallback path — always exercised regardless of pgvector."""
    repo.memory_write(pool, "Postgres is the canonical state store", scope="global")
    repo.memory_write(pool, "agents coordinate via issues", scope="global")

    hits = repo.memory_search(pool, "canonical")
    assert len(hits) == 1
    assert "canonical" in hits[0].body


def test_ilike_search_no_match(pool):
    repo.memory_write(pool, "completely unrelated content", scope="global")
    hits = repo.memory_search(pool, "xyzzy_no_match_ever")
    assert hits == []


def test_memory_write_with_embedding_no_error(pool):
    """memory_write with an embedding should never raise regardless of pgvector."""
    emb = StubEmbedder()
    vec = emb.embed("embedding write test")
    # Should not raise.
    note = repo.memory_write(pool, "embedding write test", scope="global", embedding=vec)
    assert note.id > 0
    assert note.body == "embedding write test"


def test_memory_search_with_embedding_no_error(pool):
    """memory_search with a query_embedding should never raise."""
    emb = StubEmbedder()
    repo.memory_write(pool, "semantic store note", scope="global")
    vec = emb.embed("semantic store note")
    results = repo.memory_search(pool, "semantic", limit=10, query_embedding=vec)
    # At minimum we should get the ILIKE hit.
    assert any("semantic" in n.body for n in results)


@pytest.mark.skipif(
    not repo._pgvector_available.__doc__  # always runs; skip condition is below
    or True,  # evaluated at collection time — replaced by the real check below
    reason="placeholder — real skip applied dynamically"
)
def _placeholder():
    pass  # not used; real tests below use a function-level skip


def test_pgvector_available_is_bool(pool):
    """_pgvector_available must return a boolean (True or False, never raises)."""
    repo.reset_pgvector_cache()
    result = repo._pgvector_available(pool)
    assert isinstance(result, bool)


def test_vector_ordering_when_pgvector_present(pool):
    """When pgvector IS available, vector search should rank the most-similar
    note first.  Skipped gracefully when pgvector is absent."""
    repo.reset_pgvector_cache()
    if not repo._pgvector_available(pool):
        pytest.skip("pgvector not available on this Postgres — skipping vector-ordering test")

    emb = StubEmbedder()
    close_text = "semantic memory pgvector embedding search"
    far_text = "banana pancake flour butter recipe"

    close_vec = emb.embed(close_text)
    far_vec = emb.embed(far_text)

    repo.memory_write(pool, close_text, scope="global", embedding=close_vec)
    repo.memory_write(pool, far_text, scope="global", embedding=far_vec)

    query_vec = emb.embed("semantic memory search")
    results = repo.memory_search(pool, "semantic", limit=10, query_embedding=query_vec)

    assert len(results) >= 1
    # The closest note should come first.
    assert results[0].body == close_text


def test_ilike_fallback_when_no_embeddings_stored(pool):
    """Even when pgvector is present, rows with no embedding_v fall back to ILIKE."""
    # Write without embedding so embedding_v is NULL.
    repo.memory_write(pool, "fallback text canonical", scope="global")

    emb = StubEmbedder()
    query_vec = emb.embed("canonical")

    results = repo.memory_search(pool, "canonical", limit=10, query_embedding=query_vec)
    assert any("canonical" in n.body for n in results)
