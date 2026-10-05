"""Order and payment workflows: state change + audit + customer message + owner notification, in one
transaction (messages go through the outbox, so they are only sent if the change commits)."""
import uuid

from sqlalchemy.orm import Session

from app.i18n import t
from app.models import Business, Conversation, Customer, Message, Order, Payment, User
from app.repositories.repos import ConversationRepo, SettingsRepo
from app.services import audit_service
from app.services.commerce_service import CheckoutService, OrderService, money
from app.services.hours import closed_until
from app.services.messaging_service import notify_owner, send_to_customer
from app.services.payment_service import PaymentService
from app.workflows.handoff import conversation_language


def _items(order: Order) -> str:
    return "\n".join(f"- {i.product_name} x{i.quantity}: {order.currency} {money(i.subtotal)}" for i in order.items)


def payment_instructions(db: Session, business: Business) -> str | None:
    if not business.payment_enabled:
        return None
    s = SettingsRepo(db, business.id).first()
    if s and s.payment_provider == "manual" and s.payment_instructions:
        return s.payment_instructions.strip()
    return None


def _tell_customer(db: Session, order: Order, render, event: str) -> None:
    """`render(lang) -> text`: the message is written in the conversation's current language."""
    if not order.conversation_id:
        return
    conv = ConversationRepo(db, order.business_id).get(order.conversation_id)
    if conv is not None:
        text = render(conversation_language(conv, db.get(Business, order.business_id)))
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


def order_placed_text(order: Order, business: Business, lang: str = "en") -> str:
    delivery = f"{t('delivery', lang)}: {order.currency} {money(order.delivery_fee)}\n" if order.delivery_zone_name else ""
    return (f"{t('order_confirmed', lang, number=order.order_number)}\n{_items(order)}\n{delivery}"
            f"{t('total', lang)}: {order.currency} {money(order.total)}\n"
            f"{_review_eta(business, lang)}")


def _review_eta(business: Business, lang: str = "en") -> str:
    opening = closed_until(business.business_hours, business.timezone, lang=lang)
    if opening:
        return t("review_closed", lang, shop=business.name, opening=opening)
    return t("review_soon", lang, shop=business.name)


def owner_set_status(db: Session, user: User, order: Order, status: str, reason: str | None = None) -> Order:
    business = db.get(Business, order.business_id)
    old = order.status
    OrderService(db, order.business_id).transition(order, status, reason=reason)
    audit_service.record(db, order.business_id, "order.status_changed", "order", order.id, user=user,
                         order_number=order.order_number, **{"from": old, "to": status}, reason=reason)
    instructions = payment_instructions(db, business) if status == "accepted" and order.payment_status != "paid" \
        else None

    def render(lang: str) -> str:
        if status == "accepted":
            text = t("accepted", lang, shop=business.name, number=order.order_number,
                     total=f"{order.currency} {money(order.total)}")
            # The owner's instructions are inserted exactly as written (they hold the MoMo number).
            return text + "\n" + t("to_pay", lang, instructions=instructions) if instructions else text
        if status == "cancelled":
            text = t("cancelled", lang, number=order.order_number, reason=f": {reason}" if reason else "")
            return text + t("refund_note", lang) if order.payment_status == "paid" else text
        return t(status, lang, number=order.order_number, shop=business.name)

    _tell_customer(db, order, render, f"order_{status}")
    return order


def owner_record_payment(db: Session, user: User, order: Order, *, method: str, reference: str | None,
                         note: str | None) -> Payment:
    payment = PaymentService(db, order.business_id).record_manual(order, user, method=method, reference=reference,
                                                                  note=note)
    _tell_customer(db, order, lambda lang: t("manual_payment_received", lang, number=order.order_number,
                                             amount=f"{order.currency} {money(payment.amount)}"),
                   "payment_successful")
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
