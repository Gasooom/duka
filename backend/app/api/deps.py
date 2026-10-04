import uuid
from dataclasses import dataclass

import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.core.logging import bind_context
from app.core.security import decode_access_token
from app.db.session import get_db
from app.models import Business, User


@dataclass
class TenantContext:
    """Resolved from the JWT. Every admin route receives its tenant this way, never from input."""
    db: Session
    user: User
    business: Business

    @property
    def business_id(self) -> uuid.UUID:
        return self.business.id


def get_tenant(authorization: str | None = Header(None), db: Session = Depends(get_db)) -> TenantContext:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        payload = decode_access_token(authorization.split(" ", 1)[1])
        user_id = uuid.UUID(payload["sub"])
        business_id = uuid.UUID(payload["bid"])
        token_version = int(payload.get("tv", 0))
    except (jwt.PyJWTError, KeyError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    user = db.get(User, user_id)
    if not user or not user.is_active or user.business_id != business_id or user.token_version != token_version:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    business = db.get(Business, business_id)
    if not business or not business.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Business inactive")
    bind_context(business_id=business.id)
    return TenantContext(db=db, user=user, business=business)


def require_owner(ctx: TenantContext = Depends(get_tenant)) -> TenantContext:
    if ctx.user.role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Owner role required")
    return ctx
