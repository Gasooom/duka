"""Cart totals, delivery, orders (snapshots, stock), state machine. All deterministic."""
import uuid
from decimal import Decimal

import pytest

from app.core.errors import ConflictError, ValidationError
from app.models import Business, Product
from app.services.commerce_service import CartService, DeliveryService, OrderService
from app.services.conversation_service import ConversationService, CustomerService


@pytest.fixture
def shop(fashion, db):
    bid = uuid.UUID(fashion.business_id)
    customer = CustomerService(db, bid).upsert_from_whatsapp("+250 788 123 999", "Ann")
    conv = ConversationService(db, bid).get_or_create_active(customer)
    products = {p.sku: p for p in db.query(Product).filter_by(business_id=bid)}
    return bid, customer, conv, products


def test_delivery_zone_matching(shop, db):
    bid = shop[0]
    d = DeliveryService(db, bid)
    assert d.quote("Remera, near the stadium").zone_name == "Kigali City"
    q = d.quote("Huye")
    assert q.zone_name == "Outside Kigali" and q.fee == Decimal("5000")
    assert d.quote("Nairobi").available is False
    assert d.quote(None).zone_name == "Kigali City"  # default


def test_cart_totals_use_db_prices(shop, db):
    bid, customer, conv, products = shop
    carts = CartService(db, bid)
    cart = carts.get_active(customer, conv)
    carts.add_item(cart, products["KF-SAMBA-BLK"], 1)
    carts.add_item(cart, products["KF-TEE-BLK"], 2)
    t = carts.totals(cart, delivery_location="Huye")
    assert t.subtotal == Decimal("119000")  # 95000 + 2*12000
    assert t.delivery_fee == Decimal("5000") and t.delivery_zone == "Outside Kigali"
    assert t.total == Decimal("124000") and t.currency == "RWF"
    # adding the same product merges quantities
    carts.add_item(cart, products["KF-TEE-BLK"], 1)
    assert carts.totals(cart).lines[1].quantity == 3
    carts.remove_item(cart, products["KF-TEE-BLK"].id)
    assert len(carts.totals(cart).lines) == 1
    carts.clear(cart)
    assert carts.totals(cart).total == 0


def test_cart_rejects_stock_overflow_and_inactive(shop, db):
    bid, customer, conv, products = shop
    carts = CartService(db, bid)
    cart = carts.get_active(customer, conv)
    with pytest.raises(ValidationError, match="in stock"):
        carts.add_item(cart, products["KF-BAG-CNV"], 4)  # stock 3
    products["KF-CAP-BLK"].active = False
    with pytest.raises(ValidationError, match="not available"):
        carts.add_item(cart, products["KF-CAP-BLK"], 1)
    with pytest.raises(ValidationError):
        carts.add_item(cart, products["KF-TEE-BLK"], 0)


def test_order_snapshots_prices_and_decrements_stock(shop, db):
    bid, customer, conv, products = shop
    carts, orders = CartService(db, bid), OrderService(db, bid)
    samba = products["KF-SAMBA-BLK"]
    stock_before = samba.stock_quantity
    carts.add_item(carts.get_active(customer, conv), samba, 2)
    order = orders.create_from_cart(customer, conv, delivery_location="Kicukiro")
    assert order.order_number == "KF-00001" and order.status == "pending"
    assert order.subtotal == Decimal("190000") and order.delivery_fee == Decimal("2000")
    assert order.total == Decimal("192000")
    assert samba.stock_quantity == stock_before - 2
    # Price change after ordering does not affect the order snapshot
    samba.price = Decimal("1")
    db.flush()
    db.refresh(order)
    assert order.items[0].unit_price == Decimal("95000") and order.items[0].product_name == "Adidas Samba OG Black"
    assert order.total == Decimal("192000")
    # cart was converted; a new active cart is empty
    assert carts.get_active(customer, conv).items == []
    # Cancel restocks
    orders.transition(order, "cancelled")
    assert samba.stock_quantity == stock_before


def test_order_requires_items_and_stock(shop, db):
    bid, customer, conv, products = shop
    carts, orders = CartService(db, bid), OrderService(db, bid)
    with pytest.raises(ValidationError, match="empty"):
        orders.create_from_cart(customer, conv)
    bag = products["KF-BAG-CNV"]
    carts.add_item(carts.get_active(customer, conv), bag, 3)
    bag.stock_quantity = 1  # someone else bought them meanwhile
    db.flush()
    with pytest.raises(ConflictError, match="left in stock"):
        orders.create_from_cart(customer, conv)


def test_order_requires_delivery_zone_when_enabled(shop, db):
    bid, customer, conv, products = shop
    carts, orders = CartService(db, bid), OrderService(db, bid)
    carts.add_item(carts.get_active(customer, conv), products["KF-TEE-BLK"], 1)
    with pytest.raises(ValidationError, match="No delivery zone"):
        orders.create_from_cart(customer, conv, delivery_location="Nairobi")
    # pickup-only business: no delivery fee needed
    db.get(Business, bid).delivery_enabled = False
    order = orders.create_from_cart(customer, conv)
    assert order.delivery_fee == 0


def test_order_state_machine(shop, db, fashion):
    bid, customer, conv, products = shop
    carts, orders = CartService(db, bid), OrderService(db, bid)
    carts.add_item(carts.get_active(customer, conv), products["KF-TEE-BLK"], 1)
    order = orders.create_from_cart(customer, conv)
    db.commit()
    with pytest.raises(ValidationError):
        orders.transition(order, "delivered")
    # admins can never mark an order paid
    r = fashion.patch(f"/api/orders/{order.id}", json={"status": "paid"})
    assert r.status_code == 422
    assert fashion.patch(f"/api/orders/{order.id}", json={"status": "processing"}).json()["status"] == "processing"
    for s in ("ready", "out_for_delivery", "delivered"):
        assert fashion.patch(f"/api/orders/{order.id}", json={"status": s}).status_code == 200
    assert fashion.patch(f"/api/orders/{order.id}", json={"status": "cancelled"}).status_code == 422
