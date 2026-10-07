"""MANDATORY security test: Business A must never read, modify or infer Business B's data — through the
admin API (every id-bearing route), the agent tools, the webhook routing, the repository layer, the database
itself, or concurrent traffic. New id routes and new tenant->tenant foreign keys fail the meta-tests below
until they are covered."""
import threading
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.errors import NotFoundError
from app.core.security import create_access_token
from app.db.base import Base
from app.db.session import SessionLocal
from app.integrations.whatsapp.parser import build_text_webhook
from app.main import app
from app.models import Business, Customer, Message, Order, OrderItem, Product, User
from app.repositories.repos import CartItemRepo, CartRepo, MessageRepo, ProductRepo
from app.services.conversation_service import ConversationService, CustomerService
from app.services.knowledge_service import KnowledgeService
from app.tools.commerce_tools import resolve_product
from app.tools.registry import ToolContext, execute_tool
from app.workflows.inbound import process_webhook_payload

B_CUSTOMER = "250788999000"


def _seed_b_data(b):
    """Give Business B a customer, conversation, confirmed order, reported (pending) payment, owner
    notifications, audit events and a knowledge doc."""
    b.post("/api/knowledge", json={"title": "Secret policy", "content": "B-only secret: warranty code ZEBRA-42."})
    b.patch("/api/business/settings", json={"owner_notification_phone": "250788000444"})
    for t in ("I want a Samsung phone under 300k", "add it", "deliver to Remera, KG 11 Ave", "yes",
              "I paid, transaction id TXB-0001"):
        b.send(t, from_number=B_CUSTOMER)
    orders = b.get("/api/orders").json()
    assert len(orders) == 1
    return orders[0]


def _b_ids(b) -> dict[str, str]:
    order = b.get("/api/orders").json()[0]
    payments = b.get(f"/api/orders/{order['id']}").json()["payments"]
    assert payments, "B needs a payment for the IDOR matrix"
    return {
        "product_id": b.get("/api/products").json()[0]["id"],
        "order_id": order["id"],
        "payment_id": payments[0]["id"],
        "customer_id": b.get("/api/customers").json()[0]["id"],
        "conversation_id": b.get("/api/conversations").json()[0]["id"],
        "zone_id": b.get("/api/delivery-zones").json()[0]["id"],
        "account_id": b.get("/api/whatsapp/accounts").json()[0]["id"],
        "doc_id": b.get("/api/knowledge").json()[0]["id"],
    }


def _snapshot(t) -> dict:
    """Everything a tenant can see about itself, for before/after comparison."""
    snap = {p: t.get(p).json() for p in (
        "/api/business", "/api/business/agent-config", "/api/business/settings", "/api/delivery-zones",
        "/api/whatsapp/accounts", "/api/products", "/api/categories", "/api/orders", "/api/customers",
        "/api/conversations", "/api/knowledge", "/api/dashboard/stats", "/api/dashboard/usage",
        "/api/dashboard/notifications", "/api/dashboard/audit")}
    snap["orders_detail"] = [t.get(f"/api/orders/{o['id']}").json() for o in snap["/api/orders"]]
    snap["conversations_detail"] = [t.get(f"/api/conversations/{c['id']}").json() for c in snap["/api/conversations"]]
    snap["inventory"] = [t.get(f"/api/products/{p['id']}/inventory").json() for p in snap["/api/products"]]
    return snap


