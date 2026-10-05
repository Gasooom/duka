"""Human control. Rules (explicit, no timers):
  * The AI hands over when the customer asks for a person (deterministic, multilingual), when it calls the
    handoff tool, or when the customer sends media it cannot read (voice notes, images...).
  * While a conversation is in 'human' mode the AI never replies; customer messages are stored and flagged.
  * Only a staff member's explicit "return to AI" resumes the assistant.
  * A handoff discards any order summary awaiting confirmation: a person may have changed the deal, so the
    customer must confirm a fresh summary afterwards.
"""
from sqlalchemy.orm import Session

from app.agents.language import effective
from app.i18n import t
from app.models import Business, Conversation, User
from app.services import audit_service
from app.services.commerce_service import CheckoutService
from app.services.conversation_service import ConversationService
from app.services.hours import closed_until
from app.services.messaging_service import notify_owner, send_to_customer


def conversation_language(conv: Conversation | None, business: Business) -> str:
    """The language every customer-facing message in this conversation uses."""
    return effective(conv.language_code if conv is not None else None, business.language)


def handoff_reply(business: Business, lang: str = "en", *, message: bool = False) -> str:
    """Honest expectation: after hours, say when the team will be back instead of 'shortly'.
    `message=True` words it for a single message (e.g. a voice note) rather than the conversation."""
    opening = closed_until(business.business_hours, business.timezone, lang=lang)
    key = "handoff_message" if message else "handoff"
    return t(f"{key}_closed", lang, opening=opening) if opening else t(key, lang)


def ai_paused_reply(business: Business, lang: str = "en") -> str:
    opening = closed_until(business.business_hours, business.timezone, lang=lang)
    return t("ai_paused_closed", lang, opening=opening) if opening else t("ai_paused_soon", lang)


def request_human(db: Session, business: Business, conv: Conversation, reason: str) -> None:
    """Customer-side handoff (customer asked, agent decided, or unsupported media). Idempotent."""
    if conv.status == "human":
        conv.needs_attention = True
        return
    ConversationService(db, business.id).handoff(conv, reason)
    CheckoutService(db, business.id).cancel(conv.customer)
    who = conv.customer.name or f"+{conv.customer.whatsapp_number}"
    notify_owner(db, business.id, "handoff", f"🙋 {who} needs a person on WhatsApp ({reason}). "
                                             "Open the Duka inbox to reply.",
                 entity_type="conversation", entity_id=conv.id)


def staff_take_over(db: Session, user: User, conv: Conversation) -> None:
    ConversationService(db, conv.business_id).handoff(conv, f"Taken over by {user.email}")
    CheckoutService(db, conv.business_id).cancel(conv.customer)
    conv.needs_attention = False  # a person is now handling it
    audit_service.record(db, conv.business_id, "conversation.taken_over", "conversation", conv.id, user=user)


def staff_return_to_ai(db: Session, user: User, conv: Conversation, message: str | None = None) -> None:
    ConversationService(db, conv.business_id).return_to_ai(conv)
    audit_service.record(db, conv.business_id, "conversation.returned_to_ai", "conversation", conv.id, user=user)
    if message:
        send_to_customer(db, conv.business_id, conv, message, role="human_agent",
                         metadata={"user_id": str(user.id), "event": "returned_to_ai"})
