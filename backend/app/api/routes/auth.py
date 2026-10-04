from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import TenantContext, get_tenant
from app.core.config import settings
from app.core.ratelimit import auth_limiter
from app.core.security import create_access_token
from app.db.session import get_db
from app.schemas.api import ChangePasswordIn, LoginIn, RegisterIn, TokenOut
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
    if not settings.registration_open:
        raise HTTPException(403, "Public registration is closed. Contact the Duka team to onboard your business.")
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
                                                   "currency": ctx.business.currency},
            "features": {"dev_tools": settings.enable_dev_tools and not settings.is_production,
                         "registration_open": settings.registration_open}}


@router.post("/change-password", response_model=TokenOut)
def change_password(body: ChangePasswordIn, request: Request, ctx: TenantContext = Depends(get_tenant)):
    """Other devices are signed out; this one gets a new token."""
    _limit(request)
    business_service.change_password(ctx.db, ctx.user, body.current_password, body.new_password)
    ctx.db.commit()
    token = create_access_token(ctx.user.id, ctx.business_id, ctx.user.role, ctx.user.token_version)
    return TokenOut(access_token=token, business_id=ctx.business_id, user=_user(ctx.user))
