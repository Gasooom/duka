"""Reusable conversation agent. One engine for every tenant; behaviour comes from config + data.

Context strategy (keeps tokens low):
  1. compact system prompt built from the tenant's AgentConfig
  2. deterministic state snapshot (last products shown, cart summary, last order) — cheap DB reads
  3. rolling summary of older messages (only once the conversation is long)
  4. last N customer/assistant messages (truncated)
Tool results are kept only within the current turn.
"""
import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.agents.intents import classify_confirmation, wants_human
from app.agents.providers import LLMError, LLMProvider, get_llm_provider
from app.core.config import settings
from app.core.errors import DomainError
from app.core.logging import get_logger, log_event
from app.models import AgentConfig, AgentRun, Business, Conversation, Customer, Message
from app.repositories.repos import AgentConfigRepo, AgentRunRepo
from app.services.commerce_service import CartService, CheckoutChanged, CheckoutService, OrderService, money
from app.services.conversation_service import ConversationService
from app.tools import commerce_tools  # noqa: F401  (registers tools)
from app.tools.registry import ToolContext, execute_tool, tools_for
from app.workflows.handoff import HANDOFF_REPLY, request_human
from app.workflows.orders import order_placed_text, place_confirmed_order

logger = get_logger(__name__)
GREETING_RE = re.compile(r"^\s*(hi|hello|hey|hola|bonjour|muraho|mwaramutse|habari|good (morning|afternoon|evening))"
                         r"[\s!.,]*(there)?[\s!.]*$", re.I)
MAX_MSG_CHARS = 600
MAX_TOOL_RESULT_CHARS = 3500


@dataclass
class AgentOutcome:
    text: str
    run: AgentRun
    handed_off: bool = False
    # Set when this reply IS a checkout summary: the caller links the sent message to the checkout, so only a
    # later customer YES to that delivered summary can place the order.
    checkout_cart_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None


def build_system_prompt(business: Business, cfg: AgentConfig) -> str:
    rules = [
        "Never invent products, prices, stock, delivery fees, discounts, order status or payment status. "
        "Get every fact from tools.",
        "For product questions call search_products first. Refer to products by their list position.",
        "For totals or delivery fees call calculate_cart_total / calculate_delivery. Never do arithmetic yourself.",
        "You cannot place orders. When the customer wants to order, call prepare_checkout (first ask for their "
        "delivery address if delivery applies). The system then sends the exact summary itself and places the "
        "order only if the customer replies YES.",
        "Only say an order is paid when a tool returns payment_status 'paid'. For payment, share only what "
        "initiate_payment returns. If the customer sends a transaction ID, call submit_payment_reference.",
        "For policy/FAQ questions call search_knowledge. If nothing is found, say you are not sure.",
        "If a tool returns ok=false, explain the problem simply and suggest a next step.",
    ]
    if business.human_handoff_enabled:
        rules.append("Call handoff_to_human if the customer asks for a person, is upset, or you cannot help.")
    parts = [
        f"You are the WhatsApp shopping assistant for {business.name} ({business.business_type}).",
        business.description or "",
        f"Tone: {cfg.tone}. Reply in {cfg.language} unless the customer uses another language. "
        "Keep replies short for WhatsApp: plain text, short numbered lists, no markdown tables.",
        f"Currency: {business.currency}. Delivery: {'available' if business.delivery_enabled else 'pickup only'}. "
        f"Online payment: {'mobile money' if business.payment_enabled else 'not available'}.",
        "RULES:\n- " + "\n- ".join(rules),
    ]
    if cfg.business_rules:
        parts.append(f"BUSINESS RULES:\n{cfg.business_rules}")
    if cfg.system_prompt:
        parts.append(cfg.system_prompt)
    return "\n\n".join(p for p in parts if p)


