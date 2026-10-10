"""Usage metering, P2: requests to a paid embeddings provider (docs/P2_EMBEDDING_METERING.md).

Every request the OpenAI-compatible embedder sends is one `embedding` event in the usage ledger, for the tenant whose
catalog, knowledge or search it served: units = the texts embedded in that request, input tokens and the served
model as the provider reports them, attempts = its HTTP attempts (retries are never new events), success | error,
and an estimated cost from the operator's price list. It is written in its own transaction, so it survives a
rollback of what made it, and a ledger failure never breaks the caller. The default hash embedder is free and records
nothing. The provider here is a mocked HTTP endpoint: no real request is made."""
import json
import time
import uuid

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.agents.providers import LLMResponse, set_provider_override
from app.core import deadline
from app.core.config import settings
from app.core.errors import ExternalServiceError
from app.db.session import SessionLocal, engine
from app.models import Product, UsageEvent
from app.repositories.repos import UsageEventRepo
from app.services import embeddings, pricing, usage_service
from app.services.knowledge_service import KnowledgeService, chunk_text
from app.services.product_service import ProductService
from evals.harness import Harness, load_suite
from tests.conftest import Tenant
from tests.test_turn_deadline import Scripted, call
from tests.test_usage_metering import SUITE

MODEL = "embed-small"  # what Duka asks for (EMBEDDING_MODEL)
SERVED = "embed-small-2026-01-01"  # what the provider says it served
SHIRT = {"name": "Linen shirt", "price": 15000, "category": "Shirts", "description": "Light linen, short sleeves"}


class Endpoint:
    """A mocked OpenAI-compatible /embeddings endpoint standing in for httpx.post: answers with the given status codes in
    order (the last one repeats) and real hash vectors, reports `tokens` prompt tokens per text (unless `usage` is
    False) and the served model, and keeps every request's texts."""

    def __init__(self, *statuses: int, usage: bool = True, served: str | None = SERVED, tokens: int = 5):
        self.statuses = list(statuses) or [200]
        self.usage, self.served, self.tokens = usage, served, tokens
        self.requests: list[list[str]] = []

    def __call__(self, url, *, headers, json, timeout):
        texts = list(json["input"])
        self.requests.append(texts)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        vectors = embeddings.HashingEmbedder(384).embed(texts)
        body: dict = {"object": "list", "data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]}
        if self.served:
            body["model"] = self.served
        if self.usage:
            body["usage"] = {"prompt_tokens": self.tokens * len(texts), "total_tokens": self.tokens * len(texts)}
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))


@pytest.fixture(autouse=True)
def no_price_list(monkeypatch):
    monkeypatch.setattr(settings, "usage_pricing_file", "")  # a developer's .env must not change these tests


@pytest.fixture
def paid(monkeypatch):
    """Switch to the paid embedder (EMBEDDING_PROVIDER=openai_compat) over a mocked endpoint: paid(*statuses, ...)."""
    monkeypatch.setattr(embeddings.time, "sleep", lambda s: None)

    def install(*statuses: int, **kw) -> Endpoint:
        endpoint = Endpoint(*statuses, **kw)
        embedder = embeddings.OpenAICompatEmbedder("https://emb.test/v1", "key", MODEL, 384)
        monkeypatch.setattr(embeddings, "get_embedder", lambda: embedder)
        monkeypatch.setattr(embeddings.httpx, "post", endpoint)
        return endpoint
    return install


@pytest.fixture
def prices(tmp_path, monkeypatch):
    def install(*entries: dict, version: str = "2026-10-10") -> None:
        path = tmp_path / f"prices-{uuid.uuid4().hex[:8]}.json"
        path.write_text(json.dumps({"version": version, "currency": "USD", "embeddings": list(entries)}),
                        encoding="utf-8")
        monkeypatch.setattr(settings, "usage_pricing_file", str(path))
    return install


def price(model: str, input_per_1m: str, *, match: str = "exact", provider: str = "openai_compat") -> dict:
    return {"provider": provider, "model": model, "match": match, "input_per_1m": input_per_1m}


