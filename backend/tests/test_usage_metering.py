"""Usage metering, first slice: the usage_events ledger and real AI model calls.

Every call the agent makes to a real model is recorded once, in its own transaction (so it survives the rollback of
the turn that made it), for the tenant whose conversation it served, with the served and the configured model,
tokens, attempts, status and an estimated cost from the operator's price list (USAGE_PRICING_FILE). The rules
engine, evaluation runs and operator checks record nothing. agent_runs.llm_calls is not the source of truth."""
import json
import logging
import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from app.agents.providers import (
    LLMError,
    LLMProvider,
    LLMResponse,
    UnavailableProvider,
    openai_compat,
    set_provider_override,
)
from app.agents.providers.openai_compat import OpenAICompatProvider
from app.core.config import settings
from app.db.session import SessionLocal, engine
from app.models import AgentRun, Business, Conversation, Message, UsageEvent, WebhookEvent
from app.repositories.repos import UsageEventRepo
from app.services import pricing
from app.services.usage_service import record_llm_call
from app.workflows import inbound
from evals.harness import AdversarialProvider, Harness, load_suite
from tests.conftest import drain, place_order

BACKEND = Path(__file__).resolve().parents[1]
SUITE = BACKEND / "evals" / "cases_v1.json"
ANSWER = "Which colour would you like?"  # passes the grounding check unchanged


class Model(LLMProvider):
    """Stands in for a real, metered model: returns (or raises) the scripted responses in order."""
    name = "scripted"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.asked: list[str | None] = []

    def complete(self, messages, tools, *, model=None, temperature=0.2, timeout=None):
        self.asked.append(model)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def answer(**kw) -> LLMResponse:
    return LLMResponse(content=ANSWER, **kw)


def openai_provider(handler, **kw) -> OpenAICompatProvider:
    """The production adapter over a mocked HTTP transport; its own default model is "configured-1"."""
    return OpenAICompatProvider(base_url="https://llm.test/v1", api_key="k", model="configured-1",
                                client=httpx.Client(transport=httpx.MockTransport(handler)), **kw)


def completion(content=None, tool_calls=None, *, served="configured-1-2026-07-01", usage=(100, 10)) -> httpx.Response:
    body: dict = {"choices": [{"message": {"role": "assistant", "content": content, "tool_calls": tool_calls}}]}
    if served:
        body["model"] = served
    if usage:
        body["usage"] = {"prompt_tokens": usage[0], "completion_tokens": usage[1]}
    return httpx.Response(200, json=body)


def events(**where) -> list[UsageEvent]:
    with SessionLocal() as s:
        return list(s.scalars(select(UsageEvent).filter_by(**where).order_by(UsageEvent.occurred_at)))


def count(model) -> int:
    with SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(model))


def record(business_id: uuid.UUID, key: str, **kw) -> bool:
    fields = dict(idempotency_key=key, source_type="agent_run", source_id=uuid.uuid4(), provider="scripted",
                  model="m", configured_model="m", status="success", input_tokens=1, output_tokens=1)
    return record_llm_call(engine, business_id, **{**fields, **kw})


def price(model: str, input_per_1m: str, output_per_1m: str, *, provider="scripted", match="exact") -> dict:
    return {"provider": provider, "model": model, "match": match, "input_per_1m": input_per_1m,
            "output_per_1m": output_per_1m}


@pytest.fixture(autouse=True)
def no_price_list(monkeypatch):
    monkeypatch.setattr(settings, "usage_pricing_file", "")  # a developer's .env must not change these tests


@pytest.fixture
def prices(tmp_path, monkeypatch):
    """Install a price list as the operator would: prices([entries], version=...) or prices(raw="<json>")."""
    def install(llm=None, *, version="2026-10-08", currency="USD", raw: str | None = None) -> Path:
        path = tmp_path / f"prices-{uuid.uuid4().hex[:8]}.json"
        path.write_text(raw if raw is not None else
                        json.dumps({"version": version, "currency": currency, "llm": llm or []}), encoding="utf-8")
        monkeypatch.setattr(settings, "usage_pricing_file", str(path))
        return path
    return install


