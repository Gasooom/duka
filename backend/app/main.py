import hmac
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from app import ops
from app.api.middleware import SecurityMiddleware
from app.api.routes import auth, business, crm, dashboard, dev, knowledge, orders, products, usage, webhooks
from app.core.config import settings
from app.core.errors import DomainError
from app.core.logging import clear_context, configure_logging, get_logger, log_event, request_id_var, safe_error
from app.db.session import SessionLocal
from app.services.pricing import current_price_list
from app.workflows.worker import workers

configure_logging(settings.log_level)
logger = get_logger("app")
access_logger = get_logger("app.access")

if settings.is_production and settings.production_problems():
    raise RuntimeError("Refusing to start in production:\n- " + "\n- ".join(settings.production_problems()))
if settings.usage_pricing_file:
    current_price_list()  # refuse to start with a broken price list (PricingError says why), not record usage unpriced


@asynccontextmanager
async def lifespan(_: FastAPI):
    workers.start(settings.background_workers)
    yield
    workers.stop()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan,
              description="Multi-tenant WhatsApp AI commerce platform. See /docs for the API contract.",
              docs_url=None if settings.is_production else "/docs",
              redoc_url=None if settings.is_production else "/redoc",
              openapi_url=None if settings.is_production else "/openapi.json")
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
# Outside CORS (preflights get the headers too), inside request_context (a 413 still has a request id and is logged).
app.add_middleware(SecurityMiddleware, max_body_bytes=settings.max_request_body_bytes, hsts=settings.is_production)
_QUIET_PATHS = {"/healthz", "/readyz", "/health", "/metrics"}


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    request_id_var.set(rid)
    clear_context()
    start = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    if request.url.path not in _QUIET_PATHS:
        # Path only: query strings can carry secrets (e.g. Meta's hub.verify_token).
        log_event(access_logger, "http.request", 30 if response.status_code >= 500 else 20, operation="http",
                  method=request.method, path=request.url.path, status=response.status_code,
                  duration_ms=round((time.perf_counter() - start) * 1000, 1))
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
    log_event(logger, "unhandled_error", 40, operation=request.url.path, status="error", error=safe_error(exc))
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


for r in (auth, business, products, orders, crm, knowledge, dashboard, usage, webhooks, dev):
    app.include_router(r.router)


# ---------------------------------------------------------------- operations
def _ops_allowed(request: Request) -> bool:
    """Details/metrics need `Authorization: Bearer <OPS_TOKEN>`; without a token configured they are only
    available outside production."""
    if not settings.ops_token:
        return not settings.is_production
    supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    return hmac.compare_digest(supplied, settings.ops_token)


@app.get("/healthz")
def healthz():
    """Liveness: the process is serving requests (no dependencies checked)."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz(request: Request, details: bool = False):
    """Readiness for load balancers and uptime monitors: 503 when customers are affected."""
    with SessionLocal() as db:
        level, checks = ops.readiness(db, workers.running)
    body: dict = {"status": level}
    if details and _ops_allowed(request):
        body["checks"] = [c.__dict__ for c in checks]
    return JSONResponse(status_code=503 if level == "down" else 200, content=body)


@app.get("/health")
def health(request: Request):
    """Kept for existing monitors; same as /readyz."""
    return readyz(request)


@app.get("/metrics", response_class=PlainTextResponse)
def metrics(request: Request):
    if not _ops_allowed(request):
        return PlainTextResponse("Not found", status_code=404)
    with SessionLocal() as db:
        return ops.metrics(db, workers.running)
