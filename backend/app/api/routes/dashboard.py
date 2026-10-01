from fastapi import APIRouter, Depends

from app.api.deps import TenantContext, get_tenant
from app.models import Order
from app.schemas.api import OrderOut, ProductOut
from app.services.commerce_service import OrderService
from app.services.conversation_service import ConversationService, CustomerService
from app.services.payment_service import PaymentService
from app.services.product_service import ProductService

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/stats")
def stats(ctx: TenantContext = Depends(get_tenant)):
    """Every number is a real COUNT/SUM over this tenant's rows. No synthetic analytics."""
    orders = OrderService(ctx.db, ctx.business_id)
    products = ProductService(ctx.db, ctx.business_id)
    convs = ConversationService(ctx.db, ctx.business_id)
    status_counts = {s: orders.orders.count(Order.status == s) for s in
                     ("pending", "awaiting_payment", "paid", "processing", "ready", "out_for_delivery", "delivered",
                      "cancelled")}
    return {
        "currency": ctx.business.currency,
        "orders_total": orders.orders.count(),
        "orders_by_status": status_counts,
        "revenue": float(orders.revenue()),
        "customers": CustomerService(ctx.db, ctx.business_id).repo.count(),
        "messages": convs.message_total(),
        "products": products.products.count(),
        "pending_payments": PaymentService(ctx.db, ctx.business_id).pending_count(),
        "needs_attention": convs.attention_count(),
        "low_stock": [ProductOut.of(p).model_dump(mode="json") for p in products.low_stock()[:10]],
        "recent_orders": [OrderOut.model_validate(o).model_dump(mode="json") for o in orders.list(limit=8)],
    }