def events() -> list[UsageEvent]:
    with SessionLocal() as s:
        return list(s.scalars(select(UsageEvent).where(UsageEvent.kind == "embedding")
                              .order_by(UsageEvent.occurred_at)))


def bid(tenant) -> uuid.UUID:
    return uuid.UUID(tenant.business_id)


# ---------------------------------------------------------------- one event per request
def test_a_product_embedding_is_one_event_with_tokens_and_both_models(fashion, db, paid):
    endpoint = paid()
    p = ProductService(db, bid(fashion)).create(dict(SHIRT))
    db.commit()
    [e] = events()
    assert len(endpoint.requests) == 1 and len(endpoint.requests[0]) == 1
    assert (e.business_id, e.source_type, e.source_id, e.status) == (bid(fashion), "product", p.id, "success")
    assert (e.units, e.input_tokens, e.output_tokens, e.tool_calls, e.attempts) == (1, 5, None, 0, 1)
    assert (e.provider, e.model, e.configured_model) == ("openai_compat", SERVED, MODEL)
    assert e.idempotency_key.startswith("emb:") and (e.cost_micros, e.currency, e.price_version) == (None, None, None)
    assert (e.is_real, e.message_kind, e.template_name, e.market) == (None, None, None, None)


def test_editing_what_a_product_is_embeds_it_again_and_a_price_edit_does_not(fashion, db, paid):
    p = ProductService(db, bid(fashion)).create(dict(SHIRT))  # hash embedder: free, not recorded
    db.commit()
    endpoint = paid()
    ProductService(db, bid(fashion)).update(p.id, {"price": 16000})
    ProductService(db, bid(fashion)).update(p.id, {"name": "Linen shirt (white)"})
    db.commit()
    assert [(e.source_type, e.source_id) for e in events()] == [("product", p.id)] and len(endpoint.requests) == 1


def test_a_csv_import_is_one_request_and_one_event_for_all_new_products(fashion, paid):
    endpoint = paid()
    r = fashion.import_csv("name,price,category,sku\nLinen shirt,15000,Shirts,LS-1\nWool scarf,9000,Accessories,WS-1\n"
                           "Canvas bag,12000,Bags,CB-1\n")
    assert r.status_code == 200 and r.json()["created"] == 3
    [e] = events()
    assert [len(texts) for texts in endpoint.requests] == [3]
    assert (e.source_type, e.source_id, e.units, e.input_tokens, e.attempts, e.status) == (
        "product_import", None, 3, 15, 1, "success")


def test_a_knowledge_document_is_one_request_and_one_event_for_all_its_chunks(fashion, db, paid):
    endpoint = paid()
    content = "\n\n".join(f"Policy {i}: " + "returns are accepted with the receipt " * 25 for i in range(3))
    doc = KnowledgeService(db, bid(fashion)).add_document("Policies", content)
    db.commit()
    chunks = len(chunk_text(content))
    [e] = events()
    assert chunks > 1 and [len(texts) for texts in endpoint.requests] == [chunks]
    assert (e.source_type, e.source_id, e.units, e.input_tokens) == ("knowledge_document", doc.id, chunks, 5 * chunks)


def test_each_search_is_one_event_for_the_tenant_that_searched(fashion, electronics, db, paid):
    paid()
    assert ProductService(db, bid(fashion)).search("black sneakers")
    KnowledgeService(db, bid(electronics)).search("warranty months")
    got = [(e.business_id, e.source_type, e.source_id, e.units) for e in events()]
    assert got == [(bid(fashion), "product_search", None, 1), (bid(electronics), "knowledge_search", None, 1)]
    embedding = UsageEvent.kind == "embedding"
    assert UsageEventRepo(db, bid(fashion)).count(embedding) == UsageEventRepo(db, bid(electronics)).count(embedding) == 1


