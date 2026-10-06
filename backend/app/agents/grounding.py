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
       - a partial search result must not be described with a word it does not match (search returned the
         Kitenge dress for "red dress" with missing=["red"]: "the Kitenge dress is red" is rejected);
       - any other number (including specs written with a unit: 128GB, 250g) must appear in tool data, server
         context or the customer's own message.
     The model may also never imitate the server's order summary or ask for the YES that places an order.
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

from app.services.product_service import stem

MONEY_KEYS = {"price", "unit_price", "line_total", "subtotal", "delivery_fee", "fee", "discount", "total", "amount"}
STOCK_KEYS = {"stock_quantity", "available_stock"}
ORDER_NO_RE = re.compile(r"\b[A-Z]{1,6}-\d{3,}\b")
CURRENCY = r"(?:rwf|frw|rwfs|francs?|kes|ksh|ugx|tzs|usd|eur|\$|€|فرنك|جنيه|ريال|دولار)"
# Arabic-Indic (٠-٩) and Persian (۰-۹) digits and Arabic separators: a price written as ٩٥٬٠٠٠ must be checked
# exactly like 95,000, otherwise an Arabic reply could carry an unverified number.
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٬٫", "0123456789" "0123456789" ",.")


def western_digits(text: str) -> str:
    return (text or "").translate(_DIGITS)
NUMBER_RE = re.compile(r"(?<![\w.,])(\d{1,3}(?:[,   ]\d{3})+|\d+)(?:[.,](\d{1,2}))?\s*(k\b)?", re.I)
LIST_MARKER_RE = re.compile(r"^\s*(\d{1,2})[.)]\s")
ORDINAL_RE = re.compile(r"\b(\d{1,2})(st|nd|rd|th)\b", re.I)
TOTAL_WORDS = re.compile(r"\b(total|subtotal|delivery|fee|shipping|altogether|in all|overall|cart|igiteranyo|montant|"
                         r"jumla|livraison|الإجمالي|الاجمالي|المجموع|الجملة|التوصيل|السلة)\b", re.I)
BUDGET_RE = re.compile(r"\b(under|below|less than|within|up to|budget|max(imum)?|cheaper than|munsi ya|moins de|chini ya)\b",
                       re.I)
MONEY_WORDS = re.compile(r"\b(price|costs?|priced|total|subtotal|fee|pay|amount|igiciro|prix|bei|سعر|السعر|سعره|"
                         r"بكم|المبلغ|ثمن|رسوم|الإجمالي|الجملة)\b", re.I)

PLACED_RE = re.compile(r"\border\b[^.\n]{0,40}\b(placed|confirmed|created|submitted|booked)\b|"
                       r"\b(placed|confirmed|created|submitted)\s+(your|the|an)\s+order\b|"
                       r"(تم|اتم)\s*(تأكيد|تاكيد|تسجيل)\s*(الطلب|طلبك)|الطلب\s*(اتأكد|اتاكد|اتسجل|تأكد|تم)|سجلنا\s*(الطلب|طلبك)",
                       re.I)
# "Your order is pending until the payment is confirmed" / "will be confirmed once you reply YES" describe what
# will happen, they do not claim it happened.
CONDITIONAL_RE = re.compile(r"\b(until|once|when|after|if|as soon as|will|pending)\b", re.I)
PAID_RE = re.compile(r"\b(is|are|was|been|now|fully|already)\s+(fully\s+)?paid\b|"
                     r"\bpayment\s+(has been\s+|was\s+|is\s+)?(received|confirmed|successful|complete|completed)\b|"
                     r"(مدفوع|اتدفع|تم الدفع|استلمنا الدفع|الدفع وصل|وصلنا الدفع)", re.I)
# "n't" sits inside a word ("hasn't"), so it cannot be preceded by \b like the other negations. A conditional
# ("pending until the payment is confirmed") is not a claim either.
NOT_PAID_RE = re.compile(r"(?:\b(?:not|unpaid|once|after|until|when|if)\b|n't\b)[^.\n]{0,25}\b(?:paid|payment)\b|"
                         r"\bnot yet paid\b|"
                         r"(غير|ما|مش|لم)\s*(مدفوع|يتم الدفع|اتدفع)|(بعد|لمن|عندما)\s*(ما\s*)?(تدفع|الدفع)", re.I)
