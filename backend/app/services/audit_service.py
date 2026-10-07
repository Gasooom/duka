"""Append-only audit trail for sensitive actions (the table rejects UPDATE at the database level).

Never record a secret: passwords, access tokens and API keys are not passed in here — callers record that a
credential was set or replaced, not its value."""
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditEvent, User
from app.repositories.repos import AuditEventRepo


def record(db: Session, business_id: uuid.UUID, action: str, entity_type: str, entity_id: uuid.UUID | None, *,
           user: User | None = None, actor_type: str | None = None, **data: Any) -> AuditEvent:
    return AuditEventRepo(db, business_id).add(
        actor_type=actor_type or ("user" if user else "system"), actor_user_id=user.id if user else None,
        action=action, entity_type=entity_type, entity_id=entity_id,
        data={"actor_email": user.email, **data} if user else data)


def plain(value: Any) -> Any:
    """A JSON-safe copy of a column value (Decimal prices, timestamps, ids)."""
    if isinstance(value, Decimal | uuid.UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{field: {"from": old, "to": new}} for every field whose value differs."""
    return {k: {"from": plain(before.get(k)), "to": plain(v)} for k, v in after.items() if before.get(k) != v}


def for_entity(db: Session, business_id: uuid.UUID, entity_id: uuid.UUID) -> list[AuditEvent]:
    return AuditEventRepo(db, business_id).list(where=[AuditEvent.entity_id == entity_id],
                                                order_by=[AuditEvent.created_at])
