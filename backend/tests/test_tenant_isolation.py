"""MANDATORY security test: Business A must never see Business B's data — through the admin API,
the agent tools, the webhook routing, or the repository layer."""
import uuid

import pytest

from app.core.errors import NotFoundError
from app.db.base import Base
from app.models import Business, Customer, Product
from app.repositories.repos import ProductRepo
from app.services.knowledge_service import KnowledgeService
from app.tools.commerce_tools import resolve_product
from app.tools.registry import ToolContext, execute_tool


def _seed_b_data(b, outbox):
    """Give Business B a customer, conversation, order and knowledge doc."""
    b.post("/api/knowledge", json={"title": "Secret policy", "content": "B-only secret: warranty code ZEBRA-42."})
    b.send("I want a Samsung phone under 300k", from_number="250788999000")
    b.send("add it", from_number="250788999000")
    b.send("place the order", from_number="250788999000")
    orders = b.get("/api/orders").json()
    assert len(orders) == 1
    return orders[0]


def test_every_business_owned_table_has_business_id():
    exempt = {"businesses", "alembic_version"}
    for table in Base.metadata.sorted_tables:
        if table.name in exempt:
            continue
        assert "business_id" in table.columns, f"{table.name} is missing business_id"
        assert not table.columns["business_id"].nullable, f"{table.name}.business_id must be NOT NULL"


def test_admin_api_isolation(fashion, electronics, outbox):
    a, b = fashion, electronics
    b_order = _seed_b_data(b, outbox)
    b_product = b.get("/api/products").json()[0]
    b_customer = b.get("/api/customers").json()[0]
    b_conv = b.get("/api/conversations").json()[0]
    b_doc = b.get("/api/knowledge").json()[0]

    # Direct access by id -> 404 (not 403: don't even leak existence)
    assert a.get(f"/api/products/{b_product['id']}").status_code == 404
    assert a.patch(f"/api/products/{b_product['id']}", json={"price": 1}).status_code == 404
    assert a.delete(f"/api/products/{b_product['id']}").status_code == 404
    assert a.post(f"/api/products/{b_product['id']}/stock", json={"change": 5}).status_code == 404
    assert a.get(f"/api/orders/{b_order['id']}").status_code == 404
    assert a.patch(f"/api/orders/{b_order['id']}", json={"status": "cancelled"}).status_code == 404
    assert a.get(f"/api/customers/{b_customer['id']}").status_code == 404
    assert a.get(f"/api/conversations/{b_conv['id']}").status_code == 404
    assert a.post(f"/api/conversations/{b_conv['id']}/handoff").status_code == 404
    assert a.delete(f"/api/knowledge/{b_doc['id']}").status_code == 404

    # Lists only ever contain own rows
    b_names = {p["name"] for p in b.get("/api/products").json()}
    assert not b_names & {p["name"] for p in a.get("/api/products").json()}
    assert a.get("/api/orders").json() == []
    assert a.get("/api/customers").json() == []
    assert a.get("/api/conversations").json() == []
    assert a.get("/api/knowledge").json() == []
    assert a.get("/api/knowledge/search", params={"q": "warranty code"}).json() == []
    assert all(r["product"]["name"] not in b_names for r in
               a.get("/api/products/search", params={"q": "Samsung phone"}).json())

    # B's data unchanged after A's attempts
    assert b.get(f"/api/products/{b_product['id']}").json()["price"] == b_product["price"]
    assert b.get(f"/api/orders/{b_order['id']}").json()["status"] == b_order["status"]


def test_whatsapp_number_maps_to_exactly_one_business(fashion, electronics, client):
    # Electronics cannot claim Fashion's phone_number_id
    r = electronics.post("/api/whatsapp/accounts", json={"phone_number_id": "pnid-fashion", "mode": "dev"})
    assert r.status_code == 409


def test_same_customer_number_is_separate_per_business(fashion, electronics, outbox):
    fashion.send("black sneakers", from_number="250788000777")
    electronics.send("samsung phone", from_number="250788000777")
    fa, el = fashion.get("/api/customers").json(), electronics.get("/api/customers").json()
    assert len(fa) == len(el) == 1
    assert fa[0]["id"] != el[0]["id"]
    # Each customer only got replies from their own catalog
    replies = [body for _, body in outbox.sent]
    assert "Sneakers" not in replies[1] and "Samsung" not in replies[0]


def test_agent_tools_cannot_reach_other_tenant(fashion, electronics, db, outbox):
    fashion.send("hello there friend", from_number="250788000555")
    a_biz = db.get(Business, uuid.UUID(fashion.business_id))
    customer = db.query(Customer).filter_by(business_id=a_biz.id).one()
    from app.services.conversation_service import ConversationService
    conv = ConversationService(db, a_biz.id).get_or_create_active(customer)
    ctx = ToolContext(db=db, business=a_biz, customer=customer, conversation=conv)
    b_product = db.query(Product).filter_by(business_id=uuid.UUID(electronics.business_id)).first()

    # Guessing another tenant's product UUID or SKU fails
    with pytest.raises(NotFoundError):
        resolve_product(ctx, str(b_product.id))
    with pytest.raises(NotFoundError):
        resolve_product(ctx, b_product.sku)
    result, _ = execute_tool(ctx, "add_to_cart", {"product_ref": str(b_product.id)})
    assert result["ok"] is False
    result, _ = execute_tool(ctx, "search_products", {"query": "Samsung Galaxy phone"})
    assert result["count"] == 0
    result, _ = execute_tool(ctx, "check_order_status", {"order_number": "ME-00001"})
    assert result["ok"] is False


def test_repository_ignores_spoofed_business_id(fashion, electronics, db):
    a_id, b_id = uuid.UUID(fashion.business_id), uuid.UUID(electronics.business_id)
    repo = ProductRepo(db, a_id)
    p = repo.add(name="X", price=1, currency="RWF", sku="SPOOF-1", business_id=b_id)
    assert p.business_id == a_id
    b_product = db.query(Product).filter_by(business_id=b_id).first()
    assert repo.get(b_product.id) is None
    with pytest.raises(NotFoundError):
        repo.update(b_product, price=0)


def test_knowledge_search_is_scoped(fashion, electronics, db):
    KnowledgeService(db, uuid.UUID(electronics.business_id)).add_document("W", "Warranty lasts 12 months.")
    db.commit()
    assert KnowledgeService(db, uuid.UUID(fashion.business_id)).search("warranty months") == []
    assert KnowledgeService(db, uuid.UUID(electronics.business_id)).search("warranty months")


def test_token_from_one_business_cannot_be_reused_with_tampered_claims(fashion, electronics, client):
    import jwt
    claims = jwt.decode(fashion.token, options={"verify_signature": False})
    claims["bid"] = electronics.business_id
    forged = jwt.encode(claims, "wrong-secret", algorithm="HS256")
    assert client.get("/api/products", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    # Even with a valid signature, a user whose business doesn't match the claim is rejected
    from app.core.config import settings
    signed = jwt.encode(claims, settings.jwt_secret, algorithm="HS256")
    assert client.get("/api/products", headers={"Authorization": f"Bearer {signed}"}).status_code == 401