STATUS_CLAIMS = {
    "delivered": re.compile(r"\b(is|has been|was|been)\s+delivered\b|اتسلم|تم التسليم|تم توصيل", re.I),
    "out_for_delivery": re.compile(r"\b(out for delivery|on (its|the) way|shipped|dispatched)\b|في الطريق|في السكة",
                                   re.I),
    "accepted": re.compile(r"\b(is|has been|was)\s+accepted\b|اتقبل|تم قبول", re.I),
    "cancelled": re.compile(r"\b(is|has been|was)\s+cancell?ed\b|اتلغى|تم إلغاء|تم الغاء", re.I),
    "ready": re.compile(r"\b(is|has been)\s+ready\b", re.I),
}
AVAILABLE_RE = re.compile(r"\b(in stock|available|we have|we've got|we carry|turabifite|disponible|tunayo)\b|"
                          r"متوفر|متاح|موجود|عندنا", re.I)
NEGATED_AVAIL_RE = re.compile(r"\b(not|no longer|out of stock|unavailable|sold out)\b|n't\b|"
                              r"غير متوفر|غير متاح|ما موجود|ما متوفر|مافي|ما في|ما عندنا|خلص|نفد", re.I)
PROMPT_LEAK_RE = re.compile(r"(RULES:|BUSINESS RULES:|CONTEXT:|STATE_JSON|system prompt)")
# Only the server writes the order summary and asks for the YES that places an order. Live, the model wrote its own
# "🧾 Order summary — please check: ... Reply YES to confirm this order" after prepare_checkout had FAILED.
SUMMARY_IMITATION_RE = re.compile(r"🧾|\b(reply|answer|send|type|respond)\s+(with\s+)?[\"'«]?(yes|yego|oui|ndiyo)\b|"
                                  r"\bsubiza\s+yego\b|\br[ée]pondez\s+oui\b|\bjibu\s+ndiyo\b|(أرسل|رد|ردي)\s*(بـ)?\s*«?\s*(نعم|أيوه|ايوه)",
                                  re.I)
CART_CLAIM_RE = re.compile(r"\b(added|i've added|i have added|is in your cart|are in your cart|removed from your cart)\b",
                           re.I)
# A number written with a unit is a specification (128GB, 250g, 5000mAh, 750ml), never a price: it must come from
# the tool data like any other number, but it is not checked against prices.
UNIT_AFTER_RE = re.compile(r"\s?(?:k?gs?|grams?|gb|tb|mb|mah|ml|l|litres?|liters?|w|watts?|v|inch(?:es)?|in|cm|mm|"
                           r"hz|mp|gbps|mbps|%)(?![a-z])", re.I)
ID_STRING_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
ID_KEYS = {"id", "sku", "cart_id", "product_id", "payment_id", "conversation_id", "customer_id", "order_id"}


@dataclass
class LedgerProduct:
    name: str
    price: Decimal | None
    in_stock: bool | None
    money: set[Decimal] = field(default_factory=set)
    missing: set[str] = field(default_factory=set)  # search words this product does NOT match (partial result)
    from_context: bool = False  # current DB facts of a product shown earlier / in the cart, not a tool result now


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


def build_ledger(tool_results: list[tuple[str, dict, dict]], state: dict | None, customer_text: str,
                 context_products: list[dict] | None = None, owner_text: str = "") -> Ledger:
    customer_text = western_digits(customer_text or "")
    led = Ledger(customer_text=customer_text, customer_numbers=_numbers_in(customer_text))
    # The shop's own words (description, business rules) are authoritative: "12-month warranty", "free delivery
    # above RWF 100,000" may be repeated.
    owner_text = western_digits(owner_text or "")
    led.numbers |= _numbers_in(owner_text)
    for sentence in _sentences(owner_text):
        for m in NUMBER_RE.finditer(sentence):
            value = _parse_number(m)
            if value is not None and _is_money(sentence, m.start(), m.end(), value):
                led.money.add(value)
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
    # Current DB facts for products the customer was shown earlier or has in the cart: a follow-up such as "how
    # much is the Lenovo?" is often answered from the conversation, and is then checked against TODAY's price
    # and stock (a stale or wrong one is still rejected). Facts only: never evidence of a cart/order action.
    turn_products = set(led.products)
    for d in context_products or []:
        _walk(d, led, key=None)
        key = str(d.get("name") or "").lower()
        if key in led.products and key not in turn_products:
            led.products[key].from_context = True
    # Products with facts (an empty search gives none).
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
            p.missing |= {str(w).lower() for w in node.get("missing") or []}
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
    """Sentences, keeping a list item's marker with its text: "2. **Price**: RWF 210,000" is one sentence (split
    after "2." the marker looked like a number nobody returned)."""
    out: list[str] = []
    for line in re.split(r"\n+", text):
        marker = LIST_MARKER_RE.match(line)
        start = marker.end() if marker else 0
        parts = [p for p in re.split(r"(?<=[.!?])\s+", line[start:]) if p.strip()]
        if marker:
            parts = [line[:start] + (parts[0] if parts else "")] + parts[1:]
        out.extend(p for p in parts if p.strip())
    return out


