"""Payment confirmation workflow: provider result -> payment/order update -> customer notification."""
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.core.logging import bind_context, get_logger, log_operation
from app.i18n import t
from app.models import Business, Payment
from app.services.commerce_service import OrderService, money
from app.services.conversation_service import ConversationService
from app.services.messaging_service import send_to_customer
from app.services.payment_service import PaymentService
from app.workflows.handoff import conversation_language

logger = get_logger(__name__)


def find_payment_by_reference(db: Session, provider: str, reference: str) -> Payment:
    p = db.scalar(select(Payment).where(Payment.provider == provider, Payment.provider_reference == reference))
    if not p:
        raise NotFoundError("Payment not found")
    return p


def find_payment_by_id(db: Session, provider: str, payment_id: str) -> Payment:
    try:
        pid = uuid.UUID(payment_id)
    except ValueError:
        raise NotFoundError("Payment not found")
    p = db.scalar(select(Payment).where(Payment.id == pid, Payment.provider == provider))
    if not p:
        raise NotFoundError("Payment not found")
    return p


def confirm_payment(db: Session, payment: Payment, status: str, raw: dict | None = None,
                    failure_reason: str | None = None) -> bool:
    """Tenant is derived from the payment row itself (never from the callback body)."""
    business_id = payment.business_id
    bind_context(business_id=business_id)
    with log_operation(logger, "payment.confirmation", provider=payment.provider, result=status):
        svc = PaymentService(db, business_id)
        changed = svc.apply_result(payment, status, raw, failure_reason)
        if changed:
            notify_payment_result(db, business_id, payment.id)
        return changed


def refresh_and_notify(db: Session, payment: Payment) -> bool:
    svc = PaymentService(db, payment.business_id)
    changed = svc.refresh(payment)
    if changed:
        notify_payment_result(db, payment.business_id, payment.id)
    return changed


def notify_payment_result(db: Session, business_id: uuid.UUID, payment_id: uuid.UUID) -> None:
    payment = PaymentService(db, business_id).payments.get(payment_id)
    order = OrderService(db, business_id).get(payment.order_id)
    business = db.get(Business, business_id)
    if not order.conversation_id:
        return
    conv = ConversationService(db, business_id).repo.get(order.conversation_id)
    if conv is None:
        return
    lang = conversation_language(conv, business)
    if payment.status == "successful":
        text = t("provider_paid", lang, number=order.order_number, amount=f"{order.currency} {money(payment.amount)}",
                 shop=business.name)
    else:
        text = t("provider_failed", lang, number=order.order_number,
                 reason=f" ({payment.failure_reason})" if payment.failure_reason else "")
    send_to_customer(db, business_id, conv, text, role="assistant", metadata={"event": "payment_" + payment.status})

