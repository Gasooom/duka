"""Grounding check for model-written replies.

Commerce facts must come from structured tool results or server state, never from the model. Layers:
  1. The highest-stakes texts are never model-written (order summary/confirmation, payment and order status
     notifications are server-rendered), and no tool lets the model change prices, orders or payments.
  2. This module builds a typed *ledger* of the facts the server gave the model this turn (money values, stock,
     order numbers, order/payment statuses, products, other numbers in tool data) and checks every claim in
     the model's reply against it:
       - a money amount must equal a money value from the tools; next to a single product it must be THAT
         product's price (or a cart total when the sentence talks about totals/delivery);
       - an order number must appear in tool results/state; "placed/confirmed" needs an order fact;
       - "paid" / "payment received" needs payment_status 'paid'; delivered/on the way/accepted/cancelled
         needs that order status;
       - availability claims need product facts and must not contradict stock;
       - any other number must appear in tool data, server context or the customer's own message.
  3. On any violation the engine sends the deterministic render of the same tool results instead of the
     model's text (agents/render.py), or a safe clarifying message if there are none.
Known limit: a product name the model invents *alongside* real products is not detected by name; its price,
stock or availability claims still are. The evaluation suite (M10) measures this with the real model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

MONEY_KEYS = {"price", "unit_price", "line_total", "subtotal", "delivery_fee", "fee", "discount", "total", "amount"}
STOCK_KEYS = {"stock_quantity", "available_stock"}
ORDER_NO_RE = re.compile(r"\b[A-Z]{1,6}-\d{3,}\b")
CURRENCY = r"(?:rwf|frw|rwfs|francs?|kes|ksh|ugx|tzs|usd|eur|\$|€)"
NUMBER_RE = re.compile(r"(?<![\w.,])(\d{1,3}(?:[,   ]\d{3})+|\d+)(?:[.,](\d{1,2}))?\s*(k\b)?", re.I)
LIST_MARKER_RE = re.compile(r"^\s*(\d{1,2})[.)]\s")
ORDINAL_RE = re.compile(r"\b(\d{1,2})(st|nd|rd|th)\b", re.I)
TOTAL_WORDS = re.compile(r"\b(total|subtotal|delivery|fee|shipping|altogether|in all|overall|cart|igiteranyo|montant)\b", re.I)
BUDGET_RE = re.compile(r"\b(under|below|less than|within|up to|budget|max(imum)?|cheaper than|munsi ya|moins de|chini ya)\b",
                       re.I)
MONEY_WORDS = re.compile(r"\b(price|costs?|priced|total|subtotal|fee|pay|amount|igiciro|prix|bei)\b", re.I)

PLACED_RE = re.compile(r"\border\b[^.\n]{0,40}\b(placed|confirmed|created|submitted|booked)\b|"
                       r"\b(placed|confirmed|created|submitted)\s+(your|the|an)\s+order\b", re.I)
PAID_RE = re.compile(r"\b(is|are|was|been|now|fully|already)\s+(fully\s+)?paid\b|"
                     r"\bpayment\s+(has been\s+|was\s+|is\s+)?(received|confirmed|successful|complete|completed)\b", re.I)
NOT_PAID_RE = re.compile(r"\b(not|n't|unpaid|once|after|until|when|if)\b[^.\n]{0,25}\bpaid\b|\bnot yet paid\b", re.I)
STATUS_CLAIMS = {
    "delivered": re.compile(r"\b(is|has been|was|been)\s+delivered\b", re.I),
    "out_for_delivery": re.compile(r"\b(out for delivery|on (its|the) way|shipped|dispatched)\b", re.I),
    "accepted": re.compile(r"\b(is|has been|was)\s+accepted\b", re.I),
    "cancelled": re.compile(r"\b(is|has been|was)\s+cancell?ed\b", re.I),
    "ready": re.compile(r"\b(is|has been)\s+ready\b", re.I),
}
AVAILABLE_RE = re.compile(r"\b(in stock|available|we have|we've got|we carry|turabifite|disponible|tunayo)\b", re.I)
NEGATED_AVAIL_RE = re.compile(r"\b(not|no longer|n't|out of stock|unavailable|sold out)\b", re.I)
PROMPT_LEAK_RE = re.compile(r"(RULES:|BUSINESS RULES:|CONTEXT:|STATE_JSON|system prompt)")
CART_CLAIM_RE = re.compile(r"\b(added|i've added|i have added|is in your cart|are in your cart|removed from your cart)\b",
                           re.I)
ID_STRING_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
ID_KEYS = {"id", "sku", "cart_id", "product_id", "payment_id", "conversation_id", "customer_id", "order_id"}


@dataclass
class LedgerProduct:
    name: str
    price: Decimal | None
    in_stock: bool | None
    money: set[Decimal] = field(default_factory=set)


@dataclass
class Ledger:
    money: set[Decimal] = field(default_factory=set)
    numbers: set[Decimal] = field(default_factory=set)
    stock: set[Decimal] = field(default_factory=set)
    order_numbers: set[str] = field(default_factory=set)
    order_statuses: set[str] = field(default_factory=set)
    payment_statuses: set[str] = field(default_factory=set)
    products: dict[str, LedgerProduct] = field(default_factory=dict)
    has_product_facts: bool = False
    has_cart_facts: bool = False
    has_order_facts: bool = False
    product_positions: int = 0
    customer_numbers: set[Decimal] = field(default_factory=set)
    customer_text: str = ""


@dataclass
class Violation:
    kind: str
    detail: str


def _dec(v: Any) -> Decimal | None:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return d.normalize() if d == d.to_integral() else d


def _numbers_in(text: str) -> set[Decimal]:
    out: set[Decimal] = set()
    for m in NUMBER_RE.finditer(text or ""):
        v = _parse_number(m)
        if v is not None:
            out.add(v)
    for part in re.findall(r"\d+", text or ""):  # also bare digit groups, e.g. "08:00" -> 8, 0
        out.add(Decimal(int(part)))
    return out


def _parse_number(m: re.Match) -> Decimal | None:
    whole = re.sub(r"[,   ]", "", m.group(1))
    value = _dec(f"{whole}.{m.group(2)}" if m.group(2) else whole)
    if value is None:
        return None
    if m.group(3):
        value = _dec(value * 1000)
    return value


def build_ledger(tool_results: list[tuple[str, dict, dict]], state: dict | None, customer_text: str) -> Ledger:
    led = Ledger(customer_text=customer_text or "", customer_numbers=_numbers_in(customer_text or ""))
    for name, _args, result in tool_results:
        if not result.get("ok"):
            continue
        _walk(result, led, key=None)
        if name in ("add_to_cart", "get_cart", "create_cart", "calculate_cart_total", "remove_from_cart",
                    "clear_cart", "prepare_checkout"):
            led.has_cart_facts = True
        if name in ("get_order", "check_order_status", "get_customer_orders"):
            led.has_order_facts = True
        if name == "search_products":
            led.product_positions = max(led.product_positions, len(result.get("products") or []))
    # Products the tools actually returned with facts this turn (an empty search gives none).
    led.has_product_facts = any(p.price is not None or p.in_stock is not None for p in led.products.values())
    state = state or {}
    for p in state.get("last_products") or []:  # shown in an earlier turn; names only, no facts
        led.products.setdefault(p["name"].lower(), LedgerProduct(p["name"], None, None))
    led.product_positions = max(led.product_positions, len(state.get("last_products") or []))
    for key in ("last_order", "latest_unpaid_order"):
        if state.get(key):
            led.order_numbers.add(str(state[key]).upper())
    return led


def _walk(node: Any, led: Ledger, key: str | None) -> None:
    if isinstance(node, dict):
        name = node.get("name") or node.get("product_name")
        if name and ("price" in node or "unit_price" in node or "in_stock" in node or "line_total" in node):
            p = led.products.setdefault(str(name).lower(), LedgerProduct(str(name), None, None))
            price = _dec(node.get("price", node.get("unit_price")))
            p.price = price if price is not None else p.price
            if "in_stock" in node:
                p.in_stock = bool(node["in_stock"]) and node.get("active", True) is not False
            for k in ("price", "unit_price", "line_total", "subtotal"):
                if node.get(k) is not None and _dec(node[k]) is not None:
                    p.money.add(_dec(node[k]))
        if "order_number" in node:
            led.order_numbers.add(str(node["order_number"]).upper())
            if node.get("status"):
                led.order_statuses.add(str(node["status"]))
            if node.get("payment_status"):
                led.payment_statuses.add(str(node["payment_status"]))
        for k, v in node.items():
            _walk(v, led, k)
    elif isinstance(node, list):
        for v in node:
            _walk(v, led, key)
    elif isinstance(node, bool) or node is None:
        return
    elif isinstance(node, (int, float, Decimal)):
        d = _dec(node)
        if d is None:
            return
        led.numbers.add(d)
        if key in MONEY_KEYS:
            led.money.add(d)
        if key in STOCK_KEYS:
            led.stock.add(d)
    elif isinstance(node, str):
        led.order_numbers |= {o.upper() for o in ORDER_NO_RE.findall(node)}
        if key not in ID_KEYS and not (key or "").endswith("_id") and not ID_STRING_RE.match(node):
            led.numbers |= _numbers_in(node)


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def _mentioned_products(sentence: str, led: Ledger) -> list[LedgerProduct]:
    low = sentence.lower()
    found = []
    for key, p in led.products.items():
        tokens = [t for t in re.findall(r"[a-z0-9]+", key) if len(t) > 2]
        if key in low or (len(tokens) >= 2 and sum(t in low for t in tokens) >= min(2, len(tokens))):
            found.append(p)
    return found


def _is_money(sentence: str, start: int, end: int, value: Decimal) -> bool:
    window = sentence[max(0, start - 8):end + 8].lower()
    if re.search(CURRENCY, window):
        return True
    return bool(MONEY_WORDS.search(sentence)) and value >= 100


def verify(reply: str, led: Ledger) -> list[Violation]:
    violations: list[Violation] = []
    if PROMPT_LEAK_RE.search(reply):
        violations.append(Violation("prompt_leak", "reply exposes internal instructions"))
    for order_no in ORDER_NO_RE.findall(reply):
        if order_no.upper() not in led.order_numbers and order_no.upper() not in led.customer_text.upper():
            violations.append(Violation("order_number", order_no))
    for sentence in _sentences(reply):
        clean = ORDER_NO_RE.sub(" ", sentence)
        mentioned = _mentioned_products(clean, led)
        list_marker = LIST_MARKER_RE.match(clean)
        ordinals = {Decimal(int(m.group(1))) for m in ORDINAL_RE.finditer(clean)}
        money_in_sentence: list[Decimal] = []
        for m in NUMBER_RE.finditer(clean):
            value = _parse_number(m)
            if value is None:
                continue
            if list_marker and m.start(1) == list_marker.start(1) and value <= max(led.product_positions, 10):
                continue
            if value in ordinals and value <= max(led.product_positions, 1):
                continue
            if _is_money(clean, m.start(), m.end(), value):
                if value in led.customer_numbers and BUDGET_RE.search(clean):
                    continue  # repeating the customer's own budget ("under 100,000"), not a price claim
                money_in_sentence.append(value)
                if value not in led.money:
                    violations.append(Violation("money", f"{m.group(0).strip()} is not a price/total from the tools"))
            elif value not in led.numbers and value not in led.customer_numbers:
                violations.append(Violation("number", f"{m.group(0).strip()} does not come from the tools"))
        if len(mentioned) == 1 and len(money_in_sentence) == 1 and not TOTAL_WORDS.search(clean):
            p = mentioned[0]
            allowed = p.money | ({p.price} if p.price is not None else set())
            if allowed and money_in_sentence[0] not in allowed:
                violations.append(Violation("price_mismatch", f"{money_in_sentence[0]} is not the price of {p.name}"))
        if PLACED_RE.search(clean) and not led.has_order_facts:
            violations.append(Violation("order_placed", "claims an order was placed/confirmed"))
        if PAID_RE.search(clean) and not NOT_PAID_RE.search(clean) and "paid" not in led.payment_statuses:
            violations.append(Violation("payment_status", "claims payment without a 'paid' status from the tools"))
        for status, rx in STATUS_CLAIMS.items():
            if rx.search(clean) and status not in led.order_statuses and not re.search(r"\b(will|once|when|after|if)\b", clean, re.I):
                violations.append(Violation("order_status", f"claims '{status}' without that status from the tools"))
        if AVAILABLE_RE.search(clean) and not NEGATED_AVAIL_RE.search(clean):
            with_facts = [p for p in led.products.values() if p.price is not None or p.in_stock is not None]
            if not led.has_product_facts:
                violations.append(Violation("availability", "claims availability but the tools returned no product"))
            elif not mentioned and len(with_facts) != 1:
                violations.append(Violation("availability", "claims availability of a product the tools did not return"))
            for p in mentioned:
                if p.in_stock is False:
                    violations.append(Violation("availability", f"{p.name} is out of stock"))
        if CART_CLAIM_RE.search(clean) and not led.has_cart_facts:
            violations.append(Violation("cart", "claims a cart change that no cart tool performed"))
    return violations
