"""Order and payment workflows: state change + audit + customer message + owner notification, in one
transaction (messages go through the outbox, so they are only sent if the change commits)."""
import uuid

from sqlalchemy.orm import Session

from app.models import Business, Conversation, Customer, Message, Order, Payment, User
from app.repositories.repos import ConversationRepo, SettingsRepo
from app.services import audit_service
from app.services.commerce_service import CheckoutService, OrderService, money
from app.services.hours import closed_until
from app.services.messaging_service import notify_owner, send_to_customer
from app.services.payment_service import PaymentService


def _items(order: Order) -> str:
    return "\n".join(f"- {i.product_name} x{i.quantity}: {order.currency} {money(i.subtotal)}" for i in order.items)


def payment_instructions(db: Session, business: Business) -> str | None:
    if not business.payment_enabled:
        return None
    s = SettingsRepo(db, business.id).first()
    if s and s.payment_provider == "manual" and s.payment_instructions:
        return s.payment_instructions.strip()
    return None


def _tell_customer(db: Session, order: Order, text: str, event: str) -> None:
    if not order.conversation_id:
        return
    conv = ConversationRepo(db, order.business_id).get(order.conversation_id)
    if conv is not None:
        send_to_customer(db, order.business_id, conv, text, metadata={"event": event, "order": order.order_number})


def place_confirmed_order(db: Session, business: Business, customer: Customer, conv: Conversation,
                          confirmation: Message) -> Order:
    """Customer said YES to the delivered summary: create the order, audit it, notify the owner."""
    order = CheckoutService(db, business.id).confirm(customer, conv, confirmation)
    audit_service.record(db, business.id, "order.confirmed_by_customer", "order", order.id, actor_type="customer",
                         order_number=order.order_number, confirmation_message_id=str(confirmation.id),
                         total=str(order.total))
    notify_owner(db, business.id, "new_order",
                 f"🛒 New order {order.order_number}: {order.currency} {money(order.total)}\n{_items(order)}\n"
                 f"Customer: {customer.name or ''} +{customer.whatsapp_number}\n"
                 f"{'Deliver to: ' + order.delivery_address if order.delivery_address else 'Pickup'}\n"
                 "Review and accept it in the Duka dashboard.",
                 entity_type="order", entity_id=order.id)
    return order


def order_placed_text(order: Order, business: Business) -> str:
    return (f"✅ Order {order.order_number} confirmed!\n{_items(order)}\n"
            f"{'Delivery: ' + order.currency + ' ' + money(order.delivery_fee) + chr(10) if order.delivery_zone_name else ''}"
            f"Total: {order.currency} {money(order.total)}\n"
            f"{_review_eta(business)}")


def _review_eta(business: Business) -> str:
    opening = closed_until(business.business_hours, business.timezone)
    if opening:
        return f"{business.name} is closed right now and will review it when it opens ({opening})."
    return f"{business.name} will review it and confirm shortly."


_STATUS_TEXT = {
    "ready": "📦 Your order {n} is ready.",
    "out_for_delivery": "🚚 Your order {n} is on the way.",
    "delivered": "✅ Your order {n} was delivered. Thank you for shopping with {shop}!",
}


def owner_set_status(db: Session, user: User, order: Order, status: str, reason: str | None = None) -> Order:
    business = db.get(Business, order.business_id)
    old = order.status
    OrderService(db, order.business_id).transition(order, status, reason=reason)
    audit_service.record(db, order.business_id, "order.status_changed", "order", order.id, user=user,
                         order_number=order.order_number, **{"from": old, "to": status}, reason=reason)
    if status == "accepted":
        text = f"✅ {business.name} accepted your order {order.order_number} ({order.currency} {money(order.total)})."
        instructions = payment_instructions(db, business) if order.payment_status != "paid" else None
        if instructions:
            text += f"\nTo pay: {instructions}\nReply with the transaction ID once you have paid."
    elif status == "cancelled":
        text = f"❌ Your order {order.order_number} was cancelled{': ' + reason if reason else ''}."
        if order.payment_status == "paid":
            text += " We'll contact you about your refund."
    else:
        text = _STATUS_TEXT[status].format(n=order.order_number, shop=business.name)
    _tell_customer(db, order, text, f"order_{status}")
    return order


def owner_record_payment(db: Session, user: User, order: Order, *, method: str, reference: str | None,
                         note: str | None) -> Payment:
    payment = PaymentService(db, order.business_id).record_manual(order, user, method=method, reference=reference,
                                                                  note=note)
    _tell_customer(db, order, f"✅ Payment received for order {order.order_number} "
                              f"({order.currency} {money(payment.amount)}). Thank you!", "payment_successful")
    return payment


def owner_void_payment(db: Session, user: User, payment: Payment, reason: str) -> Payment:
    return PaymentService(db, payment.business_id).void_manual(payment, user, reason)


def customer_reported_payment(db: Session, business_id: uuid.UUID, customer: Customer, order: Order,
                              reference: str) -> Payment:
    payment = PaymentService(db, business_id).report_reference(order, reference, customer.whatsapp_number)
    notify_owner(db, business_id, "payment_reported",
                 f"💳 +{customer.whatsapp_number} says they paid order {order.order_number} "
                 f"({order.currency} {money(order.total)}), reference {payment.external_reference}. "
                 "Check your MoMo/bank and record the payment in the Duka dashboard.",
                 entity_type="order", entity_id=order.id)
    return payment