# (method, path, json body). Bodies are valid so requests reach the ownership check instead of failing validation.
IDOR_MATRIX = [
    ("GET", "/api/products/{product_id}", None),
    ("PATCH", "/api/products/{product_id}", {"price": 1, "active": False}),
    ("DELETE", "/api/products/{product_id}", None),
    ("POST", "/api/products/{product_id}/stock", {"change": 5}),
    ("GET", "/api/products/{product_id}/inventory", None),
    ("GET", "/api/orders/{order_id}", None),
    ("PATCH", "/api/orders/{order_id}", {"status": "cancelled", "reason": "hijack"}),
    ("POST", "/api/orders/{order_id}/payments", {"method": "cash", "note": "fake payment from A"}),
    ("POST", "/api/payments/{payment_id}/void", {"reason": "hijack"}),
    ("POST", "/api/payments/{payment_id}/refresh", None),
    ("POST", "/api/payments/{payment_id}/simulate", {"status": "successful"}),
    ("GET", "/api/customers/{customer_id}", None),
    ("GET", "/api/conversations/{conversation_id}", None),
    ("POST", "/api/conversations/{conversation_id}/reply", {"text": "hello from A"}),
    ("POST", "/api/conversations/{conversation_id}/handoff", None),
    ("POST", "/api/conversations/{conversation_id}/return-to-ai", None),
    ("PATCH", "/api/delivery-zones/{zone_id}", {"name": "Hijacked", "fee": 0, "is_default": True}),
    ("DELETE", "/api/delivery-zones/{zone_id}", None),
    ("DELETE", "/api/whatsapp/accounts/{account_id}", None),
    ("DELETE", "/api/knowledge/{doc_id}", None),
]


def test_idor_matrix_covers_every_id_route():
    routes = {(m.upper(), path) for path, ops in app.openapi()["paths"].items()
              if path.startswith("/api/") and "{" in path for m in ops}
    assert routes == {(m, p) for m, p, _ in IDOR_MATRIX}, "add new id routes to IDOR_MATRIX"


def test_cross_tenant_idor_matrix(fashion, electronics, outbox):
    a, b = fashion, electronics
    _seed_b_data(b)
    ids = _b_ids(b)
    b.post(f"/api/conversations/{ids['conversation_id']}/handoff")  # human mode: 'reply' would otherwise be allowed
    before = _snapshot(b)
    sent_before = len(outbox.sent)

    for method, path, body in IDOR_MATRIX:
        target = path.format(**ids)
        r = a.client.request(method, target, headers=a.h, json=body)
        assert r.status_code == 404, f"{method} {target} -> {r.status_code} {r.text}"
        # Same answer as for an id that doesn't exist at all: A cannot even infer that B's id exists.
        random_target = path.format(**{k: str(uuid.uuid4()) for k in ids})
        r2 = a.client.request(method, random_target, headers=a.h, json=body)
        assert (r2.status_code, r2.json()) == (r.status_code, r.json()), f"existence oracle on {method} {path}"

    assert _snapshot(b) == before
    assert len(outbox.sent) == sent_before  # nothing was sent to B's customer


def test_lists_searches_and_aggregates_only_show_own_rows(fashion, electronics, outbox):
    a, b = fashion, electronics
    _seed_b_data(b)
    b_snap = _snapshot(b)
    a_snap = _snapshot(a)
    for path in ("/api/orders", "/api/customers", "/api/conversations", "/api/knowledge",
                 "/api/dashboard/notifications"):
        assert a_snap[path] == [], path
    # A's audit trail holds exactly its own setup (connecting its WhatsApp number, by its owner) and none of B's rows.
    a_audit, b_audit = a_snap["/api/dashboard/audit"], b_snap["/api/dashboard/audit"]
    assert [(e["action"], e["data"]["phone_number_id"]) for e in a_audit] == [("whatsapp.connected", a.phone_number_id)]
    assert {e["actor_user_id"] for e in a_audit} == {a.get("/api/auth/me").json()["user"]["id"]}
    assert not {e["id"] for e in a_audit} & {e["id"] for e in b_audit}
    assert b_snap["/api/dashboard/notifications"] and b_audit
    for path in ("/api/products", "/api/categories", "/api/delivery-zones", "/api/whatsapp/accounts"):
        assert not {x["id"] for x in a_snap[path]} & {x["id"] for x in b_snap[path]}, path
    stats, usage = a_snap["/api/dashboard/stats"], a_snap["/api/dashboard/usage"]
    assert stats["orders_total"] == stats["customers"] == stats["messages"] == stats["pending_payments"] == 0
    assert stats["revenue"] == 0 and stats["recent_orders"] == []
    assert usage["agent_runs"] == usage["messages_in"] == usage["messages_out"] == 0
    b_usage = b_snap["/api/dashboard/usage"]
    assert b_usage["agent_runs"] >= 5 and b_usage["messages_in"] == 5

    b_names = {p["name"] for p in b_snap["/api/products"]}
    assert a.get("/api/knowledge/search", params={"q": "warranty code ZEBRA"}).json() == []
    assert all(h["product"]["name"] not in b_names for h in
               a.get("/api/products/search", params={"q": "Samsung phone"}).json())
    assert a.get("/api/products", params={"q": "Samsung"}).json() == []
    assert a.get("/api/customers", params={"q": B_CUSTOMER[-6:]}).json() == []


