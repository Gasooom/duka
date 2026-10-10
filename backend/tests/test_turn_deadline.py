"""The AI turn budget (AGENT_TURN_TIMEOUT_SECONDS) covers the whole turn: model calls, tools, and work a tool starts
(an embeddings request during a search). A slow tool cannot stretch the turn by more than itself, the facts the tools
already returned are sent when the time runs out, and embedding requests take what is left of the turn as their
timeout, with backoff between retries. Design: docs/P1_RUNAWAY_GUARD.md (B1, findings F4 and F5)."""
import time
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.agents.providers import LLMProvider, LLMResponse, ToolCall, set_provider_override
from app.core import deadline
from app.core.config import settings
from app.core.errors import ExternalServiceError
from app.models import AgentRun
from app.services import embeddings, product_service

NUMBER = "250788111222"


def call(name: str, **args) -> ToolCall:
    return ToolCall(id=f"c-{uuid.uuid4().hex[:6]}", name=name, arguments=args)


class Scripted(LLMProvider):
    """Returns the scripted responses in order (the last one repeats) and records each call's timeout."""
    name = "scripted"

    def __init__(self, *responses: LLMResponse, delay: float = 0.0):
        self.responses, self.delay = list(responses), delay
        self.timeouts: list[float | None] = []

    def complete(self, messages, tools, *, model=None, temperature=0.2, timeout=None):
        self.timeouts.append(timeout)
        time.sleep(self.delay)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def last_run(db) -> AgentRun:
    db.expire_all()
    return db.scalars(select(AgentRun).order_by(AgentRun.created_at.desc())).first()


def reply(outbox) -> str:
    return [b for to, b in outbox.sent if to == NUMBER][-1]


@pytest.fixture
def slow_search(monkeypatch):
    """search_products takes `seconds` (as a slow embeddings request would) and counts how often it ran."""
    runs = []
    real = product_service.ProductService.search

    def install(seconds: float):
        def slow(self, *a, **k):
            runs.append(1)
            time.sleep(seconds)
            return real(self, *a, **k)
        monkeypatch.setattr(product_service.ProductService, "search", slow)
        return runs
    return install


def test_a_slow_tool_cannot_stretch_the_turn_and_its_facts_are_sent(fashion, outbox, db, monkeypatch, slow_search):
    monkeypatch.setattr(settings, "agent_turn_timeout_seconds", 2.0)
    runs = slow_search(1.2)
    # One model answer asking for three searches: each takes 1.2 s, the budget is 2 s.
    set_provider_override(Scripted(LLMResponse(content=None, tool_calls=[
        call("search_products", query="black sneakers") for _ in range(3)])))
    start = time.monotonic()
    fashion.send("black sneakers please")
    took = time.monotonic() - start
    run = last_run(db)
    assert len(runs) == 2  # the third search was never started: the time was up
    assert took < 2.0 + 1.2 + 1.5  # at most one tool past the budget (plus test overhead)
    assert run.status == "error" and "budget" in run.error and run.llm_calls == 1
    assert [s["skipped_tool_calls"] for s in run.steps if s["type"] == "deadline"] == [1]
    assert reply(outbox).startswith("Here's what I found")  # the facts the searches returned, not an apology


def test_a_turn_out_of_time_with_nothing_to_say_still_apologises(fashion, outbox, db, monkeypatch):
    monkeypatch.setattr(settings, "agent_turn_timeout_seconds", 1.5)
    set_provider_override(Scripted(LLMResponse(content=None, tool_calls=[call("no_such_tool")]), delay=0.8))
    fashion.send("cart?")
    run = last_run(db)
    assert run.status == "error" and "budget" in run.error
    assert reply(outbox).startswith("Sorry")  # no tool returned a fact: the fallback, never an invented answer


def test_every_model_call_gets_only_the_time_left(fashion, outbox, monkeypatch):
    monkeypatch.setattr(settings, "agent_turn_timeout_seconds", 5.0)
    model = Scripted(LLMResponse(content=None, tool_calls=[call("get_cart")]),
                     LLMResponse(content="Your cart is empty."), delay=0.3)
    set_provider_override(model)
    fashion.send("cart?")
    first, second = model.timeouts
    assert first <= 5.0 and second < first  # the second call gets what the first left over


def test_the_summary_call_never_takes_more_than_the_turn_has_left(fashion, outbox, monkeypatch):
    for text_ in ("black sneakers", "white sneakers", "do you have jackets?", "show me hoodies", "red dresses"):
        fashion.send(text_)  # the offline engine builds a history
    monkeypatch.setattr(settings, "agent_summary_trigger_messages", 4)
    monkeypatch.setattr(settings, "agent_turn_timeout_seconds", 4.0)
    model = Scripted(LLMResponse(content="Customer looks at sneakers."), LLMResponse(content="Which size?"))
    set_provider_override(model)
    fashion.send("anything in size 42?")
    summary_timeout = model.timeouts[0]
    assert summary_timeout is not None and summary_timeout <= 4.0  # not the summary's own 10 s


