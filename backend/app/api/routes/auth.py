from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import TenantContext, get_tenant
from app.core.ratelimit import auth_limiter
from app.db.session import get_db
from app.schemas.api import LoginIn, RegisterIn, TokenOut
from app.services import business_service

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _limit(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    if not auth_limiter.allow(ip):
        raise HTTPException(429, "Too many attempts, try again in a minute")


def _user(u) -> dict:
    return {"id": str(u.id), "email": u.email, "full_name": u.full_name, "role": u.role}


@router.post("/register", response_model=TokenOut, status_code=201)
def register(body: RegisterIn, request: Request, db: Session = Depends(get_db)):
    _limit(request)
    business, user, token = business_service.register_business(
        db, business_name=body.business_name, email=body.email, password=body.password, full_name=body.full_name,
        business_type=body.business_type, currency=body.currency)
    db.commit()
    return TokenOut(access_token=token, business_id=business.id, user=_user(user))


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    _limit(request)
    user, token = business_service.authenticate(db, body.email, body.password)
    return TokenOut(access_token=token, business_id=user.business_id, user=_user(user))


@router.get("/me")
def me(ctx: TenantContext = Depends(get_tenant)):
    return {"user": _user(ctx.user), "business": {"id": str(ctx.business.id), "name": ctx.business.name,
                                                   "currency": ctx.business.currency}}