def test_tenant_singletons_are_isolated(fashion, electronics):
    a, b = fashion, electronics
    before = _snapshot(b)
    assert a.patch("/api/business", json={"name": "A renamed", "order_prefix": "ZZ"}).status_code == 200
    assert a.patch("/api/business/agent-config", json={"tone": "rude", "business_rules": "free stuff"}).status_code == 200
    assert a.patch("/api/business/settings", json={"max_order_quantity": 999}).status_code == 200
    # A new default zone in A must not un-default B's zone.
    a.zone("A default", 1, ["Somewhere"], True)
    assert _snapshot(b) == before


def test_csv_sku_collision_creates_own_product_and_leaves_other_tenant_alone(fashion, electronics):
    a, b = fashion, electronics
    b_product = b.get("/api/products").json()[0]
    r = a.import_csv(f"name,price,sku,stock_quantity\nA copy,1,{b_product['sku']},3\n")
    assert r.json()["created"] == 1 and r.json()["updated"] == 0
    assert b.get(f"/api/products/{b_product['id']}").json() == b_product


def _tenant_row_counts(db, business_id) -> dict[str, int]:
    out = {}
    for table in Base.metadata.sorted_tables:
        if "business_id" in table.columns:
            out[table.name] = db.scalar(select(func.count()).select_from(table)
                                        .where(table.c.business_id == business_id))
    return out


def test_inbound_webhook_only_writes_to_the_owning_tenant(fashion, electronics, outbox, db):
    b_id = uuid.UUID(electronics.business_id)
    before = _tenant_row_counts(db, b_id)
    for t in ("black sneakers", "add 1", "place the order", "pay", "talk to a human"):
        fashion.send(t, from_number="250788123123")
    db.expire_all()
    assert _tenant_row_counts(db, b_id) == before
    assert db.scalar(select(func.count()).select_from(Customer).where(Customer.business_id == b_id)) == 0


def test_status_webhook_cannot_modify_other_tenants_message(fashion, electronics, outbox, db, client):
    electronics.send("samsung phone", from_number=B_CUSTOMER)
    b_out = db.scalars(select(Message).where(Message.business_id == uuid.UUID(electronics.business_id),
                                             Message.role == "assistant")).one()
    payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": fashion.phone_number_id},  # A's number, B's message id
        "statuses": [{"id": b_out.wa_message_id, "status": "failed", "recipient_id": B_CUSTOMER}]}}]}]}
    assert client.post("/webhooks/whatsapp", json=payload).status_code == 200
    db.refresh(b_out)
    assert b_out.delivery_status == "sent"


def test_customer_chat_cannot_reach_other_store(fashion, electronics, outbox):
    b_order = _seed_b_data(electronics)
    b_product = electronics.get("/api/products").json()[0]
    secrets_of_b = [b_order["order_number"], b_product["name"], "ZEBRA-42"]
    n = len(outbox.sent)
    for t in (f"what is the status of order {b_order['order_number']}", f"add {b_product['sku']}",
              "what is your warranty code policy?", f"pay {b_order['order_number']}"):
        fashion.send(t, from_number=B_CUSTOMER)  # same person, but talking to store A
    replies = [body for _, body in outbox.sent[n:]]
    assert len(replies) == 4
    for reply in replies:
        for s in secrets_of_b[1:]:
            assert s not in reply
    assert "not found" in replies[0].lower()


def _a_tool_ctx(fashion, db):
    fashion.send("hello there friend", from_number="250788000555")
    a_biz = db.get(Business, uuid.UUID(fashion.business_id))
    customer = db.scalars(select(Customer).where(Customer.business_id == a_biz.id)).one()
    conv = ConversationService(db, a_biz.id).get_or_create_active(customer)
    return ToolContext(db=db, business=a_biz, customer=customer, conversation=conv)


