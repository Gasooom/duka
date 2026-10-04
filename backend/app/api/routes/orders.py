import uuid

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import TenantContext, get_tenant, require_owner
from app.core.config import settings
from app.schemas.api import (
    AuditEventOut,
    CustomerOut,
    ManualPaymentIn,
    OrderOut,
    OrderStatusIn,
    PaymentOut,
    SimulatePaymentIn,
    VoidPaymentIn,
)
from app.services import audit_service
from app.services.commerce_service import OrderService
from app.services.conversation_service import CustomerService
from app.services.messaging_service import commit_and_deliver
from app.services.payment_service import PaymentService
from app.workflows.orders import owner_record_payment, owner_set_status, owner_void_payment
from app.workflows.payments import confirm_payment, refresh_and_notify

router = APIRouter(prefix="/api", tags=["orders"])


def _order_detail(ctx: TenantContext, order) -> dict:
    payments = PaymentService(ctx.db, ctx.business_id).for_order(order)
    customer = CustomerService(ctx.db, ctx.business_id).get(order.customer_id)
    return {**OrderOut.model_validate(order).model_dump(mode="json"),
            "payments": [PaymentOut.model_validate(p).model_dump(mode="json") for p in payments],
            "customer": CustomerOut.model_validate(customer).model_dump(mode="json"),
            "audit": [AuditEventOut.model_validate(e).model_dump(mode="json")
                      for e in audit_service.for_entity(ctx.db, ctx.business_id, order.id)]}


@router.get("/orders", response_model=list[OrderOut])
def list_orders(status: str | None = None, ctx: TenantContext = Depends(get_tenant)):
    return OrderService(ctx.db, ctx.business_id).list(status=status)


@router.get("/orders/{order_id}")
def get_order(order_id: uuid.UUID, ctx: TenantContext = Depends(get_tenant)):
    return _order_detail(ctx, OrderService(ctx.db, ctx.business_id).get(order_id))


@router.patch("/orders/{order_id}")
def update_status(order_id: uuid.UUID, body: OrderStatusIn, ctx: TenantContext = Depends(get_tenant)):
    """Owner review and fulfilment: accepted | ready | out_for_delivery | delivered | cancelled (with reason).
    The customer is told on WhatsApp. Payment is never set here."""
    order = OrderService(ctx.db, ctx.business_id).get(order_id)
    owner_set_status(ctx.db, ctx.user, order, body.status, body.reason)
    commit_and_deliver(ctx.db)
    return _order_detail(ctx, order)


@router.post("/orders/{order_id}/payments", status_code=201)
def record_manual_payment(order_id: uuid.UUID, body: ManualPaymentIn, ctx: TenantContext = Depends(require_owner)):
    """Owner confirms a payment received outside the platform (MoMo to the shop, cash, bank). Audited."""
    order = OrderService(ctx.db, ctx.business_id).get(order_id)
    owner_record_payment(ctx.db, ctx.user, order, method=body.method, reference=body.reference, note=body.note)
    commit_and_deliver(ctx.db)
    return _order_detail(ctx, order)


@router.post("/payments/{payment_id}/void")
def void_payment(payment_id: uuid.UUID, body: VoidPaymentIn, ctx: TenantContext = Depends(require_owner)):
    payment = PaymentService(ctx.db, ctx.business_id).payments.get_or_404(payment_id)
    owner_void_payment(ctx.db, ctx.user, payment, body.reason)
    commit_and_deliver(ctx.db)
    return _order_detail(ctx, OrderService(ctx.db, ctx.business_id).get(payment.order_id))


@router.post("/payments/{payment_id}/refresh")
def refresh_payment(payment_id: uuid.UUID, ctx: TenantContext = Depends(get_tenant)):
    """Poll the provider for the real status (useful if a callback was missed)."""
    payment = PaymentService(ctx.db, ctx.business_id).payments.get_or_404(payment_id)
    changed = refresh_and_notify(ctx.db, payment)
    commit_and_deliver(ctx.db)
    return {"changed": changed, "status": payment.status}


@router.post("/payments/{payment_id}/simulate")
def simulate_payment(payment_id: uuid.UUID, body: SimulatePaymentIn, ctx: TenantContext = Depends(get_tenant)):
    """DEV ONLY: act as the mock provider and deliver a callback. Runs the real confirmation workflow."""
    if not settings.enable_dev_tools or settings.is_production:
        raise HTTPException(404, "Not found")
    payment = PaymentService(ctx.db, ctx.business_id).payments.get_or_404(payment_id)
    if payment.provider != "mock":
        raise HTTPException(400, "Only mock payments can be simulated")
    changed = confirm_payment(ctx.db, payment, body.status, {"simulated_by": str(ctx.user.id)},
                              None if body.status == "successful" else "Simulated failure")
    commit_and_deliver(ctx.db)
    return {"changed": changed, "status": payment.status}
