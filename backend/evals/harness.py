"""Evaluation harness for the commerce agent.

Runs versioned conversations (evals/cases_v*.json) through the REAL pipeline — webhook payload -> durable inbox ->
agent (any provider) -> tools -> grounding check -> outbox — on a scratch database, and checks each turn against
system state: tools and arguments, facts that must / must not reach the customer, orders and totals, handoff,
payment status, database prices. Wording is free; facts are not.

Providers: `rules` (offline engine), `adversarial` (a model that lies on every reply: proves fabrications never
reach customers), `openai_compat` (the real model, from settings).
"""
from __future__ import annotations

import hashlib
import json
import re
import statistics
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from app.agents.providers import LLMProvider, LLMResponse, set_provider_override
from app.agents.providers.rules import RulesProvider
from app.db.base import Base
from app.integrations.whatsapp.adapters import SendResult, WhatsAppAdapter, set_adapter_override
from app.integrations.whatsapp.parser import build_text_webhook
from app.models import AgentRun, Business, Cart, Conversation, Customer, Message, Order, Product
from app.services.business_service import BusinessConfigService, register_business
from app.services.commerce_service import CartService, CheckoutService, OrderService
from app.services.conversation_service import ConversationService, CustomerService
from app.services.knowledge_service import KnowledgeService
from app.services.product_service import ProductService
from app.workflows.inbound import process_webhook_payload

ROOT = Path(__file__).resolve().parent
SEED_DATA = ROOT.parent / "seed" / "data"

STORES: dict[str, dict[str, Any]] = {
    "fashion": {
        "name": "Kigali Fashion", "csv": "kigali_fashion_products.csv", "prefix": "KF",
        "profile": {"description": "Sneakers, streetwear and African print fashion in Kigali.",
                    "phone": "+250788123456", "address": "Kigali Heights, KG 7 Ave, Kigali", "order_prefix": "KF"},
        "zones": [{"name": "Kigali City", "fee": 2000, "areas": ["Kigali", "Remera", "Kicukiro"], "is_default": True},
                  {"name": "Outside Kigali", "fee": 5000, "areas": ["Musanze", "Huye"]}],
    },
    "electronics": {
        "name": "Mama's Electronics", "csv": "mamas_electronics_products.csv", "prefix": "ME",
        "profile": {"description": "Phones, laptops and accessories with genuine warranty.",
                    "phone": "+250788654321", "address": "Downtown, KN 2 St, Kigali", "order_prefix": "ME"},
        "zones": [{"name": "Kigali", "fee": 3000, "areas": ["Kigali", "Remera", "Kicukiro"], "is_default": True}],
    },
}


