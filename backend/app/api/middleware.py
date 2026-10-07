"""HTTP hardening in the app itself, so it holds behind any proxy (Render) or none: security headers on every
response and a request-body size limit. deploy/Caddyfile sets the same headers for the self-hosted stack."""
import json

from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import get_logger, log_event

logger = get_logger(__name__)

# Swagger UI / ReDoc (development only; off in production) load their scripts from a CDN.
_DOCS_PATHS = ("/docs", "/redoc")


class SecurityMiddleware:
    """Pure ASGI (no buffering): a declared Content-Length over the limit is refused before the body is read; a body
    without one (chunked) is counted while the app reads it and stopped at the limit. Webhook signatures still see
    the exact bytes: nothing is rewritten."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int, hsts: bool) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.too_large = f"Request body too large (max {max_body_bytes // (1024 * 1024)} MB)"
        self.headers = {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        }
        if hsts:  # production is served over HTTPS only (PUBLIC_BASE_URL must be https); browsers ignore it on http
            self.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        extra = dict(self.headers)
        if not path.startswith(_DOCS_PATHS):
            # The API only returns JSON/text: nothing in a response may load or be framed.
            extra["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        if path.startswith("/api/"):
            extra["Cache-Control"] = "no-store"  # tenant data must not be kept by browsers or shared caches

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in extra.items():
                    headers.setdefault(name, value)
            await send(message)

        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_body_bytes:
            self._log(path, int(declared))
            await self._reject(send_with_headers)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    self._log(path, received)
                    # Raised inside the app's body read: FastAPI passes HTTPException through to its handler (413).
                    raise HTTPException(413, self.too_large)
            return message

        await self.app(scope, limited_receive, send_with_headers)

    async def _reject(self, send: Send) -> None:
        body = json.dumps({"detail": self.too_large}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                                (b"connection", b"close")]})  # the unread body is never consumed
        await send({"type": "http.response.body", "body": body})

    def _log(self, path: str, size: int) -> None:
        log_event(logger, "http.body_too_large", 30, operation="http", status="rejected", path=path,
                  size=size, limit=self.max_body_bytes)
