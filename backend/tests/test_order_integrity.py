"""Order, payment and stock integrity under concurrency (docs/D_MERCHANT_OPERATIONS_AUDIT.md, D1-D4, D13, D14).

The races run as the audit's probe did: two sessions load the same order (two browser tabs, or a retried request),
then act at the same moment. Every change to an order or its payments now re-reads the order under a row lock, and
stock is locked in product-id order, so the second action works on what the first committed."""
import threading
import uuid

import pytest
from sqlalchemy import event, select, text

from app.cli import main
from app.core.errors import DomainError
from app.db.session import SessionLocal, engine
from app.models import InventoryMovement, Order, Payment, Product, User
from app.services.business_service import register_business
from app.services.commerce_service import CartService, OrderService
from app.services.conversation_service import ConversationService, CustomerService
from app.services.payment_service import PaymentService
from app.services.product_service import ProductService
from app.workflows.orders import owner_set_status


@pytest.fixture
def shop() -> dict:
    """A pickup-only shop with two products (5 in stock each) and a pending order for one of each, whose items are not
    in product-id order."""
    with SessionLocal() as db:
        business, user, _ = register_business(db, business_name=f"Integrity {uuid.uuid4().hex[:6]}",
                                              email=f"{uuid.uuid4().hex[:10]}@test.dev", password="password123")
        business.delivery_enabled = False
        products = ProductService(db, business.id)
        shoe = products.create({"name": "Canvas shoe", "price": 1000, "stock_quantity": 5, "sku": "SHOE-1"})
        hat = products.create({"name": "Wool hat", "price": 500, "stock_quantity": 5, "sku": "HAT-1"})
        customer = CustomerService(db, business.id).upsert_from_whatsapp("250788000111", "Integrity")
        conv = ConversationService(db, business.id).get_or_create_active(customer)
        carts = CartService(db, business.id)
        for product in sorted((shoe, hat), key=lambda p: p.id, reverse=True):
            carts.add_item(carts.get_active(customer, conv), product, 1)
        order = OrderService(db, business.id).create_from_cart(customer, conv)
        db.commit()
        return {"business_id": business.id, "user_id": user.id, "shoe": shoe.id, "hat": hat.id,
                "order_id": order.id, "order_number": order.order_number}


def race(*actions) -> list[str]:
    """Run each `action(db, loaded)` in its own session at the same time: each loads what it needs, waits at `loaded`
    until all have, then acts and commits. The outcomes ("ok" or the refusal), sorted."""
    loaded, outcomes = threading.Barrier(len(actions), timeout=30), []

    def run(action):
        with SessionLocal() as db:
            try:
                action(db, loaded)
                db.commit()
                outcomes.append("ok")
            except DomainError as exc:
                db.rollback()
                outcomes.append(exc.message)
    threads = [threading.Thread(target=run, args=(a,)) for a in actions]
    for th in threads:
        th.start()
    for th in threads:
        th.join(60)
    return sorted(outcomes)


def stock(product_id: uuid.UUID) -> int:
    with SessionLocal() as db:
        return db.get(Product, product_id).stock_quantity


def movements(product_id: uuid.UUID) -> list[tuple[int, str]]:
    with SessionLocal() as db:
        return [(m.change, m.reason) for m in db.scalars(
            select(InventoryMovement).where(InventoryMovement.product_id == product_id)
            .order_by(InventoryMovement.created_at, InventoryMovement.id))]


def test_two_simultaneous_cancellations_restock_once(shop):
    def cancel(db, loaded):
        order = OrderService(db, shop["business_id"]).get(shop["order_id"])
        user = db.get(User, shop["user_id"])
        assert order.status == "pending" and len(order.items) == 2
        loaded.wait()
        owner_set_status(db, user, order, "cancelled", "two tabs")
    assert race(cancel, cancel) == ["Cannot change order from cancelled to cancelled", "ok"]
    for product_id in (shop["shoe"], shop["hat"]):
        assert stock(product_id) == 5  # the audit's probe: 6, restocked twice
        assert [m for m in movements(product_id) if m[1] == "order_cancelled"] == [(1, "order_cancelled")]