# ---------------------------------------------------------------- providers
class AdversarialProvider(LLMProvider):
    """Worst-case model. It picks tools like the offline engine (so the tool data is realistic) and then lies in
    every reply: wrong prices, invented products, 'paid', 'delivered', 'order placed', free delivery, discounts."""
    name = "adversarial"
    is_llm = True  # so the production grounding check applies to everything it says

    def __init__(self):
        self.rules = RulesProvider()

    def complete(self, messages, tools, *, model=None, temperature=0.2, timeout=None) -> LLMResponse:
        if messages and messages[-1]["role"] == "tool":
            return LLMResponse(content=self._lie(messages), model="adversarial")
        resp = self.rules.complete(messages + [self._state(messages)], tools)
        if not resp.tool_calls:
            return LLMResponse(content="Our prices start at RWF 1,000 and your order is already paid.", model="adversarial")
        return resp

    @staticmethod
    def _state(messages) -> dict:
        """The offline engine reads state from STATE_JSON; LLMs get it as prose in CONTEXT. Rebuild it."""
        ctx = next((m["content"] for m in messages if m["role"] == "system" and m["content"].startswith("CONTEXT:")), "")
        state: dict[str, Any] = {}
        m = re.search(r"Cart has (\d+) item", ctx)
        if m:
            state["cart_items"] = int(m.group(1))
        shown = re.search(r"Products last shown \(position: name \[product_id\]\): (.*?)(?: Cart has| Latest|$)", ctx)
        if shown:
            state["last_products"] = [{"id": pid, "name": name} for name, pid in
                                      re.findall(r"\d+: (.*?) \[([0-9a-f-]{36})\]", shown.group(1))]
        return {"role": "system", "content": "STATE_JSON:" + json.dumps(state)}

    @staticmethod
    def _lie(messages) -> str:
        idx = max(i for i, m in enumerate(messages) if m["role"] == "assistant" and m.get("tool_calls"))
        names = {c["id"]: c["function"]["name"] for c in messages[idx]["tool_calls"]}
        lies = []
        for m in messages[idx + 1:]:
            if m["role"] != "tool":
                continue
            r, name = json.loads(m["content"]), names.get(m["tool_call_id"], "")
            cart = r.get("cart") or {}
            if not r.get("ok"):
                lies.append("Done! Your order is confirmed and paid.")
            elif name == "search_products" and r.get("products"):
                p = r["products"][0]
                lies.append(f"We have {p['name']} in stock for {p['currency']} {p['price'] + 1000:,.0f}. "
                            "We also have the iPhone 15 in stock!")
            elif name == "search_products":
                lies.append("Yes, we have that in stock for RWF 25,000!")
            elif cart:
                lies.append(f"Added! Your total is {cart.get('currency', 'RWF')} {cart.get('total', 0) + 1234:,.0f} "
                            "with free delivery.")
            elif name == "calculate_delivery":
                lies.append(f"Delivery costs RWF {max(r.get('fee', 0) - 1000, 0):,.0f}.")
            elif "order_number" in r or "order" in r:
                number = r.get("order_number") or r["order"]["order_number"]
                lies.append(f"Your order {number} is paid and has been delivered.")
            elif name == "search_knowledge":
                lies.append("We offer a 90-day refund and 50% off everything today.")
            else:
                lies.append("We're open 24/7, delivery is free and your order has been placed.")
        return " ".join(lies) or "Your order has been placed and paid."


def provider_for(name: str) -> LLMProvider:
    if name == "rules":
        return RulesProvider()
    if name == "adversarial":
        return AdversarialProvider()
    if name == "openai_compat":
        from app.agents.providers.openai_compat import OpenAICompatProvider
        return OpenAICompatProvider()
    raise ValueError(f"unknown provider {name}")


class Capture(WhatsAppAdapter):
    mode = "eval"

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_text(self, to, body):
        self.sent.append((to, body))
        return SendResult(ok=True, wa_message_id=f"wamid.eval.{uuid.uuid4().hex[:10]}", delivery_status="sent")


# ---------------------------------------------------------------- results
@dataclass
class TurnRecord:
    customer: str
    replies: list[str]
    tools: list[dict]
    run_status: str | None
    latency_ms: int | None
    failures: list[str] = field(default_factory=list)


@dataclass
class CaseResult:
    id: str
    category: str
    language: str
    critical: bool
    status: str  # pass | fail | skip
    failures: list[str] = field(default_factory=list)
    turns: list[TurnRecord] = field(default_factory=list)


def money(v: float | Decimal) -> str:
    return f"RWF {float(v):,.0f}"


def prompt_fingerprint() -> str:
    from app.agents.engine import build_system_prompt
    from app.models import AgentConfig
    from app.tools.registry import TOOLS
    b = Business(name="X", business_type="retail", currency="RWF", delivery_enabled=True, payment_enabled=True,
                 human_handoff_enabled=True)
    cfg = AgentConfig(tone="friendly and concise", language="en")
    blob = build_system_prompt(b, cfg) + json.dumps([t.schema() for t in TOOLS.values()], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def git_sha() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              cwd=ROOT, timeout=5).stdout.strip() or None
    except Exception:  # noqa: BLE001 - not a git checkout (e.g. inside the image)
        return None


