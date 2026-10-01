"""Payment orchestration over the PaymentProvider abstraction."""
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import ExternalServiceError, ValidationError
from app.core.logging import get_logger, log_event
from app.integrations.payments import PaymentRequest, get_payment_provider
from app.models import Business, Order, Payment
from app.repositories.repos import PaymentRepo, SettingsRepo
from app.services.commerce_service import OrderService, money
from app.services.conversation_service import normalize_phone

logger = get_logger(__name__)


class PaymentService:
    def __init__(self, db: Session, business_id: uuid.UUID):
        self.db = db
        self.business_id = business_id
        self.payments = PaymentRepo(db, business_id)
        self.orders = OrderService(db, business_id)

    def provider_name(self) -> str:
        s = SettingsRepo(self.db, self.business_id).first()
        return s.payment_provider if s else "mock"

    def for_order(self, order: Order) -> list[Payment]:
        return self.payments.list(where=[Payment.order_id == order.id], order_by=[Payment.created_at.desc()])

    def initiate(self, order: Order, payer_phone: str) -> Payment:
        business = self.db.get(Business, self.business_id)
        if not business.payment_enabled:
            raise ValidationError("Online payment is not enabled for this business")
        if order.status not in ("pending", "awaiting_payment"):
            raise ValidationError(f"Order {order.order_number} is {order.status}; payment not required")
        # Idempotency: reuse an in-flight payment instead of charging twice.
        existing = self.payments.first(Payment.order_id == order.id, Payment.status == "pending")
        if existing:
            return existing
        phone = normalize_phone(payer_phone)
        provider = get_payment_provider(self.provider_name())
        payment = self.payments.add(order_id=order.id, provider=provider.name,
                                    provider_reference=f"pending-{uuid.uuid4()}", amount=order.total,
                                    currency=order.currency, status="pending", payer_phone=phone, raw={})
        try:
            result = provider.request_payment(PaymentRequest(
                payment_id=str(payment.id), order_number=order.order_number, amount=order.total,
                currency=order.currency, payer_phone=phone,
                description=f"{business.name} order {order.order_number}: {order.currency} {money(order.total)}"))
        except ExternalServiceError as exc:
            payment.status = "failed"
            payment.failure_reason = exc.message
            self.db.flush()
            raise
        payment.provider_reference = result.reference
        payment.raw = result.raw
        if order.status == "pending":
            self.orders.transition(order, "awaiting_payment")
        self.db.flush()
        log_event(logger, "payment.initiated", operation="payment.initiate", provider=provider.name,
                  order_number=order.order_number)
        if result.status != "pending":
            self.apply_result(payment, result.status, result.raw, result.failure_reason)
        return payment

    def apply_result(self, payment: Payment, status: str, raw: dict[str, Any] | None = None,
                     failure_reason: str | None = None) -> bool:
        """Apply a provider-confirmed status. Idempotent: returns True only if state changed."""
        if status not in ("pending", "successful", "failed"):
            raise ValidationError(f"Invalid payment status '{status}'")
        payment = self.payments.get(payment.id, for_update=True)
        if payment.status in ("successful", "failed", "cancelled") or status == "pending":
            return False
        payment.status = status
        payment.raw = {**(payment.raw or {}), "confirmation": raw or {}}
        payment.failure_reason = failure_reason
        order = self.orders.get(payment.order_id)
        if status == "successful":
            payment.confirmed_at = datetime.now(timezone.utc)
            if order.status in ("pending", "awaiting_payment"):
                if order.status == "pending":
                    self.orders.transition(order, "awaiting_payment")
                self.orders.transition(order, "paid")
        self.db.flush()
        log_event(logger, "payment.result", operation="payment.confirm", status=status,
                  order_number=order.order_number)
        return True

    def refresh(self, payment: Payment) -> bool:
        """Poll the provider (fallback when callbacks are delayed). Never guesses."""
        if payment.status != "pending":
            return False
        provider = get_payment_provider(payment.provider)
        result = provider.get_status(payment.provider_reference)
        return self.apply_result(payment, result.status, result.raw, result.failure_reason)

    def pending_count(self) -> int:
        return self.payments.count(Payment.status == "pending")
