"""HTTP hardening that lives in the app itself, so it holds on Render (no Caddy in front): security headers on every
response and a request-body limit that stops oversized requests — webhooks included — without touching what a
valid request (or its signature) sees."""
import hashlib
import hmac
import json

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.middleware import SecurityMiddleware
from app.core.config import settings
from app.db.session import SessionLocal
from app.integrations.whatsapp.parser import build_text_webhook
from app.main import app
from app.models import KnowledgeDocument, WebhookEvent

LIMIT = settings.max_request_body_bytes
TOO_LARGE = {"detail": "Request body too large (max 10 MB)"}


def _count(model) -> int:
    with SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(model))


def _chunks(data: bytes, size: int = 1 << 20):
    """Request content without a Content-Length (sent chunked): only counting the stream can stop it."""
    for i in range(0, len(data), size):
        yield data[i:i + size]


async def _plain_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"ok"})


def test_security_headers_on_every_kind_of_response(client, fashion):
    responses = {
        "health": client.get("/healthz"), "ready": client.get("/readyz"), "api": fashion.get("/api/products"),
        "unauthenticated": client.get("/api/products"), "not found": client.get("/no-such-page"),
        "validation error": client.post("/api/auth/login", json={}),
        "preflight": client.options("/api/products", headers={"Origin": "http://localhost:3000",
                                                               "Access-Control-Request-Method": "GET"}),
    }
    for name, r in responses.items():
        assert r.headers["x-content-type-options"] == "nosniff", name
        assert r.headers["x-frame-options"] == "DENY", name
        assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin", name
        assert r.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()", name
        assert r.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'", name
        assert "strict-transport-security" not in r.headers, name  # not production
    assert responses["api"].headers["cache-control"] == "no-store"  # tenant data is never cached
    assert "cache-control" not in responses["health"].headers
    assert responses["ready"].status_code == 200 and responses["health"].json() == {"status": "ok"}


def test_interactive_docs_keep_working_in_development(client):
    r = client.get("/docs")
    assert r.status_code == 200 and "swagger" in r.text.lower()
    assert "content-security-policy" not in r.headers  # Swagger UI loads its scripts from a CDN
    assert r.headers["x-frame-options"] == "DENY"


def test_hsts_is_sent_in_production_only():
    [mw] = [m for m in app.user_middleware if m.cls is SecurityMiddleware]
    assert mw.kwargs == {"max_body_bytes": LIMIT, "hsts": settings.is_production}
    prod = TestClient(SecurityMiddleware(_plain_app, max_body_bytes=LIMIT, hsts=True))
    assert prod.get("/").headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
    dev = TestClient(SecurityMiddleware(_plain_app, max_body_bytes=LIMIT, hsts=False))
    assert "strict-transport-security" not in dev.get("/").headers


def test_a_route_can_still_set_its_own_header_value():
    async def custom(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"x-frame-options", b"SAMEORIGIN")]})
        await send({"type": "http.response.body", "body": b""})
    r = TestClient(SecurityMiddleware(custom, max_body_bytes=LIMIT, hsts=False)).get("/")
    assert r.headers.get_list("x-frame-options") == ["SAMEORIGIN"]


def test_declared_oversized_body_is_refused_before_it_is_read(client):
    r = client.post("/api/auth/login", content=b"x" * (LIMIT + 1), headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json() == TOO_LARGE
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-request-id"]


def test_oversized_webhook_is_refused_and_nothing_is_stored(client, fashion):
    payload = build_text_webhook(fashion.phone_number_id, "+250700", "250788111222", "x" * (LIMIT + 10), "wamid.BIG")
    raw = json.dumps(payload).encode()
    r = client.post("/webhooks/whatsapp", content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json() == TOO_LARGE
    r = client.post("/webhooks/whatsapp", content=_chunks(raw), headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json() == TOO_LARGE  # no Content-Length: stopped while streaming
    assert _count(WebhookEvent) == 0


def test_signed_webhook_streamed_without_length_is_still_verified_and_accepted(fashion, monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_app_secret", "test-app-secret")
    raw = json.dumps(build_text_webhook(fashion.phone_number_id, "+250700", "250788111222", "black sneakers",
                                        "wamid.SIGNED")).encode()
    signature = "sha256=" + hmac.new(b"test-app-secret", raw, hashlib.sha256).hexdigest()
    r = fashion.client.post("/webhooks/whatsapp", content=_chunks(raw, 64),
                            headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature})
    assert r.status_code == 200 and r.json()["accepted"] == 1
    bad = fashion.client.post("/webhooks/whatsapp", content=_chunks(raw, 64),
                              headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=" + "0" * 64})
    assert bad.status_code == 401


def test_a_body_exactly_at_the_limit_is_accepted(client):
    body = b'{"object": "whatsapp_business_account", "entry": []}'
    raw = body + b" " * (LIMIT - len(body))  # valid JSON padded with whitespace to exactly the limit
    r = client.post("/webhooks/whatsapp", content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 200 and r.json() == {"status": "received", "accepted": 0}


def test_oversized_multipart_upload_is_refused_before_parsing(fashion):
    big = b"a" * (LIMIT + 1)
    r = fashion.post("/api/knowledge/upload", files={"file": ("notes.txt", big)})
    assert r.status_code == 413 and r.json() == TOO_LARGE
    boundary = "duka-test-boundary"
    multipart = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"notes.txt\"\r\n"
                 f"Content-Type: text/plain\r\n\r\n").encode() + big + f"\r\n--{boundary}--\r\n".encode()
    r = fashion.client.post("/api/knowledge/upload", content=_chunks(multipart),
                            headers={**fashion.h, "Content-Type": f"multipart/form-data; boundary={boundary}"})
    assert r.status_code == 413 and r.json() == TOO_LARGE
    assert _count(KnowledgeDocument) == 0


def test_route_limits_below_the_global_one_still_apply(fashion):
    r = fashion.post("/api/knowledge/upload", files={"file": ("notes.txt", b"a" * 5_000_001)})
    assert r.status_code == 413 and r.json()["detail"] == "File too large (max 5MB)"
    ok = fashion.post("/api/knowledge/upload", files={"file": ("hours.txt", b"Open daily 9am to 8pm.")})
    assert ok.status_code == 201
