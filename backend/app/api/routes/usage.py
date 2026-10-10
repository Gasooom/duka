"""Usage reports for the signed-in shop (docs/P3_MONTHLY_USAGE.md), read from the usage ledger. Read-only."""
from fastapi import APIRouter, Depends, Query

from app.api.deps import TenantContext, get_tenant
from app.services import usage_report

router = APIRouter(prefix="/api/usage", tags=["usage"])


@router.get("/monthly")
def monthly(month: str | None = Query(None, description="YYYY-MM, a calendar month of the shop's time zone; this "
                                                        "month when omitted"),
            ctx: TenantContext = Depends(get_tenant)):
    """This shop's usage in one calendar month of its own time zone, from the usage ledger (`usage_events`): AI model
    calls, paid embeddings requests and WhatsApp traffic, with the estimated costs recorded on the events. Unpriced
    events are counted, never treated as free. The shop is the signed-in user's, never a parameter."""
    return usage_report.tenant_month(ctx.db.get_bind(), ctx.business_id, ctx.business.timezone, month)