def test_agent_tools_cannot_reach_other_tenant(fashion, electronics, db, outbox):
    b_order = _seed_b_data(electronics)
    ctx = _a_tool_ctx(fashion, db)
    b_product = db.scalars(select(Product).where(Product.business_id == uuid.UUID(electronics.business_id))).first()

    with pytest.raises(NotFoundError):
        resolve_product(ctx, str(b_product.id))
    with pytest.raises(NotFoundError):
        resolve_product(ctx, b_product.sku)
    with pytest.raises(NotFoundError):
        resolve_product(ctx, b_product.name)
    for name, args in [
        ("add_to_cart", {"product_ref": str(b_product.id)}),
        ("get_product", {"product_ref": b_product.sku}),
        ("check_inventory", {"product_ref": str(b_product.id)}),
        ("remove_from_cart", {"product_ref": str(b_product.id)}),
        ("get_order", {"order_number": b_order["order_number"]}),
        ("check_order_status", {"order_number": b_order["order_number"]}),
        ("initiate_payment", {"order_number": b_order["order_number"]}),
        ("submit_payment_reference", {"order_number": b_order["order_number"], "reference": "TX-FROM-A"}),
    ]:
        result, _ = execute_tool(ctx, name, args)
        assert result["ok"] is False, (name, result)
    result, _ = execute_tool(ctx, "search_products", {"query": "Samsung Galaxy phone"})
    assert result["count"] == 0
    result, _ = execute_tool(ctx, "search_knowledge", {"query": "warranty code ZEBRA"})
    assert result["results"] == []
    result, _ = execute_tool(ctx, "get_customer_orders", {})
    assert result["orders"] == []
    info, _ = execute_tool(ctx, "get_business_information", {})
    assert info["name"] == "Kigali Fashion" and all(z["name"] != "Kigali" for z in info["delivery_zones"])


def test_repository_ignores_spoofed_business_id(fashion, electronics, db):
    a_id, b_id = uuid.UUID(fashion.business_id), uuid.UUID(electronics.business_id)
    repo = ProductRepo(db, a_id)
    p = repo.add(name="X", price=1, currency="RWF", sku="SPOOF-1", business_id=b_id)
    assert p.business_id == a_id
    b_product = db.scalars(select(Product).where(Product.business_id == b_id)).first()
    assert repo.get(b_product.id) is None
    with pytest.raises(NotFoundError):
        repo.update(b_product, price=0)
    with pytest.raises(NotFoundError):
        repo.delete(b_product)


def test_database_rejects_cross_tenant_references(fashion, electronics, db, outbox):
    """Even a buggy service that skips its ownership check cannot link A's rows to B's rows."""
    a_id, b_id = uuid.UUID(fashion.business_id), uuid.UUID(electronics.business_id)
    electronics.send("samsung phone", from_number=B_CUSTOMER)
    b_product = db.scalars(select(Product).where(Product.business_id == b_id)).first()
    b_conv_id = db.scalar(text("SELECT id FROM conversations WHERE business_id = :b"), {"b": b_id})

    a_customer = CustomerService(db, a_id).upsert_from_whatsapp("250788444555")
    a_cart = CartRepo(db, a_id).add(customer_id=a_customer.id)
    db.commit()
    with pytest.raises(IntegrityError, match="cross-tenant"):
        CartItemRepo(db, a_id).add(cart_id=a_cart.id, product_id=b_product.id, quantity=1)
    db.rollback()
    with pytest.raises(IntegrityError, match="cross-tenant"):
        MessageRepo(db, a_id).add(conversation_id=b_conv_id, role="customer", content="injected")
    db.rollback()
    with pytest.raises(IntegrityError, match="cross-tenant"):
        CartRepo(db, a_id).add(customer_id=a_customer.id, conversation_id=b_conv_id)
    db.rollback()
    # Moving a row to another tenant is impossible.
    with pytest.raises(IntegrityError, match="immutable"):
        db.execute(text("UPDATE customers SET business_id = :a WHERE business_id = :b"), {"a": a_id, "b": b_id})
    db.rollback()
    with pytest.raises(IntegrityError):
        db.execute(text("UPDATE products SET business_id = :a WHERE id = :p"), {"a": a_id, "p": b_product.id})
    db.rollback()


