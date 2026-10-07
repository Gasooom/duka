"""Owner reminders for orders that hold stock while they wait: still pending review too long, or accepted and still
unpaid too long. One reminder per order and state, safe to run again and again; nothing else changes (no
cancellation, no stock movement, no customer message), and each shop hears only about its own orders."""
from sqlalchemy import func, select, text

from app.core.config import settings
from app.db.session import SessionLocal
from app.models import InventoryMovement, Notification, Product
from app.workflows.orders import PAYMENT_REMINDER, REVIEW_REMINDER, remind_aging_orders
from app.workflows.worker import BackgroundWorkers
from tests.conftest import place_order

NUMBER = "250788111222"
OWNER_F, OWNER_E = "250788900001", "250788900002"


def _age(order_id: str, hours: float, column: str = "created_at") -> None:
    assert column in ("created_at", "accepted_at")
    with SessionLocal() as s:
        s.execute(text(f"UPDATE orders SET {column} = clock_timestamp() - make_interval(secs => :secs) "
                       "WHERE id = CAST(:id AS uuid)"), {"secs": hours * 3600, "id": order_id})
        s.commit()


def _reminders(kind: str | None = None) -> list[Notification]:
    with SessionLocal() as s:
        stmt = select(Notification).where(Notification.kind.in_((REVIEW_REMINDER, PAYMENT_REMINDER)))
        return list(s.scalars((stmt.where(Notification.kind == kind) if kind else stmt)
                              .order_by(Notification.created_at)))


def _stock() -> tuple:
    with SessionLocal() as s:
        return (sorted((str(p.id), p.stock_quantity) for p in s.scalars(select(Product))),
                s.scalar(select(func.count()).select_from(InventoryMovement)))


def _status(t, order: dict) -> dict:
    return t.get(f"/api/orders/{order['id']}").json()


def test_a_pending_order_waiting_too_long_gets_exactly_one_reminder(fashion, outbox):
    order = place_order(fashion, NUMBER)
    messages_before, stock_before = len(outbox.sent), _stock()
    assert remind_aging_orders() == 0  # just placed: nothing to remind
    _age(order["id"], 3)  # the default review threshold is 2 h (ORDER_REVIEW_REMINDER_HOURS)
    assert remind_aging_orders() == 1
    assert remind_aging_orders() == 0  # safe to run again: never repeated
    [r] = _reminders()
    assert (r.kind, r.entity_type, str(r.entity_id)) == (REVIEW_REMINDER, "order", order["id"])
    assert order["order_number"] in r.body and "waiting 3 hours" in r.body and r.status == "skipped"  # no phone set
    assert _stock() == stock_before and _status(fashion, order)["status"] == "pending"  # nothing else changed
    assert len(outbox.sent) == messages_before  # the customer is not messaged


def test_an_accepted_unpaid_order_gets_one_payment_reminder(fashion, outbox):
    order = place_order(fashion, NUMBER)
    assert fashion.patch(f"/api/orders/{order['id']}", json={"status": "accepted"}).status_code == 200
    stock_before = _stock()
    _age(order["id"], 25, "accepted_at")  # the default payment threshold is 24 h
    assert remind_aging_orders() == 1 and remind_aging_orders() == 0
    [r] = _reminders()
    assert r.kind == PAYMENT_REMINDER and "accepted 25 hours ago and is still unpaid" in r.body
    _age(order["id"], 30)  # long ago placed too, but it is no longer pending: no review reminder
    assert remind_aging_orders() == 0
    assert _stock() == stock_before and _status(fashion, order)["status"] == "accepted"


def test_each_state_is_reminded_once_per_order(fashion, outbox):
    order = place_order(fashion, NUMBER)
    _age(order["id"], 5)
    remind_aging_orders()
    fashion.patch(f"/api/orders/{order['id']}", json={"status": "accepted"})
    _age(order["id"], 26, "accepted_at")
    remind_aging_orders()
    remind_aging_orders()
    assert [r.kind for r in _reminders()] == [REVIEW_REMINDER, PAYMENT_REMINDER]


def test_paid_cancelled_and_delivered_orders_get_no_reminder(fashion, outbox):
    paid = place_order(fashion, "250788111301")
    fashion.patch(f"/api/orders/{paid['id']}", json={"status": "accepted"})
    r = fashion.post(f"/api/orders/{paid['id']}/payments", json={"method": "cash", "note": "Received at the shop"})
    assert r.status_code == 201 and _status(fashion, paid)["payment_status"] == "paid"
    cancelled = place_order(fashion, "250788111302")
    fashion.patch(f"/api/orders/{cancelled['id']}", json={"status": "cancelled", "reason": "Out of stock"})
    delivered = place_order(fashion, "250788111303")
    fashion.patch(f"/api/orders/{delivered['id']}", json={"status": "accepted"})
    fashion.patch(f"/api/orders/{delivered['id']}", json={"status": "delivered"})
    for order in (paid, cancelled, delivered):
        _age(order["id"], 72)
        _age(order["id"], 72, "accepted_at")
    stock_before = _stock()
    assert remind_aging_orders() == 0 and _reminders() == []
    assert _stock() == stock_before


def test_each_shop_is_reminded_only_about_its_own_orders(fashion, electronics, outbox):
    fashion.patch("/api/business/settings", json={"owner_notification_phone": "+" + OWNER_F})
    electronics.patch("/api/business/settings", json={"owner_notification_phone": "+" + OWNER_E})
    f_order = place_order(fashion, NUMBER)
    e_order = place_order(electronics, "250788111400", query="Do you have a Samsung phone under 300k?", pick="add it")
    _age(f_order["id"], 3)
    _age(e_order["id"], 3)
    assert remind_aging_orders() == 2
    by_order = {str(r.entity_id): r for r in _reminders()}
    assert str(by_order[f_order["id"]].business_id) == fashion.business_id
    assert str(by_order[e_order["id"]].business_id) == electronics.business_id
    assert by_order[f_order["id"]].recipient == OWNER_F and by_order[e_order["id"]].recipient == OWNER_E
    sent_to_f = [body for to, body in outbox.sent if to == OWNER_F and "waiting" in body]
    sent_to_e = [body for to, body in outbox.sent if to == OWNER_E and "waiting" in body]
    assert sent_to_f == [by_order[f_order["id"]].body] and sent_to_e == [by_order[e_order["id"]].body]
    assert fashion.get("/api/dashboard/notifications").json()[0]["entity_id"] == f_order["id"]


def test_the_worker_runs_the_sweep(fashion, outbox):
    order = place_order(fashion, NUMBER)
    _age(order["id"], 4)
    BackgroundWorkers().run_once()
    assert [str(r.entity_id) for r in _reminders(REVIEW_REMINDER)] == [order["id"]]


def test_reminders_can_be_switched_off(fashion, outbox, monkeypatch):
    monkeypatch.setattr(settings, "order_review_reminder_hours", 0)
    order = place_order(fashion, NUMBER)
    _age(order["id"], 100)
    assert remind_aging_orders() == 0
