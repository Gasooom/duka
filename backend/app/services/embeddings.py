"""Embedding providers behind one interface.

- HashingEmbedder: zero-cost, offline, deterministic feature hashing (word unigrams + char
  trigrams). It is lexical, not semantic, but makes pgvector retrieval work with no API key.
- OpenAICompatEmbedder: any OpenAI-compatible /embeddings endpoint (OpenAI, Gemini's
  OpenAI-compatible endpoint, etc.), requesting `dimensions=384` so it matches the schema.

Services embed through embed_texts and query_vector, which record every request of an embedder that costs money
(`metered`) in the usage ledger, for the tenant it served: one event per request, whatever its outcome
(docs/P2_EMBEDDING_METERING.md).
"""
import hashlib
import math
import re
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import lru_cache

import httpx
from sqlalchemy.engine import Connection, Engine

from app.core.config import settings
from app.core.deadline import remaining as turn_time_left
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger, log_event
from app.services import usage_service

logger = get_logger(__name__)
_TOKEN = re.compile(r"[a-z0-9]+")
_metering = True  # switched off by evaluation runs only (set_metering)


@dataclass
class EmbedResult:
    """One embeddings request: its vectors, or the error that ended it, and what it used."""
    vectors: list[list[float]] | None
    attempts: int = 0  # HTTP attempts sent for it (0: nothing was sent)
    input_tokens: int | None = None  # as the provider reports them; None = not reported
    model: str | None = None  # served, as the provider reports it
    error: Exception | None = None


class Embedder(ABC):
    name: str
    dim: int
    model: str | None = None
    metered = False  # True: every request costs money and is recorded in the usage ledger (embed_texts)

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
    metered = True
    MAX_ATTEMPTS = 3
    TIMEOUT_SECONDS = 20.0  # per request; inside an AI turn also capped by the time left in it
    MIN_ATTEMPT_SECONDS = 1.0

    def __init__(self, base_url: str, api_key: str, model: str, dim: int):
        if not api_key:
            raise RuntimeError("EMBEDDING_API_KEY is required for EMBEDDING_PROVIDER=openai_compat "
                               "(BLOCKED BY EXTERNAL CREDENTIAL)")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        result = self.embed_counted(texts)
        if result.error is not None:
            raise result.error
        return result.vectors

    def embed_counted(self, texts: list[str]) -> EmbedResult:
        """One embeddings request: up to MAX_ATTEMPTS HTTP attempts, retrying 429/5xx/network errors with exponential
        backoff. Inside an AI turn (a search tool) every attempt and pause fits in what is left of the turn budget;
        when too little is left the search fails like an outage instead of stretching the turn. Never raises: a
        failure is returned with the attempts it made (embed() raises it), so that it can be metered too."""
        last_exc: Exception | None = None
        attempts = 0
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            left = turn_time_left()
            if left is not None and left < self.MIN_ATTEMPT_SECONDS:
                last_exc = last_exc or "no time left in the turn budget"
                break
            attempts = attempt
            try:
                r = httpx.post(
                    f"{self.base_url}/embeddings",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"model": self.model, "input": texts, "dimensions": self.dim},
                    timeout=self.TIMEOUT_SECONDS if left is None else min(self.TIMEOUT_SECONDS, left),
                )
                if r.status_code not in (429, 500, 502, 503, 504):
                    r.raise_for_status()
                    body = r.json()
                    data = sorted(body["data"], key=lambda d: d["index"])
                    usage = body.get("usage")
                    usage = usage if isinstance(usage, dict) else {}
                    tokens = usage.get("prompt_tokens")
                    return EmbedResult([d["embedding"] for d in data], attempts=attempts,
                                       input_tokens=tokens if tokens is not None else usage.get("total_tokens"),
                                       model=body.get("model"))
                last_exc = ExternalServiceError(f"embeddings HTTP {r.status_code}")
            except httpx.TransportError as exc:
                last_exc = exc
            except Exception as exc:  # refused (4xx) or an unreadable answer: not retried, raised by embed() as before
                return EmbedResult(None, attempts=attempts, error=exc)
            if attempt < self.MAX_ATTEMPTS:
                pause = 0.5 * 2 ** (attempt - 1)
                left = turn_time_left()
                if left is not None and left - pause < self.MIN_ATTEMPT_SECONDS:
                    break
                time.sleep(pause)
        return EmbedResult(None, attempts=attempts,
                           error=ExternalServiceError(f"Embedding request failed: {last_exc}"))


def set_metering(enabled: bool) -> None:
    """Evaluation runs switch metering off while they run (evals/harness.py): like their model calls and WhatsApp
    sends, their embeddings requests are platform activity, never a tenant's usage. The requests are still made."""
    global _metering
    _metering = enabled


def embed_texts(bind: Engine | Connection, business_id: uuid.UUID, texts: list[str], *, source_type: str,
                source_id: uuid.UUID | None = None) -> list[list[float]]:
    """The embeddings of `texts`, in one request. A request of a metered embedder is recorded in the usage ledger
    for `business_id` (the calling service's tenant, never from input) whether it succeeded or not, in its own
    transaction, so a rollback of the caller never removes it; a recording failure is logged and never breaks the
    caller. Raises what Embedder.embed raises."""
    embedder = get_embedder()
    if not (embedder.metered and _metering):
        return embedder.embed(texts)
    key = f"emb:{uuid.uuid4()}"  # made before the request: one request, one event
    result = embedder.embed_counted(texts)
    if result.attempts:  # nothing was sent (no time left in the turn): nothing was used
        usage_service.record_embedding(
            bind, business_id, idempotency_key=key, source_type=source_type, source_id=source_id,
            provider=embedder.name, model=result.model, configured_model=embedder.model,
            status="success" if result.error is None else "error", units=len(texts),
            input_tokens=result.input_tokens, attempts=result.attempts)
    if result.error is not None:
        raise result.error
    return result.vectors


def query_vector(query: str, *, bind: Engine | Connection, business_id: uuid.UUID,
                 source_type: str) -> list[float] | None:
    """The embedding of a search query, or None when the embeddings service is unavailable (outage, or no time left
    in the AI turn). Searches then rank by words alone: vector similarity only ranks, never admits (CLAUDE.md rule
    11), so the results stay correct and the customer never sees a technical error. Metered like embed_texts."""
    try:
        return embed_texts(bind, business_id, [query], source_type=source_type)[0]
    except (ExternalServiceError, httpx.HTTPError) as exc:  # retries exhausted, no time left, or a 4xx (bad key)
        log_event(logger, "embeddings.unavailable", 30, operation="embeddings", status="degraded", error=str(exc)[:300])
        return None


@lru_cache
def get_embedder() -> Embedder:
    if settings.embedding_provider == "openai_compat":
        return OpenAICompatEmbedder(settings.embedding_base_url, settings.embedding_api_key,
                                    settings.embedding_model, settings.embedding_dim)
    return HashingEmbedder(settings.embedding_dim)
