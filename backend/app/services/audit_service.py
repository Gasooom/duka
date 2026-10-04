"""Append-only audit trail for sensitive actions (the table rejects UPDATE at the database level)."""
import uuid
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


def for_entity(db: Session, business_id: uuid.UUID, entity_id: uuid.UUID) -> list[AuditEvent]:
    return AuditEventRepo(db, business_id).list(where=[AuditEvent.entity_id == entity_id],
                                                order_by=[AuditEvent.created_at])
