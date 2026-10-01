"""Development WhatsApp simulator. Builds a payload in Meta's exact webhook format and runs it
through the same pipeline as real traffic (parse -> tenant -> customer -> agent -> adapter)."""
import uuid

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import TenantContext, get_tenant
from app.core.config import settings
from app.integrations.whatsapp.parser import build_text_webhook
from app.models import Message, WhatsAppAccount
from app.repositories.repos import MessageRepo, WhatsAppAccountRepo
from app.schemas.api import MessageOut, SimulateIn
from app.workflows.inbound import process_webhook_payload

router = APIRouter(prefix="/api/dev", tags=["dev"])


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
    payload = build_text_webhook(account.phone_number_id, account.display_phone_number or "", body.from_number.lstrip("+"),
                                 body.text, f"wamid.sim.{uuid.uuid4().hex}", body.name)
    results = process_webhook_payload(payload)
    result = results[0] if results else None
    replies = []
    if result and result.conversation_id:
        msgs = MessageRepo(ctx.db, ctx.business_id).list(
            where=[Message.conversation_id == result.conversation_id], order_by=[Message.created_at.desc()], limit=6)
        replies = [MessageOut.of(m).model_dump(mode="json") for m in reversed(msgs)]
    return {"status": result.status if result else "error",
            "conversation_id": str(result.conversation_id) if result and result.conversation_id else None,
            "reply": result.reply if result else None,
            "agent_run_id": str(result.agent_run_id) if result and result.agent_run_id else None,
            "account_mode": account.mode, "recent_messages": replies}
