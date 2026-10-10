"""Cart totals, delivery, explicit checkout confirmation, orders (snapshots, stock), state machine."""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.errors import ConflictError, ValidationError
from app.models import Business, Message, Product
from app.services.commerce_service import CartService, CheckoutChanged, CheckoutService, DeliveryService, OrderService
from app.services.conversation_service import ConversationService, CustomerService
from tests.conftest import ADDRESS


@pytest.fixture
def shop(fashion, db):
    bid = uuid.UUID(fashion.business_id)
    customer = CustomerService(db, bid).upsert_from_whatsapp("+250 788 123 999", "Ann")
    conv = ConversationService(db, bid).get_or_create_active(customer)
    products = {p.sku: p for p in db.query(Product).filter_by(business_id=bid)}
    return bid, customer, conv, products


def _confirm(db, bid, customer, conv, *, delivery_status="sent", later=True):
    """Simulate: summary delivered to the customer, then the customer's YES message arrives."""
    checkout = CheckoutService(db, bid)
    convs = ConversationService(db, bid)
    summary = convs.add_message(conv, "assistant", "summary", delivery_status=delivery_status)
    cart = checkout.pending(customer)
    checkout.attach_summary_message(cart.id, summary.id)
    db.flush()
    yes = convs.add_message(conv, "customer", "yes")
    if not later:
        yes.created_at = summary.created_at
    db.flush()
    return checkout.confirm(customer, conv, yes)


def test_delivery_zone_matching_never_assumes_a_zone(shop, db):
    bid = shop[0]
    d = DeliveryService(db, bid)
    assert d.quote("Remera, near the stadium").zone_name == "Kigali City"
    q = d.quote("Huye")
    assert q.zone_name == "Outside Kigali" and q.fee == Decimal("5000")
    assert d.quote("Nairobi").available is False
    assert d.quote(None).available is False  # no default zone is ever assumed


def test_cart_totals_use_db_prices(shop, db):
    bid, customer, conv, products = shop
    carts = CartService(db, bid)
    cart = carts.get_active(customer, conv)
    carts.add_item(cart, products["KF-SAMBA-BLK"], 1)
    carts.add_item(cart, products["KF-TEE-BLK"], 2)
    t = carts.totals(cart)
    assert t.delivery_pending is True and t.delivery_fee == 0 and t.total == Decimal("119000")
    t = carts.totals(cart, delivery_location="Huye")
    assert t.subtotal == Decimal("119000")  # 95000 + 2*12000
    assert t.delivery_fee == Decimal("5000") and t.delivery_zone == "Outside Kigali" and not t.delivery_pending
    assert t.total == Decimal("124000") and t.currency == "RWF"
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


def test_checkout_requires_a_real_address_in_a_zone(shop, db):
    bid, customer, conv, products = shop
    carts, checkout = CartService(db, bid), CheckoutService(db, bid)
    with pytest.raises(ValidationError, match="empty"):
        checkout.prepare(customer, conv, delivery_address=ADDRESS)
    carts.add_item(carts.get_active(customer, conv), products["KF-TEE-BLK"], 1)
    with pytest.raises(ValidationError, match="delivery address"):
        checkout.prepare(customer, conv)
    with pytest.raises(ValidationError, match="No delivery zone"):
        checkout.prepare(customer, conv, delivery_address="Nairobi CBD")
    summary = checkout.prepare(customer, conv, delivery_address=ADDRESS)
    assert "Total: RWF 14,000" in summary.text and "Deliver to: Remera, KG 11 Ave" in summary.text
    assert "Delivery (Kigali City): RWF 2,000" in summary.text and "Reply YES" in summary.text
    # pickup-only business: no address, no fee
    db.get(Business, bid).delivery_enabled = False
    summary = checkout.prepare(customer, conv)
    assert summary.totals.delivery_fee == 0 and "Pickup at the shop" in summary.text


