"""Payment orchestration. Order.payment_status (unpaid | pending | paid) is only ever changed here.

A payment becomes successful in exactly two ways:
  * the provider confirms it (signed mock callback, or MoMo status re-query)  -> confirmation_source='provider'
  * an owner records a manual payment with evidence (MoMo ref / cash note)    -> confirmation_source='owner'
The agent can only *report* a reference the customer gave (a pending manual payment the owner must verify).
Every manual action is written to the audit trail."""
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import ConflictError, ExternalServiceError, ValidationError
from app.core.logging import get_logger, log_event
from app.integrations.payments import PaymentRequest, get_payment_provider
from app.models import Business, Order, Payment, User
from app.repositories.repos import PaymentRepo, SettingsRepo
from app.services import audit_service
from app.services.commerce_service import OrderService, money
from app.services.conversation_service import normalize_phone

logger = get_logger(__name__)
MANUAL_METHODS = ("momo", "cash", "bank", "other")


class PaymentService:
    def __init__(self, db: Session, business_id: uuid.UUID):
        self.db = db
        self.business_id = business_id
        self.payments = PaymentRepo(db, business_id)
        self.orders = OrderService(db, business_id)

    def provider_name(self) -> str:
        s = SettingsRepo(self.db, self.business_id).first()
        return s.payment_provider if s else "manual"

    def for_order(self, order: Order) -> list[Payment]:
        return self.payments.list(where=[Payment.order_id == order.id], order_by=[Payment.created_at.desc()])

    def _payable(self, order: Order) -> None:
        if order.status == "cancelled":
            raise ValidationError(f"Order {order.order_number} is cancelled", code="order_cancelled",
                                  params={"number": order.order_number})
        if order.payment_status == "paid":
            raise ValidationError(f"Order {order.order_number} is already paid", code="order_paid",
                                  params={"number": order.order_number})

    def _sync_order_status(self, order: Order) -> None:
        """Derive order.payment_status from its payments."""
        statuses = {p.status for p in self.for_order(order)}
        if "successful" in statuses:
            order.payment_status = "paid"
            order.paid_at = order.paid_at or datetime.now(timezone.utc)
        else:
            order.payment_status = "pending" if "pending" in statuses else "unpaid"
            order.paid_at = None

    # ------------------------------------------------------------ provider payments (mock / momo)
    def initiate(self, order: Order, payer_phone: str) -> Payment:
        business = self.db.get(Business, self.business_id)
        if not business.payment_enabled:
            raise ValidationError("Online payment is not enabled for this business", code="payment_disabled")
        self._payable(order)
        name = self.provider_name()
        if name == "manual":
            raise ValidationError("This shop takes payment manually; share the payment instructions instead")
        if name == "mock" and settings.is_production:
            raise ValidationError("Test payments are disabled in production")
        # Idempotency: reuse an in-flight payment instead of charging twice.
        existing = self.payments.first(Payment.order_id == order.id, Payment.status == "pending",
                                       Payment.provider == name)
        if existing:
            return existing
        phone = normalize_phone(payer_phone)
        provider = get_payment_provider(name)
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
        self._sync_order_status(order)
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
        if payment.provider == "manual":
            raise ValidationError("Manual payments are confirmed by the owner, not by a provider")
        if payment.status in ("successful", "failed", "cancelled", "voided") or status == "pending":
            return False
        payment.status = status
        payment.raw = {**(payment.raw or {}), "confirmation": raw or {}}
        payment.failure_reason = failure_reason
        order = self.orders.get(payment.order_id)
        if status == "successful":
            payment.confirmed_at = datetime.now(timezone.utc)
            payment.confirmation_source = "provider"
            if order.status == "cancelled":  # money arrived for a cancelled order: staff must refund
                audit_service.record(self.db, self.business_id, "payment.received_for_cancelled_order", "order",
                                     order.id, payment_id=str(payment.id), amount=str(payment.amount))
        self._sync_order_status(order)
        self.db.flush()
        log_event(logger, "payment.result", operation="payment.confirm", status=status,
                  order_number=order.order_number)
        return True

    def refresh(self, payment: Payment) -> bool:
        """Poll the provider (fallback when callbacks are delayed). Never guesses."""
        if payment.status != "pending" or payment.provider == "manual":
            return False
        provider = get_payment_provider(payment.provider)
        result = provider.get_status(payment.provider_reference)
        return self.apply_result(payment, result.status, result.raw, result.failure_reason)

    # ------------------------------------------------------------ manual payments
    def report_reference(self, order: Order, reference: str, payer_phone: str | None = None) -> Payment:
        """The customer says they paid and gives a reference (agent tool). Recorded as PENDING for the owner to
        verify; this can never mark the order paid."""
        reference = (reference or "").strip()
        if not 4 <= len(reference) <= 128:
            raise ValidationError("Please send the full transaction reference", code="reference_too_short")
        self._payable(order)
        existing = self.payments.first(Payment.order_id == order.id, Payment.provider == "manual",
                                       Payment.external_reference == reference)
        if existing:
            return existing
        payment = self.payments.add(order_id=order.id, provider="manual", provider_reference=f"manual-{uuid.uuid4()}",
                                    amount=order.total, currency=order.currency, status="pending",
                                    payer_phone=payer_phone, external_reference=reference, raw={},
                                    note="Reported by the customer on WhatsApp; not verified")
        self._sync_order_status(order)
        audit_service.record(self.db, self.business_id, "payment.reference_reported", "order", order.id,
                             actor_type="customer", payment_id=str(payment.id), reference=reference)
        self.db.flush()
        return payment

    def record_manual(self, order: Order, user: User, *, method: str, reference: str | None,
                      note: str | None) -> Payment:
        """Owner confirms a payment they received (MoMo to the shop's number, cash, bank)."""
        if method not in MANUAL_METHODS:
            raise ValidationError(f"method must be one of {', '.join(MANUAL_METHODS)}")
        reference = (reference or "").strip() or None
        note = (note or "").strip() or None
        if method in ("momo", "bank") and not reference:
            raise ValidationError("A transaction reference is required for MoMo and bank payments")
        if method in ("cash", "other") and not (reference or note):
            raise ValidationError("Add a note (e.g. who received the cash) or a receipt number")
        self._payable(order)
        now = datetime.now(timezone.utc)
        # Settle the customer's reported reference if it matches, otherwise record a new payment.
        payment = self.payments.first(Payment.order_id == order.id, Payment.provider == "manual",
                                      Payment.status == "pending", Payment.external_reference == reference) \
            if reference else None
        try:
            with self.db.begin_nested():
                if payment is None:
                    payment = self.payments.add(order_id=order.id, provider="manual",
                                                provider_reference=f"manual-{uuid.uuid4()}", amount=order.total,
                                                currency=order.currency, status="pending", raw={})
                payment.status, payment.method, payment.external_reference = "successful", method, reference
                payment.note = note
                payment.confirmation_source, payment.confirmed_by_user_id, payment.confirmed_at = "owner", user.id, now
                self.db.flush()
        except IntegrityError:
            raise ConflictError(f"Reference '{reference}' was already used to pay another order")
        for other in self.for_order(order):  # other unverified claims on this order are now moot
            if other.id != payment.id and other.status == "pending":
                other.status = "cancelled"
        self._sync_order_status(order)
        audit_service.record(self.db, self.business_id, "payment.manual_recorded", "order", order.id, user=user,
                             payment_id=str(payment.id), method=method, reference=reference, note=note,
                             amount=str(payment.amount), currency=payment.currency)
        self.db.flush()
        return payment

    def void_manual(self, payment: Payment, user: User, reason: str) -> Payment:
        """Undo an owner-recorded payment entered by mistake. Audited; the record is kept."""
        reason = (reason or "").strip()
        if not reason:
            raise ValidationError("A reason is required to void a payment")
        if payment.provider != "manual" or payment.status != "successful":
            raise ValidationError("Only a successful manual payment can be voided")
        payment.status = "voided"
        payment.note = f"{payment.note or ''}\nVoided by {user.email}: {reason}".strip()
        order = self.orders.get(payment.order_id)
        self._sync_order_status(order)
        audit_service.record(self.db, self.business_id, "payment.voided", "order", order.id, user=user,
                             payment_id=str(payment.id), reason=reason)
        self.db.flush()
        return payment

    def pending_count(self) -> int:
        return self.payments.count(Payment.status == "pending")
