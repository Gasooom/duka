import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.routes import auth, business, crm, dashboard, dev, knowledge, orders, products, webhooks
from app.core.config import settings
from app.core.errors import DomainError
from app.core.logging import clear_context, configure_logging, get_logger, log_event, request_id_var
from app.db.session import engine

configure_logging(settings.log_level)
logger = get_logger("app")

if settings.is_production and settings.jwt_secret in ("", "change-me-in-env"):
    raise RuntimeError("JWT_SECRET must be set in production")

app = FastAPI(title=settings.app_name, version="0.1.0",
              description="Multi-tenant WhatsApp AI commerce platform. See /docs for the API contract.")
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    request_id_var.set(rid)
    clear_context()
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message, "code": exc.code})


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    errors = [{"field": ".".join(str(x) for x in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": "Invalid request", "errors": errors})


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log_event(logger, "unhandled_error", 40, operation=request.url.path, status="error", error=repr(exc)[:500])
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


for r in (auth, business, products, orders, crm, knowledge, dashboard, webhooks, dev):
    app.include_router(r.router)


@app.get("/health")
def health():
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok", "llm_provider": settings.llm_provider, "embedding_provider": settings.embedding_provider,
            "whatsapp_force_dev": settings.whatsapp_force_dev}
