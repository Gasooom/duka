"""Deterministic rules provider (NOT an LLM).

Purpose: zero-cost offline development and deterministic end-to-end tests. It implements the
same LLMProvider interface and drives the *same* tools, so everything below the agent
(tools, services, DB, WhatsApp, payments) is exercised for real. It understands a fixed set of
English commerce phrasings; production deployments should set LLM_PROVIDER=openai_compat."""
import json
import re
from typing import Any

from app.agents.providers.base import LLMProvider, LLMResponse, ToolCall
from app.agents.render import render_tool_result

ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "last": -1, "one": 1, "two": 2,
            "three": 3, "four": 4, "five": 5}
PRICE_RE = re.compile(
    r"(?:under|below|less than|max(?:imum)?|up to|within|cheaper than|not more than)\s*(?:rwf|frw|ksh|kes|\$|usd)?\s*"
    r"([\d][\d,\.]*)\s*(k|000)?", re.I)
ORDER_NO_RE = re.compile(r"\b([A-Z]{1,6}-\d{3,})\b", re.I)
LOCATION_RE = re.compile(r"\b(?:deliver(?:ed|y)?\s+to|to|in|at)\s+([A-Za-z][A-Za-z\s\-]{2,40})$", re.I)
ADDRESS_RE = re.compile(r"\b(?:deliver(?:ed|y)?\s+(?:it\s+)?to|my address is|address(?: is)?:?|ship\s+to)\s*:?\s+(.{3,200})$",
                        re.I)
PAYMENT_REF_RE = re.compile(r"\b(?:transaction(?: id)?|txn(?: id)?|ref(?:erence)?|momo ref)\b\s*(?:id|is|:|#)?\s*"
                            r"([A-Za-z0-9][A-Za-z0-9\-\.]{3,})", re.I)


def _price(text: str) -> float | None:
    m = PRICE_RE.search(text)
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    if m.group(2) and m.group(2).lower() == "k":
        n *= 1000
    return n


def _location(text: str) -> str | None:
    m = LOCATION_RE.search(text.strip().rstrip("?.!"))
    if m:
        loc = m.group(1).strip()
        if loc.lower() not in {"me", "you", "it", "my cart", "cart", "order", "the order"}:
            return loc
    return None


def _position(text: str, last_count: int) -> str | None:
    t = text.lower()
    m = re.search(r"(?:#|number |no\.? ?|item |add |option )(\d{1,2})\b", t) or re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\b", t)
    if m:
        return m.group(1)
    for word, n in ORDINALS.items():
        if re.search(rf"\b{word}\b", t):
            return str(last_count if n == -1 else n)
    if last_count == 1 and re.search(r"\b(it|that|this one|that one)\b", t):
        return "1"
    return None


