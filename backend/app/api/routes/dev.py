"""Development WhatsApp simulator. Builds a payload in Meta's exact webhook format and runs it
through the same pipeline as real traffic (parse -> tenant -> customer -> agent -> adapter)."""
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from app.api.deps import TenantContext, get_tenant
from app.core.config import settings
from app.integrations.whatsapp.parser import build_text_webhook
from app.models import AgentRun, Message, WebhookEvent, WhatsAppAccount
from app.repositories.repos import MessageRepo, WhatsAppAccountRepo
from app.schemas.api import MessageOut, SimulateIn
from app.workflows.inbound import process_webhook_payload

router = APIRouter(prefix="/api/dev", tags=["dev"])
WORKER_WAIT_SECONDS = 60


def _wait_for_worker(ctx: TenantContext, wamid: str) -> tuple[uuid.UUID | None, str | None, str | None, str]:
    """A background worker claimed the message first (or an earlier message of this customer is still being
    answered: per-customer order). Wait for it like WhatsApp would, then report what it did."""
    deadline = time.monotonic() + WORKER_WAIT_SECONDS
    status = "queued"
    while time.monotonic() < deadline:
        ctx.db.expire_all()
        event = ctx.db.scalar(select(WebhookEvent).where(WebhookEvent.business_id == ctx.business_id,
                                                         WebhookEvent.external_id == wamid))
        if event is None or event.status in ("done", "dead"):
            status = (event.result or event.status) if event else "error"
            break
        time.sleep(0.25)
    inbound = MessageRepo(ctx.db, ctx.business_id).first(Message.wa_message_id == wamid)
    if inbound is None:
        return None, None, None, status
    run = ctx.db.scalar(select(AgentRun).where(AgentRun.business_id == ctx.business_id,
                                               AgentRun.trigger_message_id == inbound.id))
    return inbound.conversation_id, run.response_text if run else None, str(run.id) if run else None, status


@router.post("/simulate")
def simulate(body: SimulateIn, ctx: TenantContext = Depends(get_tenant)):
    if not settings.enable_dev_tools or settings.is_production:
        raise HTTPException(404, "Not found")
    repo = WhatsAppAccountRepo(ctx.db, ctx.business_id)
    account = repo.first(WhatsAppAccount.is_active.is_(True))
    if account is None:
        account = repo.add(phone_number_id=f"dev-{ctx.business.slug}"[:64], display_phone_number="DEV",
                           mode="dev")
        ctx.db.commit()
    wamid = f"wamid.sim.{uuid.uuid4().hex}"
    payload = build_text_webhook(account.phone_number_id, account.display_phone_number or "", body.from_number.lstrip("+"),
                                 body.text, wamid, body.name)
    results = process_webhook_payload(payload)
    result = results[0] if results else None
    status = result.status if result else "error"
    conversation_id = result.conversation_id if result else None
    reply = result.reply if result else None
    run_id = str(result.agent_run_id) if result and result.agent_run_id else None
    if status == "queued":
        conversation_id, reply, run_id, status = _wait_for_worker(ctx, wamid)
    replies = []
    if conversation_id:
        msgs = MessageRepo(ctx.db, ctx.business_id).list(
            where=[Message.conversation_id == conversation_id], order_by=[Message.created_at.desc()], limit=6)
        replies = [MessageOut.of(m).model_dump(mode="json") for m in reversed(msgs)]
    return {"status": status, "conversation_id": str(conversation_id) if conversation_id else None,
            "reply": reply, "agent_run_id": run_id, "account_mode": account.mode, "recent_messages": replies}