def _name_spans(led: Ledger) -> list[str]:
    """Parts of product names that contain a number ("IdeaPad 3", "Air Max 90", "FreePods 4"), longest first: the
    number belongs to the name, it is not a price or a stock claim."""
    spans = set()
    for p in led.products.values():
        words = p.name.split()
        for i in range(len(words)):
            for j in range(i + 1, len(words) + 1):
                part = words[i:j]
                if any(re.search(r"\d", w) for w in part) and (len(part) >= 2 or len(words) == 1):
                    spans.add(" ".join(part))
    return sorted(spans, key=len, reverse=True)


def mentioned_products(sentence: str, led: Ledger) -> list[LedgerProduct]:
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
    reply = western_digits(reply)
    if PROMPT_LEAK_RE.search(reply):
        violations.append(Violation("prompt_leak", "reply exposes internal instructions"))
    if SUMMARY_IMITATION_RE.search(reply):
        violations.append(Violation("summary_imitation", "only the server writes the order summary and asks for YES"))
    for order_no in ORDER_NO_RE.findall(reply):
        if order_no.upper() not in led.order_numbers and order_no.upper() not in led.customer_text.upper():
            violations.append(Violation("order_number", order_no))
    name_spans = _name_spans(led)
    # A header such as "Here are two laptops available:" names no product: it is fine when the products the reply
    # does name are all known and in stock.
    named = [p for p in mentioned_products(reply, led) if p.price is not None or p.in_stock is not None]
    listing_ok = bool(named) and all(p.in_stock is not False for p in named)
    for sentence in _sentences(reply):
        clean = ORDER_NO_RE.sub(" ", sentence)
        mentioned = mentioned_products(clean, led)
        list_marker = LIST_MARKER_RE.match(clean)
        ordinals = {Decimal(int(m.group(1))) for m in ORDINAL_RE.finditer(clean)}
        money_in_sentence: list[Decimal] = []
        scan = clean
        for span in name_spans:
            scan = re.sub(rf"(?<![\w]){re.escape(span)}(?![\w])", lambda m: " " * len(m.group(0)), scan, flags=re.I)
        for m in NUMBER_RE.finditer(scan):
            value = _parse_number(m)
            if value is None:
                continue
            if list_marker and m.start(1) == list_marker.start(1) and value <= max(led.product_positions, 10):
                continue
            if value in ordinals and value <= max(led.product_positions, 1):
                continue
            if not m.group(3) and UNIT_AFTER_RE.match(scan, m.end()):
                if value not in led.numbers and value not in led.customer_numbers:
                    violations.append(Violation("number", f"{m.group(0).strip()} does not come from the tools"))
                continue
            if _is_money(scan, m.start(), m.end(), value):
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
        placed = PLACED_RE.search(clean)
        if placed and not led.has_order_facts and not CONDITIONAL_RE.search(placed.group(0)):
            violations.append(Violation("order_placed", "claims an order was placed/confirmed"))
        if PAID_RE.search(clean) and not NOT_PAID_RE.search(clean) and "paid" not in led.payment_statuses:
            violations.append(Violation("payment_status", "claims payment without a 'paid' status from the tools"))
        for status, rx in STATUS_CLAIMS.items():
            if rx.search(clean) and status not in led.order_statuses and not re.search(r"\b(will|once|when|after|if)\b", clean, re.I):
                violations.append(Violation("order_status", f"claims '{status}' without that status from the tools"))
        if AVAILABLE_RE.search(clean) and not NEGATED_AVAIL_RE.search(clean):
            # An unnamed "it's available" can only refer to the single product a tool returned THIS turn.
            with_facts = [p for p in led.products.values()
                          if (p.price is not None or p.in_stock is not None) and not p.from_context]
            if not led.has_product_facts:
                violations.append(Violation("availability", "claims availability but the tools returned no product"))
            elif not mentioned and len(with_facts) != 1 and not listing_ok:
                violations.append(Violation("availability", "claims availability of a product the tools did not return"))
            for p in mentioned:
                if p.in_stock is False:
                    violations.append(Violation("availability", f"{p.name} is out of stock"))
        if not NEGATED_AVAIL_RE.search(clean):
            unnamed = clean
            for p in led.products.values():
                unnamed = re.sub(re.escape(p.name), " ", unnamed, flags=re.I)
            for p in mentioned:  # a partial search result presented as what was asked ("the Kitenge dress is red")
                for word in sorted(p.missing):
                    if re.search(rf"\b{re.escape(stem(word))}(?:e?s)?\b", unnamed, re.I):
                        violations.append(Violation("attribute", f"{p.name} does not match '{word}'"))
        if CART_CLAIM_RE.search(clean) and not led.has_cart_facts:
            violations.append(Violation("cart", "claims a cart change that no cart tool performed"))
    return violations
