"""Send outbound WhatsApp messages for a tenant and persist them."""
import uuid

from sqlalchemy.orm import Session

from app.core.logging import get_logger, log_operation
from app.integrations.whatsapp.adapters import get_adapter
from app.models import Conversation, Message, WhatsAppAccount
from app.repositories.repos import WhatsAppAccountRepo
from app.services.conversation_service import ConversationService

logger = get_logger(__name__)


def send_to_customer(db: Session, business_id: uuid.UUID, conversation: Conversation, text: str, *,
                     role: str = "assistant", agent_run_id: uuid.UUID | None = None,
                     account: WhatsAppAccount | None = None, metadata: dict | None = None) -> Message:
    convs = ConversationService(db, business_id)
    account = account or WhatsAppAccountRepo(db, business_id).first(WhatsAppAccount.is_active.is_(True))
    if account is None:
        return convs.add_message(conversation, role, text, delivery_status="failed", agent_run_id=agent_run_id,
                                 metadata={**(metadata or {}), "error": "No WhatsApp account connected"})
    adapter = get_adapter(account)
    with log_operation(logger, "whatsapp.send", mode=adapter.mode) as ctx:
        result = adapter.send_text(conversation.customer.whatsapp_number, text)
        ctx["status"] = "ok" if result.ok else "error"
    meta = dict(metadata or {})
    if result.error:
        meta["error"] = result.error
    return convs.add_message(conversation, role, text, delivery_status=result.delivery_status,
                             wa_message_id=result.wa_message_id, agent_run_id=agent_run_id, metadata=meta)