# ---------------------------------------------------------------- harness
class Harness:
    def __init__(self, session_factory, provider_name: str):
        self.sf = session_factory
        self.provider_name = provider_name
        url = make_url(str(session_factory.kw["bind"].url))
        if not any(word in (url.database or "") for word in ("test", "eval")):
            raise RuntimeError(f"Refusing to run evals on database '{url.database}': it must contain 'test' or 'eval'")

    # -- fixtures
    def reset(self) -> None:
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        with self.sf() as db:
            db.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
            db.commit()

    def seed(self, store: str) -> tuple[uuid.UUID, str]:
        spec = STORES[store]
        with self.sf() as db:
            business, _, _ = register_business(db, business_name=spec["name"],
                                               email=f"eval-{store}-{uuid.uuid4().hex[:6]}@duka.dev",
                                               password="eval-password-123")
            cfg = BusinessConfigService(db, business.id)
            cfg.update_business(spec["profile"])
            for z in spec["zones"]:
                cfg.upsert_delivery_zone(z)
            pnid = f"eval-{store}-{uuid.uuid4().hex[:6]}"
            cfg.connect_whatsapp(phone_number_id=pnid, display_phone_number="+250700", waba_id=None,
                                 access_token=None, mode="dev")
            ProductService(db, business.id).import_csv((SEED_DATA / spec["csv"]).read_bytes())
            db.commit()
            return business.id, pnid

    def _setup(self, case: dict, bid: uuid.UUID, number: str) -> dict[str, str]:
        subs: dict[str, str] = {}
        setup = case.get("setup") or {}
        with self.sf() as db:
            for sku, qty in (setup.get("stock") or {}).items():
                db.scalars(select(Product).where(Product.business_id == bid, Product.sku == sku)).one().stock_quantity = qty
            for title, content in setup.get("knowledge") or []:
                KnowledgeService(db, bid).add_document(title, content)
            if setup.get("order"):
                self._direct_order(db, bid, number)
            if setup.get("other_customer_order"):
                subs["other_order"] = self._direct_order(db, bid, "250788999123").order_number
            db.commit()
        return subs

    @staticmethod
    def _direct_order(db, bid, number) -> Order:
        customer = CustomerService(db, bid).upsert_from_whatsapp(number, "Eval Customer")
        conv = ConversationService(db, bid).get_or_create_active(customer)
        carts = CartService(db, bid)
        product = db.scalars(select(Product).where(Product.business_id == bid).order_by(Product.price)).first()
        carts.add_item(carts.get_active(customer, conv), product, 1)
        CheckoutService(db, bid).prepare(customer, conv, delivery_address="Remera, KG 11 Ave")
        return OrderService(db, bid).create_from_cart(customer, conv)

    # -- run
    def run(self, cases: list[dict], include_llm_cases: bool) -> list[CaseResult]:
        results = []
        set_provider_override(provider_for(self.provider_name))
        try:
            for case in cases:
                if case.get("requires_llm") and not include_llm_cases:
                    results.append(CaseResult(case["id"], case["category"], case.get("language", "en"),
                                              bool(case.get("critical")), "skip", ["requires a real LLM"]))
                    continue
                results.append(self.run_case(case))
        finally:
            set_provider_override(None)
            set_adapter_override(None)
        return results

    def run_case(self, case: dict) -> CaseResult:
        self.reset()
        bid, pnid = self.seed(case["store"])
        number = "2507" + str(uuid.uuid4().int)[:8]
        subs = self._setup(case, bid, number)
        capture = Capture()
        set_adapter_override(capture)
        res = CaseResult(case["id"], case["category"], case.get("language", "en"), bool(case.get("critical")), "pass")
        for i, turn in enumerate(case["turns"]):
            wamid = f"wamid.eval.{case['id']}.{i}.{uuid.uuid4().hex[:6]}"
            spoken = turn["customer"]
            if isinstance(spoken, dict):
                payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
                    "metadata": {"phone_number_id": pnid},
                    "messages": [{"from": number, "id": wamid, "type": spoken["type"], spoken["type"]: {"id": "media"}}]}}]}]}
                label = f"[{spoken['type']}]"
            else:
                label = spoken.format(**subs) if subs else spoken
                payload = build_text_webhook(pnid, "+250700", number, label, wamid, "Eval Customer")
            before = len(capture.sent)
            process_webhook_payload(payload, self.sf)
            replies = [body for to, body in capture.sent[before:] if to == number]
            record = self._record(bid, number, wamid, label, replies)
            record.failures = self._check(turn.get("expect") or {}, bid, number, record)
            res.turns.append(record)
            res.failures += [f"turn {i + 1}: {f}" for f in record.failures]
        res.status = "fail" if res.failures else "pass"
        return res

    def _record(self, bid, number, wamid, label, replies) -> TurnRecord:
        with self.sf() as db:
            msg = db.scalar(select(Message).where(Message.business_id == bid, Message.wa_message_id == wamid))
            run = db.scalar(select(AgentRun).where(AgentRun.trigger_message_id == msg.id)) if msg else None
            tools = [{"tool": s["tool"], "arguments": s["arguments"], "ok": s["ok"]}
                     for s in (run.steps if run else []) if s["type"] == "tool"]
            return TurnRecord(label, replies, tools, run.status if run else None, run.latency_ms if run else None)

    def _check(self, exp: dict, bid, number, rec: TurnRecord) -> list[str]:
        f: list[str] = []
        reply = "\n".join(rec.replies)
        low = reply.lower()
        called = [t["tool"] for t in rec.tools]
        for t in exp.get("tools", []):
            if t not in called:
                f.append(f"expected tool {t}, got {called}")
        for t in exp.get("no_tools", []):
            if t in called:
                f.append(f"tool {t} must not be called")
        for tool, want in (exp.get("tool_args") or {}).items():
            args = next((t["arguments"] for t in rec.tools if t["tool"] == tool), None)
            for k, v in want.items():
                got = (args or {}).get(k)
                if got is None or (isinstance(v, (int, float)) and float(got) != float(v)) or \
                        (isinstance(v, str) and v.lower() not in str(got).lower()):
                    f.append(f"{tool}.{k}={got!r}, expected {v!r}")
        for s in exp.get("reply_has", []):
            if s.lower() not in low:
                f.append(f"reply lacks {s!r}")
        if exp.get("reply_has_any") and not any(s.lower() in low for s in exp["reply_has_any"]):
            f.append(f"reply has none of {exp['reply_has_any']}")
        for s in exp.get("reply_lacks", []):
            if s.lower() in low:
                f.append(f"reply must not contain {s!r}")
        for rx in exp.get("reply_lacks_regex", []):
            if re.search(rx, reply, re.I):
                f.append(f"reply must not match /{rx}/")
        if exp.get("grounded") and rec.run_status == "ungrounded":
            f.append("the model's reply failed the grounding check (replaced by the server rendering)")
        with self.sf() as db:
            customer = db.scalar(select(Customer).where(Customer.business_id == bid, Customer.whatsapp_number == number))
            orders = list(db.scalars(select(Order).where(Order.business_id == bid, Order.customer_id == customer.id)
                                     .order_by(Order.created_at))) if customer else []
            conv = db.scalar(select(Conversation).where(Conversation.business_id == bid,
                                                        Conversation.customer_id == customer.id)) if customer else None
            if "orders" in exp and len(orders) != exp["orders"]:
                f.append(f"{len(orders)} order(s), expected {exp['orders']}")
            if "order_total" in exp and (not orders or float(orders[-1].total) != float(exp["order_total"])):
                f.append(f"order total {orders[-1].total if orders else None}, expected {exp['order_total']}")
            if "payment_status" in exp and (not orders or orders[-1].payment_status != exp["payment_status"]):
                f.append(f"payment status {orders[-1].payment_status if orders else None}, expected {exp['payment_status']}")
            if "language" in exp and (conv.language_code if conv else None) != exp["language"]:
                f.append(f"conversation language {conv.language_code if conv else None}, expected {exp['language']}")
            # Product names and prices stay as in the catalog, so check for Arabic wording, not an Arabic majority.
            if exp.get("reply_script") == "arabic" and len(re.findall(r"[ء-ي]", reply)) < 10:
                f.append("reply is not written in Arabic")
            if "handoff" in exp and bool(conv and conv.status == "human") != exp["handoff"]:
                f.append(f"handoff={conv.status if conv else None}, expected {'human' if exp['handoff'] else 'ai'}")
            if "pending_summary" in exp:
                cart = db.scalar(select(Cart).where(Cart.business_id == bid, Cart.customer_id == customer.id,
                                                    Cart.status == "active")) if customer else None
                if bool(cart and cart.checkout) != exp["pending_summary"]:
                    f.append(f"pending summary={bool(cart and cart.checkout)}, expected {exp['pending_summary']}")
            if "cart_items" in exp:
                cart = db.scalar(select(Cart).where(Cart.business_id == bid, Cart.customer_id == customer.id,
                                                    Cart.status == "active")) if customer else None
                qty = sum(i.quantity for i in cart.items) if cart else 0
                if qty != exp["cart_items"]:
                    f.append(f"cart has {qty} item(s), expected {exp['cart_items']}")
            for sku, price in (exp.get("db_price") or {}).items():
                p = db.scalars(select(Product).where(Product.business_id == bid, Product.sku == sku)).one()
                if float(p.price) != float(price):
                    f.append(f"{sku} price changed to {p.price}")
            if "reply_has_db_price_of_position" in exp:
                shown = (conv.state or {}).get("last_products") or [] if conv else []
                pos = exp["reply_has_db_price_of_position"]
                if len(shown) < pos:
                    f.append(f"no product at position {pos}")
                else:
                    p = db.get(Product, uuid.UUID(shown[pos - 1]["id"]))
                    if money(p.price).lower() not in low:
                        f.append(f"reply lacks the catalog price {money(p.price)} of {p.name}")
        return f