class AgentEngine:
    def __init__(self, db: Session, business: Business, provider: LLMProvider | None = None):
        self.db = db
        self.business = business
        self.provider = provider or get_llm_provider()
        self.convs = ConversationService(db, business.id)
        repo = AgentConfigRepo(db, business.id)
        self.cfg = repo.first() or repo.add()

    # ------------------------------------------------------------------ context
    def _state_snapshot(self, customer: Customer, conv: Conversation) -> tuple[str, dict[str, Any]]:
        state = dict(conv.state or {})
        lines = [f"Customer: {customer.name or 'unknown name'} (WhatsApp {customer.whatsapp_number})."]
        if state.get("last_products"):
            lines.append("Products last shown (position: name [product_id]): " + "; ".join(
                f"{i}: {p['name']} [{p['id']}]" for i, p in enumerate(state["last_products"], 1)))
        carts = CartService(self.db, self.business.id)
        cart = carts.get_active(customer, conv, create=False)
        if cart and cart.items:
            lines.append(f"Cart has {sum(i.quantity for i in cart.items)} item(s).")
            state["cart_items"] = sum(i.quantity for i in cart.items)
        unpaid = OrderService(self.db, self.business.id).latest_unpaid(customer)
        if unpaid:
            lines.append(f"Latest unpaid order: {unpaid.order_number} ({unpaid.status}, "
                         f"{unpaid.currency} {money(unpaid.total)}).")
            state["latest_unpaid_order"] = unpaid.order_number
        return "CONTEXT: " + " ".join(lines), state

    def _history(self, conv: Conversation) -> list[dict[str, Any]]:
        n = self.cfg.max_history_messages or settings.agent_max_history_messages
        msgs = self.convs.history(conv, limit=n, roles=("customer", "assistant", "human_agent"))
        out = []
        for m in msgs:
            role = "user" if m.role == "customer" else "assistant"
            out.append({"role": role, "content": m.content[:MAX_MSG_CHARS]})
        return out

    def _maybe_summarize(self, conv: Conversation) -> None:
        """Compact older history into conv.summary once the conversation gets long. With a real LLM
        this is one cheap call every `agent_summary_trigger_messages` messages."""
        total = self.convs.message_count(conv)
        trigger = settings.agent_summary_trigger_messages
        if total - conv.summarized_message_count < trigger:
            return
        keep = self.cfg.max_history_messages or settings.agent_max_history_messages
        older = self.convs.history(conv, roles=("customer", "assistant"))[:-keep]
        if not older:
            return
        transcript = "\n".join(f"{m.role}: {m.content[:300]}" for m in older[-40:])
        summary = None
        if self.provider.is_llm:
            try:
                resp = self.provider.complete([
                    {"role": "system", "content": "Summarize this shopping conversation in <=80 words: customer "
                                                  "preferences, products discussed, decisions. No prices."},
                    {"role": "user", "content": (conv.summary or "") + "\n" + transcript}], [], temperature=0)
                summary = resp.content
            except LLMError:
                summary = None
        if not summary:
            asks = [m.content[:80] for m in older if m.role == "customer"][-5:]
            summary = "Earlier the customer said: " + " | ".join(asks)
        conv.summary = summary[:1000]
        conv.summarized_message_count = total

    def build_messages(self, customer: Customer, conv: Conversation) -> list[dict[str, Any]]:
        snapshot, state = self._state_snapshot(customer, conv)
        messages: list[dict[str, Any]] = [{"role": "system", "content": build_system_prompt(self.business, self.cfg)},
                                          {"role": "system", "content": snapshot}]
        if not self.provider.is_llm:
            messages.append({"role": "system", "content": "STATE_JSON:" + json.dumps(state, default=str)})
        if conv.summary:
            messages.append({"role": "system", "content": f"Earlier conversation summary: {conv.summary}"})
        messages.extend(self._history(conv))
        return messages

    # ------------------------------------------------------------------ run
    def run(self, customer: Customer, conv: Conversation, trigger: Message) -> AgentOutcome:
        start = time.perf_counter()
        run = AgentRunRepo(self.db, self.business.id).add(
            conversation_id=conv.id, customer_id=customer.id, trigger_message_id=trigger.id,
            provider=self.provider.name, model=self.cfg.model or (settings.llm_model if self.provider.is_llm else "rules"),
            status="running", input_text=trigger.content, steps=[], llm_calls=0)
        steps: list[dict[str, Any]] = []
        prompt_tokens = completion_tokens = 0
        text: str | None = None
        handed_off = False
        outcome = AgentOutcome(text="", run=run)
        checkout_summary: tuple[str, str] | None = None  # (summary text, cart id) from prepare_checkout

        # Deterministic steps first: order confirmation and "talk to a person" never depend on the LLM.
        deterministic = self._deterministic_turn(customer, conv, trigger, outcome)
        if deterministic is not None:
            text, run.status, step = deterministic
            steps.append({"type": "deterministic", **step})
            handed_off = outcome.handed_off
        # Cost control: a bare greeting never needs the LLM.
        elif GREETING_RE.match(trigger.content or ""):
            text = self.cfg.greeting
            run.status = "fast_path"
            steps.append({"type": "fast_path", "reason": "greeting"})
        else:
            try:
                self._maybe_summarize(conv)
                messages = self.build_messages(customer, conv)
                tool_schemas = [t.schema() for t in tools_for(self.business)]
                ctx = ToolContext(db=self.db, business=self.business, customer=customer, conversation=conv)
                for _ in range(settings.agent_max_tool_iterations):
                    t0 = time.perf_counter()
                    resp = self.provider.complete(messages, tool_schemas, model=self.cfg.model,
                                                  temperature=float(self.cfg.temperature))
                    run.llm_calls += 1
                    prompt_tokens += resp.prompt_tokens or 0
                    completion_tokens += resp.completion_tokens or 0
                    steps.append({"type": "llm", "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                                  "prompt_tokens": resp.prompt_tokens, "completion_tokens": resp.completion_tokens,
                                  "decision": "tool_calls" if resp.tool_calls else "respond",
                                  "content": (resp.content or "")[:500] or None})
                    if not resp.tool_calls:
                        text = (resp.content or "").strip() or None
                        break
                    messages.append({"role": "assistant", "content": resp.content or "",
                                     "tool_calls": [{"id": c.id, "type": "function",
                                                     "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                                                    for c in resp.tool_calls]})
                    for call in resp.tool_calls:
                        result, latency = execute_tool(ctx, call.name, call.arguments)
                        handed_off = handed_off or (call.name == "handoff_to_human" and result.get("ok"))
                        if call.name == "prepare_checkout" and result.get("ok"):
                            checkout_summary = (result["summary_text"], result["cart"]["cart_id"])
                        result_json = json.dumps(result, default=str)
                        steps.append({"type": "tool", "tool": call.name, "arguments": call.arguments,
                                      "ok": result.get("ok"), "error": result.get("error"),
                                      "result": json.loads(result_json) if len(result_json) < 6000 else result_json[:6000],
                                      "latency_ms": round(latency, 1)})
                        self.convs.add_message(conv, "tool_call", f"{call.name}({json.dumps(call.arguments)})",
                                               metadata={"tool": call.name, "arguments": call.arguments},
                                               agent_run_id=run.id)
                        self.convs.add_message(conv, "tool_result", result_json[:4000],
                                               metadata={"tool": call.name, "ok": result.get("ok")}, agent_run_id=run.id)
                        messages.append({"role": "tool", "tool_call_id": call.id,
                                         "content": result_json[:MAX_TOOL_RESULT_CHARS]})
                run.status = "success" if text else "error"
                if not text:
                    run.error = "No final response within the tool-iteration limit"
            except Exception as exc:  # LLM outage, bad response... never crash the webhook
                run.status = "error"
                run.error = f"{type(exc).__name__}: {str(exc)[:500]}"
                steps.append({"type": "error", "error": run.error})
                log_event(logger, "agent.error", 40, operation="agent.run", status="error", error=run.error)
            if checkout_summary is not None:
                # The customer confirms exactly what the server computed, never a paraphrase by the model.
                text, outcome.checkout_cart_id = checkout_summary[0], uuid.UUID(checkout_summary[1])
                steps.append({"type": "checkout_summary", "cart_id": checkout_summary[1]})
        if not text:
            text = self.cfg.fallback_message
        run.steps = steps
        run.response_text = text
        run.prompt_tokens = prompt_tokens or None
        run.completion_tokens = completion_tokens or None
        run.latency_ms = int((time.perf_counter() - start) * 1000)
        self.db.flush()
        log_event(logger, "agent.run", operation="agent.run", status=run.status, duration_ms=run.latency_ms,
                  llm_calls=run.llm_calls, tools=[s["tool"] for s in steps if s["type"] == "tool"])
        outcome.text, outcome.handed_off = text, handed_off
        return outcome

    def _deterministic_turn(self, customer: Customer, conv: Conversation, trigger: Message,
                            outcome: AgentOutcome) -> tuple[str, str, dict[str, Any]] | None:
        """(reply, run status, debug step) when the turn is handled without the LLM, else None."""
        checkout = CheckoutService(self.db, self.business.id)
        if checkout.pending(customer):
            answer = classify_confirmation(trigger.content)
            if answer == "yes":
                try:
                    with self.db.begin_nested():  # a failure mid-way (e.g. stock race) leaves no partial order
                        order = place_confirmed_order(self.db, self.business, customer, conv, trigger)
                except CheckoutChanged as exc:
                    checkout.cancel(customer)
                    try:
                        with self.db.begin_nested():
                            summary = checkout.prepare(customer, conv)
                    except DomainError as exc2:
                        return f"{exc.message} {exc2.message}", "checkout_changed", {"reason": exc.message}
                    outcome.checkout_cart_id = summary.cart_id
                    return (f"{exc.message} Here is the updated summary:\n\n{summary.text}", "checkout_changed",
                            {"reason": exc.message})
                except DomainError as exc:
                    return f"Sorry — I couldn't place the order: {exc.message}", "checkout_failed", {"reason": exc.message}
                outcome.order_id = order.id
                self.convs.set_state(conv, last_order=order.order_number)
                return order_placed_text(order, self.business), "order_confirmed", {"order_number": order.order_number}
            if answer == "no":
                checkout.cancel(customer)
                return ("No problem — the order was not placed. What would you like to change?",
                        "checkout_declined", {})
        if wants_human(trigger.content):
            if self.business.human_handoff_enabled:
                request_human(self.db, self.business, conv, "Customer asked for a person")
                outcome.handed_off = True
                return HANDOFF_REPLY, "handoff", {"reason": "customer asked for a person"}
            contact = f" You can reach us at {self.business.phone}." if self.business.phone else ""
            return (f"Our team isn't available on this chat right now.{contact}", "handoff_unavailable",
                    {"reason": "handoff disabled"})
        return None


__all__ = ["AgentEngine", "AgentOutcome", "build_system_prompt"]
