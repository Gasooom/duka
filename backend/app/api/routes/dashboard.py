from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func

from app.api.deps import TenantContext, get_tenant
from app.core.config import settings as app_settings
from app.models import AgentRun, AuditEvent, Message, Notification, Order, Product
from app.models.commerce import ORDER_STATUSES
from app.repositories.repos import AgentRunRepo, AuditEventRepo, MessageRepo, NotificationRepo
from app.schemas.api import AuditEventOut, NotificationOut, OrderOut, ProductOut
from app.services.business_service import BusinessConfigService
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
    status_counts = {s: orders.orders.count(Order.status == s) for s in ORDER_STATUSES}
    return {
        "currency": ctx.business.currency,
        "orders_total": orders.orders.count(),
        "orders_by_status": status_counts,
        "orders_awaiting_review": status_counts["pending"],
        "orders_unpaid": orders.orders.count(Order.payment_status != "paid", Order.status != "cancelled"),
        "revenue": float(orders.revenue()),
        "customers": CustomerService(ctx.db, ctx.business_id).repo.count(),
        "messages": convs.message_total(),
        "products": products.products.count(),
        "pending_payments": PaymentService(ctx.db, ctx.business_id).pending_count(),
        "needs_attention": convs.attention_count(),
        "ai_enabled": BusinessConfigService(ctx.db, ctx.business_id).settings().ai_enabled,
        "low_stock": [ProductOut.of(p).model_dump(mode="json") for p in products.low_stock()[:10]],
        "recent_orders": [OrderOut.model_validate(o).model_dump(mode="json") for o in orders.list(limit=8)],
    }


@router.get("/usage")
def usage(days: int = Query(30, ge=1, le=366), ctx: TenantContext = Depends(get_tenant)):
    """Assistant and messaging ACTIVITY for this tenant over the last `days` days, counted from agent_runs and
    messages. Not a provider-usage or cost report: `llm_calls` also counts the offline rules engine, the calls of a
    turn that failed and was retried are not all here, and messages are not send attempts. Provider usage and
    estimated costs: GET /api/usage/monthly (the usage ledger). Its semantics are to be revisited with the usage UI
    (roadmap C5)."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    runs = AgentRunRepo(ctx.db, ctx.business_id)
    row = ctx.db.execute(runs.select(
        func.count(AgentRun.id), func.coalesce(func.sum(AgentRun.llm_calls), 0),
        func.coalesce(func.sum(AgentRun.prompt_tokens), 0), func.coalesce(func.sum(AgentRun.completion_tokens), 0),
        func.count(AgentRun.id).filter(AgentRun.status == "error"),
        func.avg(AgentRun.latency_ms)).where(AgentRun.created_at >= since)).one()
    messages = MessageRepo(ctx.db, ctx.business_id)
    return {
        "days": days,
        "agent_runs": row[0], "llm_calls": int(row[1]), "prompt_tokens": int(row[2]),
        "completion_tokens": int(row[3]), "agent_errors": row[4],
        "avg_latency_ms": round(float(row[5]), 1) if row[5] is not None else None,
        "messages_in": messages.count(Message.role == "customer", Message.created_at >= since),
        "messages_out": messages.count(Message.role.in_(("assistant", "human_agent")), Message.created_at >= since),
    }


@router.get("/notifications", response_model=list[NotificationOut])
def notifications(limit: int = Query(50, ge=1, le=200), ctx: TenantContext = Depends(get_tenant)):
    """Owner notifications (new orders, handoffs, reported payments) and whether they reached WhatsApp."""
    return NotificationRepo(ctx.db, ctx.business_id).list(order_by=[Notification.created_at.desc()], limit=limit)


@router.get("/audit", response_model=list[AuditEventOut])
def audit(limit: int = Query(100, ge=1, le=500), ctx: TenantContext = Depends(get_tenant)):
    return AuditEventRepo(ctx.db, ctx.business_id).list(order_by=[AuditEvent.created_at.desc()], limit=limit)


@router.get("/setup")
def setup(ctx: TenantContext = Depends(get_tenant)):
    """What is still missing before this shop can serve real customers. Every check is a real lookup."""
    cfg = BusinessConfigService(ctx.db, ctx.business_id)
    b, s = ctx.business, cfg.settings()
    accounts = [a for a in cfg.whatsapp_accounts() if a.is_active]
    live = any(a.mode == "cloud" and a.access_token_encrypted for a in accounts)
    products = ProductService(ctx.db, ctx.business_id).products.count(Product.active.is_(True))
    zones = [z for z in cfg.delivery_zones() if z.active]
    checks = [
        ("whatsapp", live, "Connect your real WhatsApp Business number" if not live else "WhatsApp number connected",
         "/dashboard/whatsapp"),
        ("products", products > 0, f"{products} active product(s)" if products else "Add or import your products",
         "/dashboard/products"),
        ("delivery", not b.delivery_enabled or bool(zones),
         "Add delivery zones (fees and the areas they cover)" if b.delivery_enabled and not zones
         else "Delivery set up" if b.delivery_enabled else "Pickup only", "/dashboard/settings"),
        ("payment_instructions", not (b.payment_enabled and s.payment_provider == "manual")
         or bool(s.payment_instructions), "Write the payment instructions customers receive"
         if b.payment_enabled and s.payment_provider == "manual" and not s.payment_instructions
         else "Payment instructions set", "/dashboard/settings"),
        ("owner_alerts", bool(s.owner_notification_phone), "Add your WhatsApp number for new-order alerts"
         if not s.owner_notification_phone else "Order alerts go to +" + s.owner_notification_phone,
         "/dashboard/settings"),
        ("hours", bool(b.business_hours), "Set your business hours" if not b.business_hours else "Business hours set",
         "/dashboard/business"),
        ("ai_enabled", s.ai_enabled, "The AI assistant is paused" if not s.ai_enabled else "AI assistant is on",
         "/dashboard/settings"),
        # Platform-level, not fixable by the shop, shown so nobody mistakes the offline engine for real AI.
        ("platform_ai", app_settings.llm_provider == "openai_compat" and bool(app_settings.llm_api_key),
         "Platform: AI language model not configured yet (offline rules engine)"
         if not (app_settings.llm_provider == "openai_compat" and app_settings.llm_api_key)
         else "AI language model connected", None),
    ]
    return {"ready": all(ok for _, ok, _, _ in checks),
            "checks": [{"key": k, "ok": ok, "message": m, "link": link} for k, ok, m, link in checks]}
