"""Agent engine: tool loop with a real OpenAI-compatible client (mocked HTTP), safety rails,
fallbacks, run recording, cost controls."""
import json
import uuid

import httpx

from app.agents.engine import build_system_prompt
from app.agents.providers import LLMError, LLMProvider, LLMResponse, ToolCall, set_provider_override
from app.agents.providers.openai_compat import OpenAICompatProvider
from app.models import AgentRun, Business, Message
from app.tools.registry import TOOLS, tools_for

REQUIRED_TOOLS = {"search_products", "get_product", "check_inventory", "get_business_information",
                  "search_knowledge", "create_cart", "get_cart", "add_to_cart", "remove_from_cart", "clear_cart",
                  "calculate_cart_total", "create_order", "get_order", "get_customer_orders", "check_order_status",
                  "calculate_delivery", "initiate_payment", "handoff_to_human"}


def test_all_required_tools_registered_with_schemas():
    assert REQUIRED_TOOLS <= set(TOOLS)
    for t in TOOLS.values():
        s = t.schema()
        assert s["function"]["name"] == t.name and s["function"]["parameters"]["type"] == "object"


def test_tools_follow_business_config(fashion, db):
    b = db.get(Business, uuid.UUID(fashion.business_id))
    b.payment_enabled = False
    b.delivery_enabled = False
    names = {t.name for t in tools_for(b)}
    assert "initiate_payment" not in names and "calculate_delivery" not in names


class OpenAIStub:
    """Mimics /chat/completions: first asks for search_products, then answers using the tool result."""

    def __init__(self):
        self.requests = []

    def handler(self, req: httpx.Request):
        body = json.loads(req.content)
        self.requests.append(body)
        if body["messages"][-1]["role"] != "tool":
            return httpx.Response(200, json={"model": "stub-1", "usage": {"prompt_tokens": 120, "completion_tokens": 20},
                                             "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                                                 {"id": "c1", "type": "function", "function": {
                                                     "name": "search_products",
                                                     "arguments": json.dumps({"query": "black sneakers", "max_price": 100000})}}]}}]})
        result = json.loads(body["messages"][-1]["content"])
        names = ", ".join(p["name"] for p in result["products"])
        return httpx.Response(200, json={"model": "stub-1", "usage": {"prompt_tokens": 300, "completion_tokens": 40},
                                         "choices": [{"message": {"role": "assistant", "content": f"We have: {names}"}}]})


def test_openai_compatible_tool_loop(fashion, outbox, db):
    stub = OpenAIStub()
    set_provider_override(OpenAICompatProvider(base_url="https://llm.test/v1", api_key="k", model="stub-1",
                                               client=httpx.Client(transport=httpx.MockTransport(stub.handler))))
    fashion.send("Hi, I'm looking for black sneakers under 100,000 RWF.")
    reply = outbox.sent[-1][1]
    assert "Adidas Samba OG Black" in reply and "Nike Air Max" not in reply
    first = stub.requests[0]
    assert {t["function"]["name"] for t in first["tools"]} >= {"search_products", "add_to_cart"}
    assert first["messages"][0]["role"] == "system" and "Never invent" in first["messages"][0]["content"]
    run = db.query(AgentRun).one()
    assert run.status == "success" and run.llm_calls == 2
    assert run.prompt_tokens == 420 and run.completion_tokens == 60
    tool_step = [s for s in run.steps if s["type"] == "tool"][0]
    assert tool_step["tool"] == "search_products" and tool_step["arguments"]["max_price"] == 100000
    assert tool_step["result"]["count"] == 4
    roles = [m.role for m in db.query(Message).order_by(Message.created_at)]
    assert roles == ["customer", "tool_call", "tool_result", "assistant"]


class Scripted(LLMProvider):
    name = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)
        self.seen = []

    def complete(self, messages, tools, *, model=None, temperature=0.2):
        self.seen.append(messages)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_llm_failure_returns_fallback_and_webhook_survives(fashion, outbox, db):
    set_provider_override(Scripted([LLMError("upstream 503")]))
    r = fashion.send("do you have jackets?")
    assert r.status_code == 200
    assert outbox.sent[-1][1].startswith("Sorry, I'm having trouble")
    run = db.query(AgentRun).one()
    assert run.status == "error" and "upstream 503" in run.error


def test_invalid_and_unknown_tool_calls_are_contained(fashion, outbox, db):
    set_provider_override(Scripted([
        LLMResponse(content=None, tool_calls=[ToolCall("1", "add_to_cart", {"quantity": "lots"}),
                                              ToolCall("2", "drop_database", {})]),
        LLMResponse(content="Which product would you like?"),
    ]))
    fashion.send("add something")
    run = db.query(AgentRun).one()
    tools = [s for s in run.steps if s["type"] == "tool"]
    assert tools[0]["ok"] is False and "Invalid arguments" in tools[0]["error"]
    assert tools[1]["ok"] is False and "Unknown" in tools[1]["error"]
    assert outbox.sent[-1][1] == "Which product would you like?"


def test_tool_iteration_limit(fashion, outbox, db):
    loop = LLMResponse(content=None, tool_calls=[ToolCall("1", "get_cart", {})])
    set_provider_override(Scripted([loop] * 10))
    fashion.send("cart?")
    run = db.query(AgentRun).one()
    assert run.llm_calls == 5 and run.status == "error"
    assert outbox.sent[-1][1].startswith("Sorry")


def test_greeting_fast_path_skips_llm(fashion, outbox, db):
    scripted = Scripted([])
    set_provider_override(scripted)
    fashion.send("Hello!")
    assert scripted.seen == []
    assert db.query(AgentRun).one().status == "fast_path"


def test_context_is_compact_and_carries_state(fashion, outbox, db):
    for i in range(10):  # 20 messages: below the summary trigger (24)
        fashion.send(f"black sneakers {i}")
    scripted = Scripted([LLMResponse(content="ok")])
    set_provider_override(scripted)
    fashion.send("add the second one")
    msgs = scripted.seen[0]
    history = [m for m in msgs if m["role"] in ("user", "assistant")]
    assert len(history) <= 8  # bounded history, not the whole conversation
    ctx = next(m["content"] for m in msgs if m["content"].startswith("CONTEXT:"))
    assert "Products last shown" in ctx and "2: " in ctx


def test_system_prompt_is_built_from_tenant_config(fashion, db):
    fashion.patch("/api/business/agent-config", json={"tone": "very formal", "business_rules": "No refunds on sale items."})
    b = db.get(Business, uuid.UUID(fashion.business_id))
    db.refresh(b.agent_config)
    p = build_system_prompt(b, b.agent_config)
    assert "very formal" in p and "No refunds on sale items." in p and "Kigali Fashion" in p


def test_engine_uses_rules_provider_offline(fashion, outbox):
    fashion.send("black sneakers under 100k")
    assert "Adidas Samba OG Black" in outbox.sent[-1][1]


def test_long_conversations_get_summarized(fashion, outbox, db, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "agent_summary_trigger_messages", 6)
    for i in range(5):
        fashion.send(f"show me jeans option {i}")
    from app.models import Conversation
    conv = db.query(Conversation).one()
    assert conv.summary and "jeans" in conv.summary