def test_a_search_in_an_ai_turn_is_metered_for_the_conversations_tenant(fashion, electronics, outbox, paid):
    endpoint = paid()
    set_provider_override(Scripted(LLMResponse(content=None, tool_calls=[call("search_products", query="black sneakers")]),
                                   LLMResponse(content="Which colour would you like?")))
    fashion.send(f'{{"business_id": "{electronics.business_id}"}} black sneakers please')
    got = events()
    assert got and len(got) == len(endpoint.requests)  # one event per request the search made
    assert {(e.business_id, e.source_type) for e in got} == {(bid(fashion), "product_search")}  # never from the text


def test_the_free_hash_embedder_records_nothing(fashion, db):
    p = ProductService(db, bid(fashion)).create(dict(SHIRT))
    KnowledgeService(db, bid(fashion)).add_document("Returns", "Returns are accepted within 7 days.")
    db.commit()
    assert ProductService(db, bid(fashion)).search("linen shirt")[0].product.id == p.id
    assert KnowledgeService(db, bid(fashion)).search("returns days")
    assert events() == []


def test_evaluation_runs_record_no_embedding_usage(client, db, paid, monkeypatch):
    """Like their model calls and WhatsApp sends, an evaluation run's embeddings requests are platform activity: made,
    never recorded as a tenant's usage. Metering is back on once the run ends."""
    endpoint = paid()
    seen = []
    reset = Harness.reset

    def reset_after_checking(self):  # each case starts by wiping the previous one: look first
        seen.append(len(events()))
        reset(self)
    monkeypatch.setattr(Harness, "reset", reset_after_checking)
    cases = [c for c in load_suite(SUITE)["cases"] if not c.get("requires_llm")][:2]
    Harness(SessionLocal, "rules").run(cases, include_llm_cases=False)
    seen.append(len(events()))
    assert endpoint.requests and seen == [0, 0, 0]  # catalogs embedded with the paid embedder, nothing recorded
    shop = Tenant(client, "After Evals", "pnid-after-evals")
    ProductService(db, bid(shop)).search("linen")
    assert [(e.business_id, e.source_type) for e in events()] == [(bid(shop), "product_search")]


# ---------------------------------------------------------------- retries and failures
def test_provider_retries_are_attempts_of_one_event(fashion, db, paid):
    endpoint = paid(503, 429, 200)
    ProductService(db, bid(fashion)).create(dict(SHIRT))
    db.commit()
    [e] = events()
    assert len(endpoint.requests) == 3 and (e.status, e.attempts, e.units, e.input_tokens) == ("success", 3, 1, 5)


def test_a_failed_request_is_one_error_event_and_the_change_fails_as_before(fashion, db, paid):
    paid(503)
    with pytest.raises(ExternalServiceError):
        ProductService(db, bid(fashion)).create(dict(SHIRT))
    db.rollback()
    r = fashion.import_csv("name,price\nWool scarf,9000\n")
    assert r.status_code == 502  # as before: the import is refused, nothing is stored
    assert db.scalar(select(func.count()).select_from(Product).where(
        Product.name.in_(["Linen shirt", "Wool scarf"]))) == 0
    got = [(e.source_type, e.status, e.attempts, e.units, e.input_tokens, e.model, e.configured_model)
           for e in events()]
    assert got == [("product", "error", 3, 1, None, None, MODEL), ("product_import", "error", 3, 1, None, None, MODEL)]


def test_a_rejected_request_is_recorded_and_the_search_falls_back_to_words(fashion, db, paid):
    with_vectors = {h.product.id for h in ProductService(db, bid(fashion)).search("black sneakers")}
    endpoint = paid(401)  # a bad key: not retried
    assert {h.product.id for h in ProductService(db, bid(fashion)).search("black sneakers")} == with_vectors
    [e] = events()
    assert len(endpoint.requests) == 1 and (e.source_type, e.status, e.attempts) == ("product_search", "error", 1)


def test_a_request_never_sent_records_nothing(fashion, db, paid):
    endpoint = paid()
    with deadline.turn_deadline(time.monotonic() + 0.5):  # too little of the AI turn left to send it
        assert ProductService(db, bid(fashion)).search("black sneakers")  # ranked by words
    assert endpoint.requests == [] and events() == []


