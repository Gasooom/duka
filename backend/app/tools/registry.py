"""Tool registry: typed args (pydantic), JSON schemas for the LLM, safe execution.

The agent never touches the database. It can only call these tools, and tools call services.
Every execution runs inside a SAVEPOINT so a failing tool cannot leave partial writes."""
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.core.logging import get_logger, log_event
from app.models import Business, Conversation, Customer

logger = get_logger(__name__)


@dataclass
class ToolContext:
    db: Session
    business: Business
    customer: Customer
    conversation: Conversation

    @property
    def business_id(self) -> uuid.UUID:
        return self.business.id


@dataclass
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    fn: Callable[[ToolContext, Any], dict[str, Any]]
    mutates: bool = False
    # Optional predicate: is this tool enabled for the business configuration?
    enabled_if: Callable[[Business], bool] | None = None

    def schema(self) -> dict[str, Any]:
        params = self.args_model.model_json_schema()
        params.pop("title", None)
        for prop in params.get("properties", {}).values():
            prop.pop("title", None)
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": params}}


TOOLS: dict[str, Tool] = {}


def register(tool: Tool) -> Tool:
    TOOLS[tool.name] = tool
    return tool


def tools_for(business: Business) -> list[Tool]:
    return [t for t in TOOLS.values() if t.enabled_if is None or t.enabled_if(business)]


def execute_tool(ctx: ToolContext, name: str, raw_args: dict[str, Any] | None) -> tuple[dict[str, Any], float]:
    """Returns (result, latency_ms). Result always has ok: bool."""
    start = time.perf_counter()
    tool = TOOLS.get(name)
    enabled = {t.name for t in tools_for(ctx.business)}
    if tool is None or name not in enabled:
        return {"ok": False, "error": f"Unknown or disabled tool '{name}'"}, 0.0
    try:
        args = tool.args_model.model_validate(raw_args or {})
    except PydanticValidationError as exc:
        errs = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        return {"ok": False, "error": f"Invalid arguments: {errs}"}, (time.perf_counter() - start) * 1000
    nested = ctx.db.begin_nested()
    try:
        data = tool.fn(ctx, args)
        nested.commit()
        result = {"ok": True, **data}
    except DomainError as exc:
        nested.rollback()
        result = {"ok": False, "error": exc.message}
    except Exception as exc:  # never let a tool crash the conversation
        nested.rollback()
        log_event(logger, "tool.crash", 40, operation=f"tool.{name}", status="error", error=repr(exc)[:300])
        result = {"ok": False, "error": "Internal error while running the tool"}
    latency = (time.perf_counter() - start) * 1000
    log_event(logger, f"tool.{name}", operation=f"tool.{name}", status="ok" if result["ok"] else "error",
              duration_ms=round(latency, 1))
    return result, latency
