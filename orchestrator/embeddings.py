"""Embeddings — lightweight protocol + provider factory.

Providers
---------
stub  (default)
    Deterministic, network-free bag-of-words embedder.  Each token in the text
    is hashed with a seeded xxhash-style int, mapped to one of 256 dimensions,
    and accumulated.  The resulting histogram is L2-normalised to a unit vector.
    Same text always produces the same 256-float vector; texts sharing tokens
    share dimensions, so cosine similarity is meaningful.

openai
    Uses the OpenAI SDK ``client.embeddings.create()``.  Lazily imported so the
    SDK is only loaded when the provider is actually requested.  Requires
    ``embed_api_key`` (and optionally ``embed_base_url`` / ``embed_model``) in
    Settings.  The ``dimensions`` parameter is passed so the model returns
    exactly 256 floats (only supported by text-embedding-3-* family).

none / ""
    Returns ``None`` from ``make_embedder`` — callers must treat a None embedder
    as "no embedding available" and degrade gracefully.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Optional, Protocol

if TYPE_CHECKING:
    from .config import Settings


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

class Embedder(Protocol):
    def embed(self, text: str) -> list[float]:
        ...


# ---------------------------------------------------------------------------
# Stub embedder — deterministic, network-free
# ---------------------------------------------------------------------------

_DIMS = 256


def _hash_token(token: str) -> int:
    """Deterministic integer hash for a single token (FNV-1a, 32-bit)."""
    h = 2166136261  # FNV offset basis
    for ch in token.encode("utf-8"):
        h ^= ch
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def _tokenize(text: str) -> list[str]:
    """Very simple whitespace + punctuation tokeniser."""
    import re
    return re.findall(r"[a-zA-Z0-9]+", text.lower())


class StubEmbedder:
    """Bag-of-words embedder using FNV-1a token hashing into 256 dimensions."""

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * _DIMS
        tokens = _tokenize(text)
        if not tokens:
            # All-zero → normalise to unit vector along dim 0 to avoid ZeroDivision.
            vec[0] = 1.0
            return vec
        for token in tokens:
            h = _hash_token(token)
            dim = h % _DIMS
            # Use a secondary hash for the weight so repeated tokens accumulate.
            weight = 1.0 + ((_hash_token(token + "_w") & 0xFF) / 255.0)
            vec[dim] += weight
        # L2-normalise.
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            vec[0] = 1.0
            return vec
        return [v / norm for v in vec]


# ---------------------------------------------------------------------------
# OpenAI embedder — lazy import, 256-dim
# ---------------------------------------------------------------------------

class OpenAIEmbedder:
    """Thin wrapper around the OpenAI embeddings endpoint."""

    def __init__(self, api_key: str, base_url: str = "", model: str = "") -> None:
        try:
            import openai  # noqa: PLC0415  # lazy import
        except ImportError as exc:
            raise ImportError(
                "openai package is required for embed_provider='openai'; "
                "install it or switch to embed_provider='stub'."
            ) from exc
        kwargs: dict = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = openai.OpenAI(**kwargs)
        self._model = model or "text-embedding-3-small"

    def embed(self, text: str) -> list[float]:
        resp = self._client.embeddings.create(
            model=self._model,
            input=text,
            dimensions=_DIMS,
        )
        return resp.data[0].embedding


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_embedder(settings: "Settings") -> Optional[Embedder]:
    """Return an Embedder instance for the configured provider, or None.

    Provider resolution:
        "stub"        → StubEmbedder (default)
        "openai"      → OpenAIEmbedder
        "none" / ""   → None
    """
    provider = (settings.embed_provider or "").lower().strip()
    if provider in ("none", ""):
        return None
    if provider == "stub":
        return StubEmbedder()
    if provider == "openai":
        return OpenAIEmbedder(
            api_key=settings.embed_api_key,
            base_url=settings.embed_base_url,
            model=settings.embed_model,
        )
    raise ValueError(
        f"Unknown embed_provider {provider!r}; expected 'stub', 'openai', or 'none'."
    )