class RulesProvider(LLMProvider):
    name = "rules"
    is_llm = False

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, model: str | None = None,
                 temperature: float = 0.2, timeout: float | None = None) -> LLMResponse:
        allowed = {t["function"]["name"] for t in tools}
        if messages and messages[-1]["role"] == "tool":
            return LLMResponse(content=self._render(messages, self._state(messages).get("language", "en")),
                               model="rules")
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "") or ""
        state = self._state(messages)
        call = self._intent(user, state, allowed)
        if call is None:
            return LLMResponse(content="I can help you find products, manage your cart, place orders and pay. "
                                       "What are you looking for?", model="rules")
        name, args = call
        return LLMResponse(content=None, tool_calls=[ToolCall(id="rules_1", name=name, arguments=args)], model="rules")

    @staticmethod
    def _state(messages: list[dict[str, Any]]) -> dict[str, Any]:
        for m in messages:
            if m["role"] == "system" and m.get("content", "").startswith("STATE_JSON:"):
                try:
                    return json.loads(m["content"][len("STATE_JSON:"):])
                except json.JSONDecodeError:
                    return {}
        return {}

    def _intent(self, text: str, state: dict[str, Any], allowed: set[str]) -> tuple[str, dict] | None:
        t = text.lower().strip()
        last_count = len(state.get("last_products") or [])

        def ok(name: str) -> bool:
            return name in allowed

        if ok("handoff_to_human") and re.search(r"\b(human|real person|agent|staff|manager|complain|complaint)\b", t):
            return "handoff_to_human", {"reason": text[:200]}
        ref = PAYMENT_REF_RE.search(text)
        if ok("submit_payment_reference") and ref and re.search(r"\b(paid|sent|transaction|txn|ref)", t):
            m = ORDER_NO_RE.search(text.replace(ref.group(1), ""))  # the reference itself may look like one
            return "submit_payment_reference", {k: v for k, v in {"reference": ref.group(1),
                                                                   "order_number": m.group(1) if m else None}.items() if v}
        address = ADDRESS_RE.search(text.strip())
        if state.get("cart_items") and address:
            return "prepare_checkout", {"delivery_address": address.group(1).strip(" .!")}
        if ok("initiate_payment") and re.search(r"^(pay|pay now|i want to pay|i'?ll pay|make (a )?payment|checkout and pay)\b|\bpay (for )?(it|the order|my order|now)\b", t):
            m = ORDER_NO_RE.search(text)
            phone = re.search(r"\b(\+?\d{9,15})\b", text)
            return "initiate_payment", {k: v for k, v in {"order_number": m.group(1) if m else None,
                                                           "phone_number": phone.group(1) if phone else None}.items() if v}
        if re.search(r"\bmy orders\b|\border history\b", t):
            return "get_customer_orders", {}
        if re.search(r"\b(status|track|where is my order|has my order)\b", t) or (ORDER_NO_RE.search(text) and "order" in t):
            m = ORDER_NO_RE.search(text)
            return "check_order_status", {"order_number": m.group(1)} if m else {}
        if re.search(r"\b(place|confirm|make|complete|submit)\b.{0,20}\border\b|\bcheck ?out\b|\border it\b", t):
            loc = _location(text)
            return "prepare_checkout", {"delivery_address": loc} if loc else {}
        if re.search(r"\b(clear|empty)\b.{0,15}\b(cart|basket)\b", t):
            return "clear_cart", {}
        if re.search(r"\b(remove|delete|take out)\b", t):
            pos = _position(t, last_count) or "1"
            return "remove_from_cart", {"product_ref": pos}
        if re.search(r"\b(add|i'?ll take|i want (the|that)|give me (the|that)|buy (the|that))\b", t):
            pos = _position(t, last_count)
            qty = re.search(r"\b(\d{1,2})\s*(?:x|pcs|pieces|units|of)\b", t)
            if pos is None and last_count == 1:
                pos = "1"
            if pos is None:
                ref = re.sub(r"^.*?\b(add|take|want|give me|buy)\b\s*(the|a|an)?\s*", "", t).strip(" ?.!") or "1"
                pos = ref
            args: dict[str, Any] = {"product_ref": pos}
            if qty:
                args["quantity"] = int(qty.group(1))
            return "add_to_cart", args
        if re.search(r"\b(how much|total|including delivery|grand total|what do i owe)\b", t):
            loc = _location(text)
            return "calculate_cart_total", {"delivery_location": loc} if loc else {}
        if re.search(r"\b(cart|basket)\b", t):
            return "get_cart", {}
        if re.search(r"\b(return|refund|exchange|warranty|guarantee|policy|outside|deliver|delivery|shipping)\b", t):
            if ok("calculate_delivery") and re.search(r"\b(fee|cost|charge|price)\b", t) and _location(text):
                return "calculate_delivery", {"location": _location(text)}
            return "search_knowledge", {"query": text}
        if re.search(r"\b(open|hours|address|located|location|phone number|contact)\b", t):
            return "get_business_information", {}
        if len(t) < 2:
            return None
        args = {"query": text}
        price = _price(text)
        if price is not None:
            args["max_price"] = price
            args["query"] = PRICE_RE.sub("", text).strip()
        return "search_products", args

    @staticmethod
    def _render(messages: list[dict[str, Any]], lang: str = "en") -> str:
        # Render the tool results produced in this turn (after the last assistant tool_calls message).
        idx = max(i for i, m in enumerate(messages) if m["role"] == "assistant" and m.get("tool_calls"))
        calls = {c["id"]: c for c in messages[idx]["tool_calls"]}
        parts = []
        for m in messages[idx + 1:]:
            if m["role"] != "tool":
                continue
            call = calls.get(m["tool_call_id"], {})
            fn = call.get("function", {})
            parts.append(render_tool_result(fn.get("name", ""), json.loads(fn.get("arguments") or "{}"),
                                            json.loads(m["content"]), lang))
        return "\n\n".join(parts)