def _trigger_args(db, prefix: str) -> dict[str, list[str]]:
    rows = db.execute(text("SELECT c.relname, t.tgargs FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                           "WHERE t.tgname LIKE :p"), {"p": f"{prefix}%"}).all()
    return {name: [a for a in bytes(args).decode().split("\x00") if a] for name, args in rows}


def test_every_tenant_foreign_key_is_guarded_in_the_database(db):
    guarded = _trigger_args(db, "tenant_fk_")
    immutable = _trigger_args(db, "tenant_immutable_")
    for table in Base.metadata.sorted_tables:
        if "business_id" not in table.columns:
            continue
        assert table.name in immutable, f"{table.name}: business_id must be immutable"
        pairs = list(zip(guarded.get(table.name, [])[::2], guarded.get(table.name, [])[1::2]))
        for fk in table.foreign_keys:
            parent = fk.column.table
            if parent.name != "businesses" and "business_id" in parent.columns:
                assert (fk.parent.name, parent.name) in pairs, \
                    f"{table.name}.{fk.parent.name} -> {parent.name} has no same-tenant trigger (add it in a migration)"


def test_every_business_owned_table_has_business_id():
    exempt = {"businesses", "alembic_version"}
    for table in Base.metadata.sorted_tables:
        if table.name in exempt:
            continue
        assert "business_id" in table.columns, f"{table.name} is missing business_id"
        assert not table.columns["business_id"].nullable, f"{table.name}.business_id must be NOT NULL"


def test_knowledge_search_is_scoped(fashion, electronics, db):
    KnowledgeService(db, uuid.UUID(electronics.business_id)).add_document("W", "Warranty lasts 12 months.")
    db.commit()
    assert KnowledgeService(db, uuid.UUID(fashion.business_id)).search("warranty months") == []
    assert KnowledgeService(db, uuid.UUID(electronics.business_id)).search("warranty months")


# ---------------------------------------------------------------- identity
def test_whatsapp_number_maps_to_exactly_one_business(fashion, electronics):
    r = electronics.post("/api/whatsapp/accounts", json={"phone_number_id": "pnid-fashion", "mode": "dev"})
    assert r.status_code == 409
    assert fashion.get("/api/whatsapp/accounts").json()[0]["phone_number_id"] == "pnid-fashion"


def test_same_customer_number_is_separate_per_business(fashion, electronics, outbox):
    fashion.send("black sneakers", from_number="250788000777")
    electronics.send("samsung phone", from_number="250788000777")
    fa, el = fashion.get("/api/customers").json(), electronics.get("/api/customers").json()
    assert len(fa) == len(el) == 1
    assert fa[0]["id"] != el[0]["id"]
    replies = [body for _, body in outbox.sent]
    assert "Sneakers" not in replies[1] and "Samsung" not in replies[0]


def test_forged_and_stale_tokens_are_rejected(fashion, electronics, client, db):
    claims = jwt.decode(fashion.token, options={"verify_signature": False})
    claims["bid"] = electronics.business_id
    forged = jwt.encode(claims, "wrong-secret-0123456789abcdef0123456789", algorithm="HS256")
    assert client.get("/api/products", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    # Valid signature, but the user does not belong to the claimed business.
    signed = jwt.encode(claims, settings.jwt_secret, algorithm="HS256")
    assert client.get("/api/products", headers={"Authorization": f"Bearer {signed}"}).status_code == 401
    # Unsigned token.
    none_tok = jwt.encode({**claims, "bid": fashion.business_id}, None, algorithm="none")
    assert client.get("/api/products", headers={"Authorization": f"Bearer {none_tok}"}).status_code == 401
    # Expired token.
    expired = jwt.encode({**jwt.decode(fashion.token, options={"verify_signature": False}),
                          "exp": datetime.now(timezone.utc) - timedelta(minutes=1)}, settings.jwt_secret,
                         algorithm="HS256")
    assert client.get("/api/products", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
    # Deactivated user / deactivated business.
    user = db.scalars(select(User).where(User.business_id == uuid.UUID(fashion.business_id))).one()
    user.is_active = False
    db.commit()
    assert fashion.get("/api/products").status_code == 401
    user.is_active = True
    db.get(Business, uuid.UUID(fashion.business_id)).is_active = False
    db.commit()
    assert fashion.get("/api/products").status_code == 403


def test_staff_cannot_change_owner_settings(fashion, db, client):
    from app.core.security import hash_password
    staff = User(business_id=uuid.UUID(fashion.business_id), email=f"staff-{uuid.uuid4().hex[:6]}@test.dev",
                 password_hash=hash_password("password123"), role="staff")
    db.add(staff)
    db.commit()
    h = {"Authorization": f"Bearer {create_access_token(staff.id, staff.business_id, 'staff')}"}
    for method, path, body in [
        ("PATCH", "/api/business", {"name": "x"}),
        ("PATCH", "/api/business/agent-config", {"tone": "x"}),
        ("PATCH", "/api/business/settings", {"max_order_quantity": 1}),
        ("POST", "/api/delivery-zones", {"name": "x", "fee": 0}),
        ("POST", "/api/whatsapp/accounts", {"phone_number_id": "staff-pnid", "mode": "dev"}),
    ]:
        assert client.request(method, path, headers=h, json=body).status_code == 403, path
    assert client.get("/api/orders", headers=h).status_code == 200


# ---------------------------------------------------------------- tenant creation
def test_public_registration_is_closed_in_production(client, monkeypatch):
    body = {"business_name": "Intruder Shop", "email": "intruder@test.dev", "password": "password123"}
    monkeypatch.setattr(settings, "app_env", "production")
    assert client.post("/api/auth/register", json=body).status_code == 403
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "allow_public_registration", False)
    assert client.post("/api/auth/register", json=body).status_code == 403


def test_operator_cli_onboards_a_business(client, capsys):
    from app.cli import main
    email = f"owner-{uuid.uuid4().hex[:6]}@test.dev"
    assert main(["create-business", "--name", "Pilot Shop", "--email", email]) == 0
    password = capsys.readouterr().out.split("shown once): ")[1].strip()
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200
    assert main(["create-business", "--name", "Again", "--email", email, "--password", "password123"]) == 1


# ---------------------------------------------------------------- concurrency
def test_concurrent_traffic_across_tenants_stays_isolated(fashion, electronics, outbox, db):
    """Customers of both stores shop at the same time; every row, order number and reply stays in its tenant."""
    plans = [(fashion.phone_number_id, f"25078810000{i}", "black sneakers") for i in range(4)] + \
            [(electronics.phone_number_id, f"25078820000{i}", "samsung phone") for i in range(4)]
    errors: list[BaseException] = []

    def shop(pnid, number, query):
        try:
            for t in (query, "add 1", "deliver to Remera, KG 11 Ave", "yes"):
                payload = build_text_webhook(pnid, "+250700", number, t, f"wamid.{uuid.uuid4().hex}")
                assert [r.status for r in process_webhook_payload(payload, SessionLocal)] == ["replied"]
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=shop, args=p) for p in plans]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors

    a_id, b_id = uuid.UUID(fashion.business_id), uuid.UUID(electronics.business_id)
    for bid, prefix in ((a_id, "KF"), (b_id, "ME")):
        numbers = sorted(db.scalars(select(Order.order_number).where(Order.business_id == bid)))
        assert numbers == [f"{prefix}-0000{i}" for i in range(1, 5)]
        # Every ordered product belongs to the ordering tenant.
        foreign = db.scalar(select(func.count()).select_from(OrderItem).join(Product, Product.id == OrderItem.product_id)
                            .where(OrderItem.business_id == bid, Product.business_id != bid))
        assert foreign == 0
    a_names = set(db.scalars(select(Product.name).where(Product.business_id == a_id)))
    b_names = set(db.scalars(select(Product.name).where(Product.business_id == b_id)))
    for to, body in outbox.sent:
        own, other = (a_names, b_names) if to.startswith("2507881") else (b_names, a_names)
        assert not any(n in body for n in other - own), (to, body)