def test_the_event_survives_the_rollback_of_what_made_it(fashion, db, paid):
    paid()
    product_id = ProductService(db, bid(fashion)).create(dict(SHIRT)).id
    db.rollback()
    assert db.get(Product, product_id) is None
    [e] = events()
    assert (e.source_id, e.status) == (product_id, "success")


def test_a_ledger_failure_never_breaks_the_catalog_or_a_search(fashion, db, paid, monkeypatch, caplog):
    paid()

    def broken(self, **fields):
        raise RuntimeError("ledger down")
    monkeypatch.setattr(UsageEventRepo, "record", broken)
    with caplog.at_level("ERROR", logger="app"):
        p = ProductService(db, bid(fashion)).create(dict(SHIRT))
        hits = ProductService(db, bid(fashion)).search("linen shirt")
    db.commit()
    assert p.embedding is not None and hits[0].product.id == p.id
    failed = [r.extra_fields for r in caplog.records if r.getMessage() == "usage.record_failed"]
    assert [(f["kind"], f["source_type"], f["input_count"]) for f in failed] == [
        ("embedding", "product", 5), ("embedding", "product_search", 5)]
    assert events() == []


def test_odd_usage_from_a_provider_never_loses_the_event(fashion, db, paid):
    paid(tokens=-1)  # a nonsensical count is stored as unknown, not refused
    ProductService(db, bid(fashion)).create(dict(SHIRT))
    paid(usage=False, served=None)
    ProductService(db, bid(fashion)).search("linen shirt")
    assert [(e.input_tokens, e.model) for e in events()] == [(None, SERVED), (None, None)]


# ---------------------------------------------------------------- the ledger's rules
@pytest.mark.parametrize("fields", [dict(is_real=False), dict(is_real=True), dict(message_kind="free_form"),
                                    dict(market="250")])
def test_an_embedding_event_never_carries_whatsapp_fields(fashion, db, fields):
    with pytest.raises(IntegrityError, match="ck_usage_events_wa_fields"):
        UsageEventRepo(db, bid(fashion)).record(kind="embedding", idempotency_key=f"emb:{uuid.uuid4()}",
                                                status="success", **fields)
        db.flush()
    db.rollback()


def test_embedding_events_can_never_be_changed_or_removed(fashion, db, paid):
    paid()
    ProductService(db, bid(fashion)).create(dict(SHIRT))
    db.commit()
    for sql in ("UPDATE usage_events SET units = 0 WHERE kind = 'embedding'",
                "DELETE FROM usage_events WHERE kind = 'embedding'"):
        with pytest.raises(IntegrityError, match="append-only"):
            db.execute(text(sql))
        db.rollback()
    assert len(events()) == 1


# ---------------------------------------------------------------- prices
def test_a_request_is_priced_from_the_reported_tokens_and_the_served_model(fashion, db, paid, prices):
    prices(price(MODEL, "2", match="prefix"), price(SERVED, "4"))
    paid()
    ProductService(db, bid(fashion)).create(dict(SHIRT))
    db.commit()
    fashion.import_csv("name,price\nWool scarf,9000\nCanvas bag,12000\n")
    # The exact entry for the served model wins over the prefix: 5 tokens x 4 = 20 millionths; the import, 10 x 4.
    assert [(e.cost_micros, e.currency, e.price_version) for e in events()] == [(20, "USD", "2026-10-10"),
                                                                               (40, "USD", "2026-10-10")]


def test_the_configured_model_prices_a_request_whose_served_model_is_unlisted_or_unknown(fashion, db, paid, prices):
    prices(price(MODEL, "2"))
    paid(served="another-model")
    ProductService(db, bid(fashion)).search("linen")
    paid(served=None)
    ProductService(db, bid(fashion)).search("linen")
    assert [e.cost_micros for e in events()] == [10, 10]


def test_unreported_usage_is_unpriced_and_a_failed_request_costs_nothing(fashion, db, paid, prices):
    prices(price(MODEL, "2", match="prefix"))
    paid(usage=False)
    ProductService(db, bid(fashion)).search("linen")
    paid(503)
    ProductService(db, bid(fashion)).search("linen")
    got = [(e.status, e.cost_micros, e.currency, e.price_version) for e in events()]
    assert got == [("success", None, None, "2026-10-10"), ("error", 0, "USD", "2026-10-10")]