# ---------------------------------------------------------------- AI model calls
def test_a_model_call_is_recorded_once_with_tokens_and_both_models(fashion, outbox, db, monkeypatch):
    monkeypatch.setattr(settings, "llm_allowed_models", "big-model")
    fashion.patch("/api/business/agent-config", json={"model": "big-model"})
    model = Model(answer(prompt_tokens=120, completion_tokens=20, model="big-model-2026-05-01"))
    set_provider_override(model)
    fashion.send("do you have jackets?")
    assert outbox.sent[-1][1] == ANSWER and model.asked == ["big-model"]
    run = db.query(AgentRun).one()
    [e] = events()
    assert (e.kind, e.status, e.units, e.attempts, e.tool_calls) == ("llm_call", "success", 1, 1, 0)
    assert (e.input_tokens, e.output_tokens) == (120, 20)
    # The served model is what the provider said; the configured one is what Duka asked for (the tenant's choice).
    assert (e.provider, e.model, e.configured_model) == ("scripted", "big-model-2026-05-01", "big-model")
    assert (e.source_type, e.source_id) == ("agent_run", run.id)
    assert e.business_id == uuid.UUID(fashion.business_id) and e.idempotency_key.startswith("llm:")
    assert (e.cost_micros, e.currency, e.price_version) == (None, None, None)  # no price list: unpriced
    assert run.model == "big-model"  # agent_runs is unchanged (it never knew the served model)


def test_the_production_adapter_records_each_call_with_the_model_the_provider_served(fashion, outbox):
    search = [{"id": "c1", "type": "function",
               "function": {"name": "search_products", "arguments": json.dumps({"query": "black sneakers"})}}]
    replies = [completion(tool_calls=search, usage=(120, 20)), completion("Which size do you wear?", usage=(300, 40))]
    set_provider_override(openai_provider(lambda req: replies.pop(0)))
    fashion.send("black sneakers please")
    first, second = events()
    assert [(e.input_tokens, e.output_tokens, e.tool_calls) for e in (first, second)] == [(120, 20, 1), (300, 40, 0)]
    assert {(e.provider, e.model, e.configured_model, e.attempts, e.status) for e in (first, second)} == \
        {("openai_compat", "configured-1-2026-07-01", "configured-1", 1, "success")}
    assert first.source_id == second.source_id and first.idempotency_key != second.idempotency_key  # one turn


def test_a_served_model_the_provider_does_not_report_is_not_assumed(fashion, outbox):
    set_provider_override(openai_provider(lambda req: completion("Which size do you wear?", served=None)))
    fashion.send("black sneakers please")
    [e] = events()
    assert e.model is None and e.configured_model == "configured-1"


