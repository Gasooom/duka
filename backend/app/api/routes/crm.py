import uuid

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import TenantContext, get_tenant
from app.schemas.api import AgentRunOut, CustomerOut, HumanReplyIn, MessageOut, OrderOut, ReturnToAiIn
from app.services.commerce_service import OrderService
from app.services.conversation_service import ConversationService, CustomerService
from app.services.messaging_service import commit_and_deliver, send_to_customer
from app.workflows.handoff import staff_return_to_ai, staff_take_over

router = APIRouter(prefix="/api", tags=["customers", "conversations"])


@router.get("/customers")
def list_customers(q: str | None = None, ctx: TenantContext = Depends(get_tenant)):
    customers = CustomerService(ctx.db, ctx.business_id).list(q=q)
    orders = OrderService(ctx.db, ctx.business_id)
    out = []
    for c in customers:
        cust_orders = orders.for_customer(c, limit=100)
        out.append({**CustomerOut.model_validate(c).model_dump(mode="json"), "order_count": len(cust_orders),
                    "total_spent": float(sum(o.total for o in cust_orders if o.status not in ("pending", "awaiting_payment", "cancelled")))})
    return out


@router.get("/customers/{customer_id}")
def get_customer(customer_id: uuid.UUID, ctx: TenantContext = Depends(get_tenant)):
    c = CustomerService(ctx.db, ctx.business_id).get(customer_id)
    return {**CustomerOut.model_validate(c).model_dump(mode="json"),
            "orders": [OrderOut.model_validate(o).model_dump(mode="json")
                       for o in OrderService(ctx.db, ctx.business_id).for_customer(c, limit=50)]}


def _conv_summary(conv, last) -> dict:
    return {"id": str(conv.id), "status": conv.status, "needs_attention": conv.needs_attention,
            "language": conv.language_code, "language_confidence": conv.language_confidence,
            "handoff_reason": conv.handoff_reason,
            "customer": {"id": str(conv.customer.id), "name": conv.customer.name,
                         "whatsapp_number": conv.customer.whatsapp_number},
            "last_message": MessageOut.of(last).model_dump(mode="json") if last else None,
            "last_message_at": conv.last_message_at}


@router.get("/conversations")
def list_conversations(status: str | None = None, needs_attention: bool | None = None,
                       ctx: TenantContext = Depends(get_tenant)):
    svc = ConversationService(ctx.db, ctx.business_id)
    convs = svc.list(status=status, needs_attention=needs_attention)
    last = svc.latest_messages_by_conversation([c.id for c in convs])
    return [_conv_summary(c, last.get(c.id)) for c in convs]


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: uuid.UUID, ctx: TenantContext = Depends(get_tenant)):
    """Conversation debugger payload: every message (incl. tool calls/results) and every agent run."""
    svc = ConversationService(ctx.db, ctx.business_id)
    conv = svc.get(conversation_id)
    return {**_conv_summary(conv, None), "summary": conv.summary, "state": conv.state,
            "messages": [MessageOut.of(m).model_dump(mode="json") for m in svc.history(conv)],
            "agent_runs": [AgentRunOut.model_validate(r).model_dump(mode="json") for r in svc.runs_for(conv)]}


@router.post("/conversations/{conversation_id}/reply")
def human_reply(conversation_id: uuid.UUID, body: HumanReplyIn, ctx: TenantContext = Depends(get_tenant)):
    svc = ConversationService(ctx.db, ctx.business_id)
    conv = svc.get(conversation_id)
    if conv.status != "human":
        raise HTTPException(400, "Take over the conversation (handoff) before replying manually")
    msg = send_to_customer(ctx.db, ctx.business_id, conv, body.text, role="human_agent",
                           metadata={"user_id": str(ctx.user.id)})
    conv.needs_attention = False
    commit_and_deliver(ctx.db)
    ctx.db.refresh(msg)  # final delivery status (sent / failed / retry)
    return MessageOut.of(msg)


@router.post("/conversations/{conversation_id}/handoff")
def take_over(conversation_id: uuid.UUID, ctx: TenantContext = Depends(get_tenant)):
    """Staff takes over: the AI stays silent for this conversation until someone returns it to AI."""
    conv = ConversationService(ctx.db, ctx.business_id).get(conversation_id)
    staff_take_over(ctx.db, ctx.user, conv)
    ctx.db.commit()
    return {"status": conv.status}


@router.post("/conversations/{conversation_id}/return-to-ai")
def return_to_ai(conversation_id: uuid.UUID, body: ReturnToAiIn | None = None,
                 ctx: TenantContext = Depends(get_tenant)):
    """The only way the assistant resumes after a handoff. Optionally tells the customer."""
    conv = ConversationService(ctx.db, ctx.business_id).get(conversation_id)
    if conv.status != "human":
        raise HTTPException(400, "The assistant is already handling this conversation")
    staff_return_to_ai(ctx.db, ctx.user, conv, body.message if body else None)
    commit_and_deliver(ctx.db)
    return {"status": conv.status}