def test_a_request_is_never_priced_by_another_providers_entry_or_by_model_prices(fashion, db, paid, tmp_path,
                                                                                    monkeypatch):
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"version": "v1", "currency": "USD",
                                "llm": [{"provider": "openai_compat", "model": MODEL, "input_per_1m": "9",
                                         "output_per_1m": "9"}],
                                "embeddings": [price(MODEL, "2", provider="other")]}), encoding="utf-8")
    monkeypatch.setattr(settings, "usage_pricing_file", str(path))
    paid()
    ProductService(db, bid(fashion)).search("linen")
    assert [(e.cost_micros, e.price_version) for e in events()] == [(None, "v1")]


def test_costs_are_rounded_half_to_even_and_tokens_are_always_kept():
    """A cost below half a millionth of the currency is stored as 0; the tokens stay on the event, so a total can be
    priced from summed tokens rather than from rounded per-event costs."""
    def cost(tokens: int, per_1m: str) -> int:
        entry = price(MODEL, per_1m)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(pricing, "current_price_list",
                       lambda: pricing.PriceList(version="v", currency="USD", embeddings=(entry,)))
            return pricing.embedding_request_price(provider="openai_compat", model=MODEL, configured_model=MODEL,
                                                   input_tokens=tokens, failed=False).cost_micros
    assert [cost(t, "0.1") for t in (3, 5, 15, 25, 1_000_000)] == [0, 0, 2, 2, 100_000]


def test_an_unusable_price_list_never_loses_the_event(fashion, db, paid, monkeypatch, caplog):
    paid()

    def broken(**kw):
        raise pricing.PricingError("USAGE_PRICING_FILE x: cannot be read")
    monkeypatch.setattr(pricing, "embedding_request_price", broken)
    with caplog.at_level("ERROR", logger="app"):
        ProductService(db, bid(fashion)).search("linen")
    assert [(e.status, e.cost_micros) for e in events()] == [("success", None)]
    assert any(r.getMessage() == "usage.pricing_failed" for r in caplog.records)


_E = '{"provider": "openai_compat", "model": "m", "input_per_1m": "1"}'


@pytest.mark.parametrize("raw, reason", [
    ('{"version": "v1", "currency": "USD", "embeddings": [' + _E + ", " + _E + "]}", "embeddings: openai_compat"),
    ('{"version": "v1", "currency": "USD", "embeddings": [' + _E.replace('"1"', '"-1"') + "]}",
     "greater than or equal to 0"),
    ('{"version": "v1", "currency": "USD", "embeddings": [' + _E.replace('"1"}', '"1", "output_per_1m": "1"}') + "]}",
     "output_per_1m"),  # embeddings have no output tokens: a copied model entry is refused, not half-read
    ('{"version": "v1", "currency": "USD", "embeddings": [' + _E.replace('"m",', '"m", "match": "fuzzy",') + "]}",
     "match"),
])
def test_an_invalid_embeddings_price_list_is_refused_with_the_reason(tmp_path, monkeypatch, raw, reason):
    path = tmp_path / f"prices-{uuid.uuid4().hex[:8]}.json"
    path.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(settings, "usage_pricing_file", str(path))
    with pytest.raises(pricing.PricingError) as exc:
        pricing.current_price_list()
    assert reason in str(exc.value) and "USAGE_PRICING_FILE" in str(exc.value)


def test_the_writer_keeps_only_what_the_ledger_can_hold(fashion):
    """record_embedding on its own: counts it cannot store become unknown, never an error."""
    assert usage_service.record_embedding(
        engine, bid(fashion), idempotency_key="emb:odd", source_type="product_search", source_id=None,
        provider="openai_compat", model="m" * 300, configured_model=MODEL, status="success", units=2**40,
        input_tokens=True, attempts=-3)
    [e] = events()
    assert (e.units, e.input_tokens, e.attempts, len(e.model)) == (0, None, 0, 100)