def test_the_deadline_is_only_set_inside_a_turn(fashion, outbox):
    assert deadline.remaining() is None
    with deadline.turn_deadline(time.monotonic() + 3):
        assert 2.5 < deadline.remaining() <= 3
    assert deadline.remaining() is None


# ---------------------------------------------------------------- embeddings
class FakePost:
    """Stands in for httpx.post: answers with the given status codes in order and records each timeout."""

    def __init__(self, *statuses: int):
        self.statuses, self.timeouts = list(statuses), []

    def __call__(self, url, *, headers, json, timeout):
        self.timeouts.append(timeout)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        body = {"data": [{"index": 0, "embedding": [0.0] * 384}]}
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))


@pytest.fixture
def embedder(monkeypatch):
    sleeps = []
    monkeypatch.setattr(embeddings.time, "sleep", lambda s: sleeps.append(s))
    e = embeddings.OpenAICompatEmbedder("https://emb.test/v1", "key", "m", 384)
    return e, sleeps


def test_embeddings_outside_a_turn_keep_their_timeout_and_back_off(embedder, monkeypatch):
    e, sleeps = embedder
    post = FakePost(503, 503, 200)
    monkeypatch.setattr(embeddings.httpx, "post", post)
    assert len(e.embed(["phone"])[0]) == 384
    assert post.timeouts == [20.0, 20.0, 20.0] and sleeps == [0.5, 1.0]  # exponential backoff, not a tight loop


def test_embeddings_inside_a_turn_take_only_the_time_left(embedder, monkeypatch):
    e, _ = embedder
    post = FakePost(200)
    monkeypatch.setattr(embeddings.httpx, "post", post)
    with deadline.turn_deadline(time.monotonic() + 3):
        e.embed(["phone"])
    assert 0 < post.timeouts[0] <= 3


def test_embeddings_stop_retrying_when_the_turn_runs_out(embedder, monkeypatch):
    e, sleeps = embedder
    post = FakePost(503)
    monkeypatch.setattr(embeddings.httpx, "post", post)
    with deadline.turn_deadline(time.monotonic() + 1.2), pytest.raises(ExternalServiceError):
        e.embed(["phone"])
    assert len(post.timeouts) == 1 and sleeps == []  # no retry that would end after the turn
    with deadline.turn_deadline(time.monotonic() + 0.5), pytest.raises(ExternalServiceError, match="no time left"):
        e.embed(["phone"])
    assert len(post.timeouts) == 1  # nothing sent at all with too little time left


# ---------------------------------------------------------------- searches when embeddings are unavailable
class DownEmbedder(embeddings.Embedder):
    name, dim = "down", 384

    def embed(self, texts):
        raise ExternalServiceError("Embedding request failed: no time left in the turn budget")


def test_searches_rank_by_words_when_embeddings_are_unavailable(fashion, db, monkeypatch, caplog):
    from app.services.knowledge_service import KnowledgeService
    from app.services.product_service import ProductService
    bid = uuid.UUID(fashion.business_id)
    KnowledgeService(db, bid).add_document("Returns", "Returns are accepted within 7 days with the receipt.")
    db.commit()
    with_vectors = {h.product.id for h in ProductService(db, bid).search("black sneakers")}
    monkeypatch.setattr(embeddings, "get_embedder", lambda: DownEmbedder())
    with caplog.at_level("WARNING", logger="app"):
        hits = ProductService(db, bid).search("black sneakers")
        policy = KnowledgeService(db, bid).search("returns receipt")
        unrelated = ProductService(db, bid).search("phone")
    # Words admit, vectors only rank: without vectors the same products are found, none added, none lost.
    assert hits and {h.product.id for h in hits} == with_vectors
    assert policy and policy[0].document_title == "Returns"
    assert unrelated == []  # never a filler result
    assert any(r.getMessage() == "embeddings.unavailable" for r in caplog.records)


def test_a_customer_never_sees_an_embeddings_error(fashion, outbox, monkeypatch):
    monkeypatch.setattr(embeddings, "get_embedder", lambda: DownEmbedder())
    set_provider_override(Scripted(LLMResponse(content=None, tool_calls=[call("search_products", query="black sneakers")]),
                                   LLMResponse(content=None, tool_calls=[call("search_products", query="black sneakers")])))
    fashion.send("black sneakers please")
    text_ = reply(outbox)
    assert "Embedding" not in text_ and "HTTP" not in text_ and text_.startswith("Here's what I found")


def test_a_rejected_embeddings_request_also_falls_back_to_words(fashion, db, monkeypatch):
    from app.services.product_service import ProductService
    bid = uuid.UUID(fashion.business_id)
    with_vectors = {h.product.id for h in ProductService(db, bid).search("black sneakers")}
    e = embeddings.OpenAICompatEmbedder("https://emb.test/v1", "bad-key", "m", 384)
    monkeypatch.setattr(embeddings, "get_embedder", lambda: e)
    monkeypatch.setattr(embeddings.httpx, "post", FakePost(401))  # not retryable: raise_for_status
    assert {h.product.id for h in ProductService(db, bid).search("black sneakers")} == with_vectors