def test_order_requires_confirmation_of_a_delivered_unchanged_summary(shop, db):
    bid, customer, conv, products = shop
    carts, checkout = CartService(db, bid), CheckoutService(db, bid)
    cart = carts.get_active(customer, conv)
    carts.add_item(cart, products["KF-SAMBA-BLK"], 1)
    yes = ConversationService(db, bid).add_message(conv, "customer", "yes")
    with pytest.raises(ValidationError, match="no order summary"):
        checkout.confirm(customer, conv, yes)  # nothing prepared
    checkout.prepare(customer, conv, delivery_address=ADDRESS)
    with pytest.raises(ValidationError, match="not delivered"):
        _confirm(db, bid, customer, conv, delivery_status="failed")
    with pytest.raises(ValidationError, match="not delivered"):
        _confirm(db, bid, customer, conv, delivery_status="queued")
    with pytest.raises(ValidationError, match="not delivered"):
        _confirm(db, bid, customer, conv, later=False)  # the YES must come after the summary
    # Price changed after the summary was shown -> no order, the customer must see a new summary.
    products["KF-SAMBA-BLK"].price = Decimal("99000")
    db.flush()
    with pytest.raises(CheckoutChanged):
        _confirm(db, bid, customer, conv)
    assert checkout.pending(customer) is None
    checkout.prepare(customer, conv, delivery_address=ADDRESS)
    cart.checkout = {**cart.checkout, "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
    with pytest.raises(CheckoutChanged, match="expired"):
        _confirm(db, bid, customer, conv)
    checkout.prepare(customer, conv, delivery_address=ADDRESS)
    order = _confirm(db, bid, customer, conv)
    assert order.total == Decimal("101000") and order.confirmation_message_id is not None and order.confirmed_at
    assert db.get(Message, order.confirmation_message_id).content == "yes"


def test_order_snapshots_prices_and_decrements_stock(shop, db):
    bid, customer, conv, products = shop
    carts, orders, checkout = CartService(db, bid), OrderService(db, bid), CheckoutService(db, bid)
    samba = products["KF-SAMBA-BLK"]
    stock_before = samba.stock_quantity
    carts.add_item(carts.get_active(customer, conv), samba, 2)
    checkout.prepare(customer, conv, delivery_address="Kicukiro, KK 15 Rd")
    order = _confirm(db, bid, customer, conv)
    assert order.order_number == "KF-00001" and order.status == "pending" and order.payment_status == "unpaid"
    assert order.subtotal == Decimal("190000") and order.delivery_fee == Decimal("2000")
    assert order.total == Decimal("192000") and order.delivery_address == "Kicukiro, KK 15 Rd"
    assert samba.stock_quantity == stock_before - 2
    samba.price = Decimal("1")
    db.flush()
    db.refresh(order)
    assert order.items[0].unit_price == Decimal("95000") and order.items[0].product_name == "Adidas Samba OG Black"
    assert order.total == Decimal("192000")
    assert carts.get_active(customer, conv).items == []
    orders.transition(order, "cancelled", reason="customer changed their mind")
    assert samba.stock_quantity == stock_before and order.cancel_reason == "customer changed their mind"


def test_order_requires_stock_at_confirmation(shop, db):
    bid, customer, conv, products = shop
    carts, checkout = CartService(db, bid), CheckoutService(db, bid)
    bag = products["KF-BAG-CNV"]
    carts.add_item(carts.get_active(customer, conv), bag, 3)
    checkout.prepare(customer, conv, delivery_address=ADDRESS)
    bag.stock_quantity = 1  # someone else bought them meanwhile
    db.flush()
    with pytest.raises(CheckoutChanged):
        _confirm(db, bid, customer, conv)


def test_create_from_cart_never_assumes_delivery(shop, db):
    bid, customer, conv, products = shop
    carts, orders = CartService(db, bid), OrderService(db, bid)
    carts.add_item(carts.get_active(customer, conv), products["KF-TEE-BLK"], 1)
    with pytest.raises(ValidationError, match="delivery address"):
        orders.create_from_cart(customer, conv)
    with pytest.raises(ValidationError, match="empty"):
        orders.create_from_cart(CustomerService(db, bid).upsert_from_whatsapp("250788000999"), None)


def test_concurrent_stock_race_is_a_conflict(shop, db):
    bid, customer, conv, products = shop
    carts, orders = CartService(db, bid), OrderService(db, bid)
    bag = products["KF-BAG-CNV"]
    carts.add_item(carts.get_active(customer, conv), bag, 3)
    CheckoutService(db, bid).prepare(customer, conv, delivery_address=ADDRESS)
    bag.stock_quantity = 2
    db.flush()
    with pytest.raises(ConflictError, match="left in stock"):
        orders.create_from_cart(customer, conv)


def test_order_state_machine(shop, db, fashion):
    bid, customer, conv, products = shop
    carts = CartService(db, bid)
    carts.add_item(carts.get_active(customer, conv), products["KF-TEE-BLK"], 1)
    CheckoutService(db, bid).prepare(customer, conv, delivery_address=ADDRESS)
    order = _confirm(db, bid, customer, conv)
    db.commit()
    with pytest.raises(ValidationError):
        OrderService(db, bid).transition(order, "delivered")  # must be accepted first
    db.rollback()  # as a request does after an error: the check holds the order's row lock until then
    for forbidden in ("paid", "awaiting_payment", "processing"):  # payment is not an order status
        assert fashion.patch(f"/api/orders/{order.id}", json={"status": forbidden}).status_code == 422
    r = fashion.patch(f"/api/orders/{order.id}", json={"status": "accepted"}).json()
    assert r["status"] == "accepted" and r["accepted_at"] and r["payment_status"] == "unpaid"
    for s in ("ready", "out_for_delivery", "delivered"):
        assert fashion.patch(f"/api/orders/{order.id}", json={"status": s}).status_code == 200
    assert fashion.patch(f"/api/orders/{order.id}", json={"status": "cancelled"}).status_code == 422
    actions = [(e["action"], e["data"].get("to")) for e in fashion.get(f"/api/orders/{order.id}").json()["audit"]]
    assert actions == [("order.status_changed", s) for s in ("accepted", "ready", "out_for_delivery", "delivered")]
