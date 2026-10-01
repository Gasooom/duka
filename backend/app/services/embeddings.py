"""Embedding providers behind one interface.

- HashingEmbedder: zero-cost, offline, deterministic feature hashing (word unigrams + char
  trigrams). It is lexical, not semantic, but makes pgvector retrieval work with no API key.
- OpenAICompatEmbedder: any OpenAI-compatible /embeddings endpoint (OpenAI, Gemini's
  OpenAI-compatible endpoint, etc.), requesting `dimensions=384` so it matches the schema.
"""
import hashlib
import math
import re
from abc import ABC, abstractmethod
from functools import lru_cache

import httpx

from app.core.config import settings
from app.core.errors import ExternalServiceError

_TOKEN = re.compile(r"[a-z0-9]+")


class Embedder(ABC):
    name: str
    dim: int

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


def _stem(tok: str) -> str:
    for suf in ("ies", "es", "s"):
        if len(tok) > 4 and tok.endswith(suf):
            return tok[: -len(suf)] + ("y" if suf == "ies" else "")
    return tok


class HashingEmbedder(Embedder):
    name = "hash"

    def __init__(self, dim: int = 384):
        self.dim = dim

    def _features(self, text: str) -> list[tuple[str, float]]:
        toks = [_stem(t) for t in _TOKEN.findall(text.lower())]
        feats: list[tuple[str, float]] = [("w:" + t, 1.0) for t in toks]
        for t in toks:
            padded = f"#{t}#"
            feats.extend(("c:" + padded[i:i + 3], 0.3) for i in range(len(padded) - 2))
        return feats

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            for feat, weight in self._features(text or ""):
                h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "big")
                vec[h % self.dim] += weight if (h >> 63) & 1 else -weight
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


class OpenAICompatEmbedder(Embedder):
    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str, model: str, dim: int):
        if not api_key:
            raise RuntimeError("EMBEDDING_API_KEY is required for EMBEDDING_PROVIDER=openai_compat "
                               "(BLOCKED BY EXTERNAL CREDENTIAL)")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        last_exc: Exception | None = None
        for _ in range(3):
            try:
                r = httpx.post(
                    f"{self.base_url}/embeddings",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"model": self.model, "input": texts, "dimensions": self.dim},
                    timeout=20,
                )
                if r.status_code in (429, 500, 502, 503, 504):
                    last_exc = ExternalServiceError(f"embeddings HTTP {r.status_code}")
                    continue
                r.raise_for_status()
                data = sorted(r.json()["data"], key=lambda d: d["index"])
                return [d["embedding"] for d in data]
            except httpx.TransportError as exc:
                last_exc = exc
        raise ExternalServiceError(f"Embedding request failed: {last_exc}")


@lru_cache
def get_embedder() -> Embedder:
    if settings.embedding_provider == "openai_compat":
        return OpenAICompatEmbedder(settings.embedding_base_url, settings.embedding_api_key,
                                    settings.embedding_model, settings.embedding_dim)
    return HashingEmbedder(settings.embedding_dim)