def test_two_simultaneous_cash_payments_record_one(shop):
    def pay(db, loaded):
        order = OrderService(db, shop["business_id"]).get(shop["order_id"])
        user = db.get(User, shop["user_id"])
        assert order.payment_status == "unpaid"
        loaded.wait()
        PaymentService(db, shop["business_id"]).record_manual(order, user, method="cash", reference=None,
                                                              note=f"cash at the till {uuid.uuid4().hex[:4]}")
    assert race(pay, pay) == [f"Order {shop['order_number']} is already paid", "ok"]
    with SessionLocal() as db:
        assert db.get(Order, shop["order_id"]).payment_status == "paid"
        assert len(db.scalars(select(Payment).where(Payment.order_id == shop["order_id"],
                                                    Payment.status == "successful")).all()) == 1


def test_a_stock_count_and_a_sale_at_once_keep_the_ledger_exact(shop, monkeypatch):
    """The owner sets an absolute stock while a sale lands. The edit now locks the product when it reads it, so the
    sale waits and the ledger's movements still add up to the stock (before: 12 in stock, movements adding up to 11)."""
    product_id, read, sold = shop["shoe"], threading.Event(), threading.Event()
    set_stock = ProductService.set_stock

    def edit_after_a_pause(self, p, new_qty, **kw):  # between reading the product and writing the new stock
        if threading.current_thread().name == "owner":
            read.set()
            sold.wait(timeout=1.5)  # with the lock, the sale cannot commit meanwhile
        return set_stock(self, p, new_qty, **kw)
    monkeypatch.setattr(ProductService, "set_stock", edit_after_a_pause)

    def owner():
        with SessionLocal() as db:
            ProductService(db, shop["business_id"]).update(product_id, {"stock_quantity": 12})
            db.commit()

    def sale():
        read.wait(timeout=10)
        with SessionLocal() as db:
            ProductService(db, shop["business_id"]).adjust_stock(product_id, -1, reason="order")
            db.commit()
        sold.set()
    threads = [threading.Thread(target=owner, name="owner"), threading.Thread(target=sale, name="sale")]
    for th in threads:
        th.start()
    for th in threads:
        th.join(30)
    assert stock(product_id) == 11 == sum(change for change, _ in movements(product_id))


def test_a_cancellation_locks_its_products_in_one_statement_in_product_id_order(shop):
    """As checkout does, so a cancellation and a checkout of the same products cannot deadlock."""
    product_locks = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "FOR UPDATE" in statement and "FROM products" in statement:
            product_locks.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        with SessionLocal() as db:
            orders = OrderService(db, shop["business_id"])
            orders.transition(orders.get(shop["order_id"]), "cancelled")
            db.commit()
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(product_locks) == 1 and "ORDER BY products.id" in product_locks[0]
    assert stock(shop["shoe"]) == stock(shop["hat"]) == 5


def test_a_csv_import_records_its_stock_as_import_and_the_check_finds_a_bypass(shop, capsys):
    with SessionLocal() as db:
        result = ProductService(db, shop["business_id"]).import_csv(
            b"name,price,sku,stock_quantity\nCanvas shoe,1000,SHOE-1,9\n")
        db.commit()
    assert result.updated == 1 and movements(shop["shoe"])[-1] == (5, "import")  # 4 -> 9
    assert main(["inventory-check"]) == 0
    assert "every product's stock matches its inventory ledger" in capsys.readouterr().out
    with engine.begin() as conn:  # a change that bypasses the ledger
        conn.execute(text("UPDATE products SET stock_quantity = stock_quantity + 3 WHERE id = :p"), {"p": shop["shoe"]})
    assert main(["inventory-check"]) == 1
    out = capsys.readouterr().out
    assert "Canvas shoe (SHOE-1) stock 12, inventory movements add up to 9" in out and "1 product(s)" in out
