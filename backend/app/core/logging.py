"""Structured JSON logging with request/tenant context propagated via contextvars."""
import json
import logging
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
business_id_var: ContextVar[str | None] = ContextVar("business_id", default=None)
customer_id_var: ContextVar[str | None] = ContextVar("customer_id", default=None)
conversation_id_var: ContextVar[str | None] = ContextVar("conversation_id", default=None)

_SECRET_KEYS = {"password", "token", "access_token", "api_key", "secret", "authorization", "password_hash"}


def _redact(data: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in data.items():
        if any(s in k.lower() for s in _SECRET_KEYS):
            out[k] = "***"
        else:
            out[k] = v
    return out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
            "business_id": business_id_var.get(),
            "customer_id": customer_id_var.get(),
            "conversation_id": conversation_id_var.get(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(_redact(extra))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps({k: v for k, v in payload.items() if v is not None}, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    logging.getLogger("uvicorn.access").setLevel("WARNING")
    logging.getLogger("httpx").setLevel("WARNING")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, msg: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, msg, extra={"extra_fields": fields})


@contextmanager
def log_operation(logger: logging.Logger, operation: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Log an operation with status + duration. Yields a dict callers can add fields to."""
    start = time.perf_counter()
    ctx: dict[str, Any] = dict(fields)
    try:
        yield ctx
    except Exception as exc:
        log_event(logger, operation, logging.ERROR, operation=operation, status="error",
                  error=str(exc)[:500], duration_ms=round((time.perf_counter() - start) * 1000, 1), **ctx)
        raise
    else:
        log_event(logger, operation, operation=operation, status=ctx.pop("status", "ok"),
                  duration_ms=round((time.perf_counter() - start) * 1000, 1), **ctx)


def bind_context(*, business_id: Any = None, customer_id: Any = None, conversation_id: Any = None) -> None:
    if business_id is not None:
        business_id_var.set(str(business_id))
    if customer_id is not None:
        customer_id_var.set(str(customer_id))
    if conversation_id is not None:
        conversation_id_var.set(str(conversation_id))


def clear_context() -> None:
    for var in (business_id_var, customer_id_var, conversation_id_var):
        var.set(None)
