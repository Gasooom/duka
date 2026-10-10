"""Public webhooks: WhatsApp Cloud API and payment providers."""
import json
from functools import partial

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.errors import NotFoundError
from app.core.logging import get_logger, log_event
from app.core.security import hmac_sha256, verify_meta_signature
from app.db.session import get_db
from app.services.messaging_service import commit_and_deliver
from app.workflows.inbound import ingest_and_commit
from app.workflows.payments import confirm_payment, find_payment_by_id, find_payment_by_reference, refresh_and_notify
from app.workflows.worker import workers

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = get_logger(__name__)


@router.get("/whatsapp")
def verify_whatsapp(mode: str | None = Query(None, alias="hub.mode"),
                    token: str | None = Query(None, alias="hub.verify_token"),
                    challenge: str | None = Query(None, alias="hub.challenge")):
    """Meta's subscription handshake: echo hub.challenge when the verify token matches."""
    if mode == "subscribe" and settings.whatsapp_verify_token and token == settings.whatsapp_verify_token:
        return Response(content=challenge or "", media_type="text/plain")
    raise HTTPException(403, "Verification failed")


@router.post("/whatsapp")
async def receive_whatsapp(request: Request):
    raw = await request.body()
    if settings.whatsapp_app_secret:
        if not verify_meta_signature(raw, request.headers.get("X-Hub-Signature-256"), settings.whatsapp_app_secret):
            log_event(logger, "webhook.bad_signature", 30, operation="webhook.whatsapp", status="rejected")
            raise HTTPException(401, "Invalid signature")
    elif settings.is_production:
        raise HTTPException(503, "WHATSAPP_APP_SECRET must be configured in production")
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")
    # Persist before acknowledging: if this raises (e.g. database down) the 5xx makes Meta redeliver.
    # Processing (agent + reply) happens in the workers, so the response stays fast.
    # Here only with a valid signature, or in development with no app secret configured (then nothing is verified).
    result = await run_in_threadpool(partial(ingest_and_commit, payload, verified=bool(settings.whatsapp_app_secret)))
    workers.wake()
    return {"status": "received", "accepted": len(result.event_ids)}


# ---------------------------------------------------------------- payments
@router.post("/payments/mock")
async def mock_payment_callback(request: Request, db: Session = Depends(get_db)):
    """Mock provider callback. Body: {"reference": "...", "status": "successful"|"failed"}.
    Requires header X-Mock-Signature = hex HMAC-SHA256(PAYMENT_WEBHOOK_SECRET, body)."""
    if settings.is_production:
        raise HTTPException(404, "Not found")
    raw = await request.body()
    if not settings.payment_webhook_secret:
        raise HTTPException(503, "PAYMENT_WEBHOOK_SECRET not configured")
    if request.headers.get("X-Mock-Signature") != hmac_sha256(settings.payment_webhook_secret, raw):
        raise HTTPException(401, "Invalid signature")
    body = json.loads(raw)
    try:
        payment = find_payment_by_reference(db, "mock", str(body.get("reference")))
    except NotFoundError:
        raise HTTPException(404, "Payment not found")
    changed = confirm_payment(db, payment, str(body.get("status")), body,
                              body.get("reason") if body.get("status") == "failed" else None)
    commit_and_deliver(db)
    return {"changed": changed, "status": payment.status}


@router.api_route("/payments/momo/{payment_id}", methods=["PUT", "POST"])
def momo_callback(payment_id: str, db: Session = Depends(get_db)):
    """MTN MoMo callback. MoMo callbacks are unsigned, so the body is only a hint:
    we re-query the MoMo API for the authoritative status before changing anything."""
    try:
        payment = find_payment_by_id(db, "momo", payment_id)
    except NotFoundError:
        raise HTTPException(404, "Payment not found")
    changed = refresh_and_notify(db, payment)
    commit_and_deliver(db)
    return {"changed": changed, "status": payment.status}