# ---------------------------------------------------------------- report
def build_report(suite: dict, provider: str, model: str | None, results: list[CaseResult], seconds: float) -> dict:
    ran = [r for r in results if r.status != "skip"]
    by_cat: dict[str, dict[str, int]] = {}
    for r in results:
        c = by_cat.setdefault(r.category, {"pass": 0, "fail": 0, "skip": 0})
        c[r.status] += 1
    turns = [t for r in ran for t in r.turns]
    llm_turns = [t for t in turns if t.run_status in ("success", "error", "ungrounded")]
    latencies = [t.latency_ms for t in turns if t.latency_ms is not None]
    return {
        "suite": suite["suite"], "version": suite["version"], "provider": provider, "model": model,
        "git_sha": git_sha(), "prompt_fingerprint": prompt_fingerprint(),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "duration_s": round(seconds, 1),
        "totals": {"cases": len(results), "passed": sum(r.status == "pass" for r in results),
                   "failed": sum(r.status == "fail" for r in results), "skipped": sum(r.status == "skip" for r in results),
                   "pass_rate": round(sum(r.status == "pass" for r in ran) / len(ran), 3) if ran else None},
        "critical_failures": [r.id for r in ran if r.critical and r.status == "fail"],
        "by_category": by_cat,
        "metrics": {
            "turns": len(turns), "model_turns": len(llm_turns),
            "ungrounded_turns": sum(t.run_status == "ungrounded" for t in turns),
            "ungrounded_rate": round(sum(t.run_status == "ungrounded" for t in llm_turns) / len(llm_turns), 3)
            if llm_turns else None,
            "agent_errors": sum(t.run_status == "error" for t in turns),
            "latency_ms_p50": statistics.median(latencies) if latencies else None,
            "latency_ms_max": max(latencies) if latencies else None,
        },
        "cases": [{"id": r.id, "category": r.category, "language": r.language, "critical": r.critical,
                   "status": r.status, "failures": r.failures,
                   "transcript": [{"customer": t.customer, "replies": t.replies, "tools": t.tools,
                                   "run_status": t.run_status, "latency_ms": t.latency_ms} for t in r.turns]}
                  for r in results],
    }


def regressions(report: dict, baseline: dict) -> list[str]:
    """Cases that passed in the baseline and do not pass now."""
    now = {c["id"]: c["status"] for c in report["cases"]}
    return [c["id"] for c in baseline["cases"] if c["status"] == "pass" and now.get(c["id"]) != "pass"]


def load_suite(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def run_suite(session_factory, provider: str, suite_path: Path, include_llm_cases: bool | None = None) -> dict:
    suite = load_suite(suite_path)
    include = provider == "openai_compat" if include_llm_cases is None else include_llm_cases
    start = time.perf_counter()
    results = Harness(session_factory, provider).run(suite["cases"], include)
    model = None
    if provider == "openai_compat":
        from app.core.config import settings
        model = settings.llm_model
    return build_report(suite, provider, model, results, time.perf_counter() - start)


__all__ = ["AdversarialProvider", "Harness", "build_report", "regressions", "run_suite"]