def test_provider_retries_are_one_event_that_counts_every_attempt(fashion, outbox, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    replies = [httpx.Response(503, text="overloaded"), httpx.Response(429, text="slow down"),
               completion("Which size do you wear?", usage=(80, 9))]
    set_provider_override(openai_provider(lambda req: replies.pop(0)))
    fashion.send("black sneakers please")
    [e] = events()
    assert (e.status, e.attempts, e.input_tokens, e.output_tokens) == ("success", 3, 80, 9)


@pytest.mark.parametrize("responses, attempts", [
    ([(500, "boom")] * 3, 3),  # retried up to the provider's limit, then given up
    ([(401, "bad key")], 1),  # not retryable
    ([(200, "not json")], 1),  # a malformed answer
])
def test_a_failed_call_is_recorded_as_an_error_and_the_customer_gets_the_fallback(fashion, outbox, db, monkeypatch,
                                                                                    responses, attempts):
    monkeypatch.setattr("time.sleep", lambda s: None)
    replies = list(responses)

    def handler(req):
        status, body = replies.pop(0)
        return httpx.Response(status, text=body)

    set_provider_override(openai_provider(handler, max_attempts=3))
    fashion.send("black sneakers please")
    assert outbox.sent[-1][1].startswith("Sorry, I'm having trouble")
    assert db.query(AgentRun).one().status == "error"
    [e] = events()
    assert (e.status, e.attempts, e.model, e.input_tokens, e.output_tokens) == ("error", attempts, None, None, None)
    assert e.configured_model == "configured-1"


def test_an_error_from_any_model_provider_is_recorded(fashion, outbox):
    set_provider_override(Model(LLMError("upstream 503")))
    fashion.send("do you have jackets?")
    [e] = events()
    assert (e.status, e.attempts, e.provider, e.configured_model) == ("error", 1, "scripted", settings.llm_model)


def test_the_conversation_summary_call_is_recorded_too(fashion, outbox, db, monkeypatch):
    for t in ("black sneakers", "white sneakers", "do you have jackets?", "show me hoodies", "red dresses"):
        fashion.send(t)  # the offline engine: nothing to record
    assert events() == []
    monkeypatch.setattr(settings, "agent_summary_trigger_messages", 4)  # the next turn summarises older history
    set_provider_override(Model(
        LLMResponse(content="Customer looks at sneakers and jackets.", prompt_tokens=200, completion_tokens=15),
        answer(prompt_tokens=90, completion_tokens=8)))
    fashion.send("anything in size 42?")
    conv = db.query(Conversation).one()
    assert conv.summary == "Customer looks at sneakers and jackets."
    summary, turn = events()
    assert (summary.source_type, summary.source_id) == ("conversation", conv.id)
    assert (summary.input_tokens, summary.output_tokens, summary.status) == (200, 15, "success")
    assert (turn.source_type, turn.input_tokens, turn.output_tokens) == ("agent_run", 90, 8)
    assert summary.configured_model == turn.configured_model == settings.llm_model


def _make_due(db):
    db.execute(update(WebhookEvent).where(WebhookEvent.status == "retry")
               .values(next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    db.commit()


@pytest.mark.parametrize("first_call", ["success", "error"])
def test_usage_commits_on_its_own_and_survives_the_rollback_of_the_turn(fashion, outbox, db, monkeypatch, first_call):
    first = answer(prompt_tokens=50, completion_tokens=5) if first_call == "success" else LLMError("upstream 503")
    set_provider_override(Model(first, answer(prompt_tokens=60, completion_tokens=6)))
    send, seen = inbound.send_to_customer, {}

    def crash(turn_db, *args, **kwargs):
        # The model call is over and the turn's transaction is still open: another connection already sees the
        # usage event, but not the agent run the turn is about to roll back.
        with SessionLocal() as other:
            seen["usage"] = other.scalar(select(func.count()).select_from(UsageEvent))
            seen["runs"] = other.scalar(select(func.count()).select_from(AgentRun))
        seen["runs_in_turn"] = turn_db.scalar(select(func.count()).select_from(AgentRun))
        raise RuntimeError("crash after the model call")

    monkeypatch.setattr(inbound, "send_to_customer", crash)
    fashion.send("do you have jackets?", wa_id="wamid.METERED")
    assert seen == {"usage": 1, "runs": 0, "runs_in_turn": 1}
    # The whole turn rolled back and will be retried; the usage event did not roll back with it.
    assert count(AgentRun) == count(Message) == 0 and outbox.sent == []
    assert db.scalars(select(WebhookEvent.status)).one() == "retry"
    [recorded] = events()
    assert recorded.status == first_call and db.get(AgentRun, recorded.source_id) is None

    # Application retry: the turn runs again and calls the model again — a new call, a new event.
    monkeypatch.setattr(inbound, "send_to_customer", send)
    _make_due(db)
    drain()
    assert outbox.sent[-1][1] == ANSWER and count(AgentRun) == 1
    again, retry = events()
    assert again.id == recorded.id and retry.idempotency_key != recorded.idempotency_key
    assert (retry.status, retry.input_tokens) == ("success", 60)
    # Usage outlives the data it was for: deleting the conversation (with its messages and agent runs) and the
    # customer, and purging processed webhook events, removes none of it.
    db.execute(text("DELETE FROM conversations"))
    db.execute(text("DELETE FROM customers"))
    db.execute(text("DELETE FROM webhook_events"))
    db.commit()
    assert count(AgentRun) == count(Message) == 0 and count(UsageEvent) == 2


def test_a_repeated_idempotency_key_is_recorded_once(fashion, electronics):
    a, b = uuid.UUID(fashion.business_id), uuid.UUID(electronics.business_id)
    assert record(a, "llm:same") is True
    assert record(a, "llm:same", input_tokens=999) is False  # the same event again: a no-op, nothing overwritten
    assert record(b, "llm:same") is True  # keys are unique per tenant: tenants never collide
    assert [(e.business_id, e.input_tokens) for e in events(idempotency_key="llm:same")] == [(a, 1), (b, 1)]


def test_concurrent_writers_of_one_key_store_one_event(fashion):
    bid, results = uuid.UUID(fashion.business_id), []
    barrier = threading.Barrier(6)

    def write():
        barrier.wait()
        results.append(record(bid, "llm:raced"))

    threads = [threading.Thread(target=write) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(results) == [False] * 5 + [True] and count(UsageEvent) == 1


def test_odd_values_from_a_provider_never_lose_the_event(fashion):
    assert record(uuid.UUID(fashion.business_id), "llm:odd", input_tokens=-5, output_tokens="12", tool_calls=True,
                  model="x" * 300)
    [e] = events()
    assert (e.input_tokens, e.output_tokens, e.tool_calls, len(e.model)) == (None, None, 0, 100)


def test_a_metering_failure_never_breaks_the_reply(fashion, outbox, monkeypatch, caplog):
    def broken(self, **fields):
        raise RuntimeError("usage table unavailable")

    monkeypatch.setattr(UsageEventRepo, "record", broken)
    set_provider_override(Model(answer(prompt_tokens=10, completion_tokens=5, model="served-1")))
    with caplog.at_level(logging.ERROR, logger="app"):
        fashion.send("do you have jackets?")
    assert outbox.sent[-1][1] == ANSWER
    [log] = [r for r in caplog.records if r.getMessage() == "usage.record_failed"]
    fields = log.extra_fields  # what was not stored, for the operator
    assert (fields["kind"], fields["model"], fields["input_count"], fields["output_count"]) == \
        ("llm_call", "served-1", 10, 5)
    assert count(UsageEvent) == 0


# ---------------------------------------------------------------- what is never metered
def test_the_rules_engine_records_no_ai_usage(fashion, outbox, db):
    place_order(fashion)  # search, add, address, YES: a whole order through the offline engine
    fashion.send("Hello!")  # greeting fast path
    runs = db.query(AgentRun).all()
    assert sum(r.llm_calls for r in runs) > 0  # agent_runs.llm_calls counts the rules engine as calls...
    assert count(UsageEvent) == 0  # ...the usage ledger only records real model calls


def test_a_provider_with_no_model_behind_it_records_nothing(fashion, outbox):
    set_provider_override(UnavailableProvider("LLM_API_KEY is not set"))
    fashion.send("do you have jackets?")
    assert outbox.sent[-1][1].startswith("Sorry, I'm having trouble") and count(UsageEvent) == 0


def test_llm_check_is_an_operator_call_and_records_nothing(fashion, monkeypatch, capsys):
    from app.cli import main
    call = [{"id": "1", "type": "function", "function": {"name": "search_products", "arguments": '{"query": "shoes"}'}}]

    class Mocked(OpenAICompatProvider):
        def __init__(self, **kw):
            super().__init__(api_key="k", client=httpx.Client(transport=httpx.MockTransport(
                lambda req: completion(tool_calls=call, served="served-1"))), **kw)

    monkeypatch.setattr(openai_compat, "OpenAICompatProvider", Mocked)
    assert main(["llm-check"]) == 0
    assert "model=served-1" in capsys.readouterr().out
    assert count(UsageEvent) == 0


def _one_case_per_category_and_language() -> list[dict]:
    """Every kind of flow in every language, without running the whole suite again (test_evals.py does)."""
    picked, covered = [], set()
    for case in load_suite(SUITE)["cases"]:
        keys = {("category", case["category"]), ("language", case.get("language", "en"))}
        if not case.get("requires_llm") and keys - covered:
            picked.append(case)
            covered |= keys
    return picked


@pytest.mark.parametrize("provider", ["rules", "adversarial"])
def test_evaluation_runs_record_no_usage(monkeypatch, provider):
    after_each_case = []
    reset = Harness.reset

    def reset_after_checking(self):  # each case starts by wiping the previous one: look first
        after_each_case.append(count(UsageEvent))
        reset(self)

    monkeypatch.setattr(Harness, "reset", reset_after_checking)
    cases = _one_case_per_category_and_language()
    results = Harness(SessionLocal, provider).run(cases, include_llm_cases=False)
    after_each_case.append(count(UsageEvent))
    assert len(after_each_case) == len(cases) + 1 and set(after_each_case) == {0}
    if provider == "adversarial":  # a model did answer (and was caught lying), yet nothing was metered
        assert any(t.run_status == "ungrounded" for r in results for t in r.turns)


def test_outside_an_evaluation_run_the_same_model_is_metered(fashion, outbox):
    """The exclusion above is the harness marking its provider unmetered, not something about the provider."""
    set_provider_override(AdversarialProvider())
    fashion.send("black sneakers")
    assert count(UsageEvent) > 0


# ---------------------------------------------------------------- pricing
def test_prices_are_exact_decimals_whether_written_as_strings_or_numbers(prices):
    prices(raw='{"version": "v1", "currency": "USD", "llm": ['
               '{"provider": "scripted", "model": "m", "input_per_1m": 0.123, "output_per_1m": "4.567"}]}')
    [entry] = pricing.current_price_list().llm
    assert entry.input_per_1m == Decimal("0.123") and entry.output_per_1m == Decimal("4.567")  # never a float

    def cost(i, o):
        return pricing.llm_call_price(provider="scripted", model="m", configured_model=None, input_tokens=i,
                                      output_tokens=o, failed=False).cost_micros

    assert cost(1_000_000, 1_000_000) == 4_690_000  # 0.123 + 4.567 = 4.69 dollars, in millionths
    assert cost(1234, 567) == 2741  # 151.782 + 2589.489 = 2741.271 millionths
    assert cost(0, 0) == 0


def test_cost_rounds_half_to_even(prices):
    prices([price("m", "0.5", "0")])

    def cost(i):
        return pricing.llm_call_price(provider="scripted", model="m", configured_model=None, input_tokens=i,
                                      output_tokens=0, failed=False).cost_micros

    assert [cost(1), cost(3), cost(5)] == [0, 2, 2]  # 0.5, 1.5, 2.5 millionths: no systematic rounding up


def test_the_served_model_price_wins_over_the_configured_model(fashion, outbox, prices):
    prices([price("served-x", "1", "2"), price(settings.llm_model, "100", "200")])
    set_provider_override(Model(answer(prompt_tokens=1000, completion_tokens=100, model="served-x")))
    fashion.send("do you have jackets?")
    [e] = events()
    assert (e.cost_micros, e.currency, e.price_version) == (1000 * 1 + 100 * 2, "USD", "2026-10-08")


def test_the_configured_model_prices_a_call_whose_served_model_is_unlisted_or_unknown(fashion, outbox, prices):
    prices([price(settings.llm_model, "0.5", "1.5")])
    set_provider_override(Model(answer(prompt_tokens=1000, completion_tokens=100, model="not-in-the-list"),
                                answer(prompt_tokens=1000, completion_tokens=100)))  # served model not reported
    fashion.send("do you have jackets?")
    fashion.send("and hoodies?")
    assert [(e.model, e.cost_micros) for e in events()] == [("not-in-the-list", 650), (None, 650)]


def test_prefix_entries_match_dated_model_names_and_the_most_specific_entry_wins(prices):
    prices([price("acme-chat", "5", "5", match="prefix"), price("acme-chat-mini", "1", "1", match="prefix"),
            price("acme-chat-mini-2026-01-01", "9", "9")])
    price_list = pricing.current_price_list()
    assert price_list.llm_price("scripted", "acme-chat-mini-2026-02-02").model == "acme-chat-mini"  # longest prefix
    assert price_list.llm_price("scripted", "acme-chat-2026-02-02").model == "acme-chat"
    assert price_list.llm_price("scripted", "acme-chat-mini-2026-01-01").match == "exact"  # exact beats prefixes
    assert price_list.llm_price("scripted", "acme") is None
    assert price_list.llm_price("openai_compat", "acme-chat-mini") is None  # entries are per provider


def test_a_model_missing_from_the_price_list_is_kept_unpriced(fashion, outbox, prices):
    prices([price("some-other-model", "1", "1")])
    set_provider_override(Model(answer(prompt_tokens=10, completion_tokens=2, model="served-unlisted")))
    fashion.send("do you have jackets?")
    [e] = events()
    assert (e.cost_micros, e.currency, e.price_version) == (None, None, "2026-10-08")
    assert (e.input_tokens, e.output_tokens) == (10, 2)  # the usage itself is kept, ready to be priced later


def test_unreported_usage_is_unpriced_and_a_failed_call_costs_nothing(fashion, outbox, prices):
    prices([price(settings.llm_model, "1", "1")])
    set_provider_override(Model(answer(), LLMError("upstream 503")))  # no token counts; then an outage
    fashion.send("do you have jackets?")
    fashion.send("and hoodies?")
    unreported, failed = events()
    assert (unreported.input_tokens, unreported.cost_micros, unreported.price_version) == (None, None, "2026-10-08")
    assert (failed.status, failed.cost_micros, failed.currency, failed.price_version) == \
        ("error", 0, "USD", "2026-10-08")


def test_each_event_keeps_the_price_list_version_it_was_priced_with(fashion, outbox, prices):
    prices([price(settings.llm_model, "1", "1")], version="2026-10")
    set_provider_override(Model(answer(prompt_tokens=100, completion_tokens=0),
                                answer(prompt_tokens=100, completion_tokens=0)))
    fashion.send("do you have jackets?")
    prices([price(settings.llm_model, "3", "3")], version="2026-11")  # the operator publishes new prices
    fashion.send("and hoodies?")
    assert [(e.cost_micros, e.price_version) for e in events()] == [(100, "2026-10"), (300, "2026-11")]


_P = '{"provider": "p", "model": "m", "input_per_1m": "1", "output_per_1m": "1"}'


@pytest.mark.parametrize("raw, reason", [
    ("not json", "not valid JSON"),
    ('{"currency": "USD"}', "version"),
    ('{"version": "v1", "currency": "usd"}', "currency"),
    ('{"version": "v1", "currency": "USD", "llm": [' + _P.replace('"1", "output', '"-1", "output') + "]}",
     "greater than or equal to 0"),
    ('{"version": "v1", "currency": "USD", "llm": [' + _P.replace("input_per_1m", "input_per_1M") + "]}",
     "input_per_1M"),  # a typo is refused, not ignored
    ('{"version": "v1", "currency": "USD", "llm": [' + _P.replace('"1", "output', 'NaN, "output') + "]}",
     "input_per_1m"),
    ('{"version": "v1", "currency": "USD", "llm": [' + _P.replace('"m",', '"m", "match": "fuzzy",') + "]}",
     "match"),
    ('{"version": "v1", "currency": "USD", "llm": [' + _P + ", " + _P + "]}", "listed twice"),
])
def test_an_invalid_price_list_is_refused_with_the_reason(prices, raw, reason):
    prices(raw=raw)
    with pytest.raises(pricing.PricingError) as exc:
        pricing.current_price_list()
    assert reason in str(exc.value) and "USAGE_PRICING_FILE" in str(exc.value)


def test_a_missing_price_list_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "usage_pricing_file", str(tmp_path / "missing.json"))
    with pytest.raises(pricing.PricingError, match="cannot be read"):
        pricing.current_price_list()


def test_the_api_refuses_to_start_with_a_broken_price_list(tmp_path):
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    good.write_text('{"version": "v1", "currency": "USD", "llm": [' + _P + "]}", encoding="utf-8")
    bad.write_text('{"version": "v1", "llm": []}', encoding="utf-8")

    def start(path):
        return subprocess.run([sys.executable, "-c", "import app.main"], cwd=BACKEND, capture_output=True, text=True,
                              env={**os.environ, "USAGE_PRICING_FILE": str(path)}, timeout=120)

    assert start(good).returncode == 0
    r = start(bad)
    assert r.returncode != 0 and "PricingError" in r.stderr and "currency" in r.stderr


# ---------------------------------------------------------------- insert-only ledger
def test_usage_events_can_be_inserted_but_never_updated_or_deleted(fashion, electronics, db):
    """The ledger is the durable record of usage and cost: rows are inserted, never changed or removed, whoever
    asks: raw SQL, the ORM or the tenant repository."""
    a, b = uuid.UUID(fashion.business_id), uuid.UUID(electronics.business_id)
    assert record(a, "llm:kept") is True  # INSERT
    [kept] = events()
    for sql, error in (("UPDATE usage_events SET cost_micros = 0", "append-only"),
                       ("UPDATE usage_events SET input_tokens = 1000000", "append-only"),
                       (f"UPDATE usage_events SET business_id = '{b}'", "immutable"),
                       ("DELETE FROM usage_events", "append-only"),
                       (f"DELETE FROM usage_events WHERE id = '{kept.id}'", "append-only")):
        with pytest.raises(IntegrityError, match=error):
            db.execute(text(sql))
        db.rollback()
    repo = UsageEventRepo(db, a)
    with pytest.raises(IntegrityError, match="append-only"):
        repo.update(repo.get(kept.id), cost_micros=0)
    db.rollback()
    with pytest.raises(IntegrityError, match="append-only"):
        repo.delete(repo.get(kept.id))
    db.rollback()
    [e] = events()
    assert (e.id, e.business_id, e.idempotency_key, e.input_tokens, e.cost_micros) == (kept.id, a, "llm:kept", 1, None)
    assert record(a, "llm:next") is True and count(UsageEvent) == 2  # inserting still works


def test_no_delete_or_cascade_can_remove_usage(db):
    """Insert-only through the audit trail's trigger function (duka_append_only), here on UPDATE and DELETE. The
    business cannot be deleted while it has usage either (ON DELETE RESTRICT, the ledger's only foreign key)."""
    triggers = db.execute(text(
        "SELECT t.tgname FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid "
        "WHERE t.tgrelid = 'usage_events'::regclass AND p.proname = 'duka_append_only'")).scalars().all()
    assert triggers == ["usage_events_append_only"]
    fks = db.execute(text("SELECT confrelid::regclass::text, confdeltype FROM pg_constraint "
                          "WHERE conrelid = 'usage_events'::regclass AND contype = 'f'")).all()
    assert [tuple(fk) for fk in fks] == [("businesses", "r")]  # r = RESTRICT
    shop = Business(name="Bare Shop", slug=f"bare-{uuid.uuid4().hex[:8]}")  # no users, nothing else to cascade
    db.add(shop)
    db.commit()
    record(shop.id, "llm:a")
    with pytest.raises(IntegrityError, match="usage_events"):
        db.execute(text("DELETE FROM businesses WHERE id = :b"), {"b": shop.id})
    db.rollback()
    assert [e.idempotency_key for e in events()] == ["llm:a"]
