import math

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import TenantContext, client_ip, get_tenant
from app.core.config import settings
from app.core.errors import PermissionDenied
from app.core.logging import get_logger, log_event
from app.core.ratelimit import auth_limiter, login_backoff
from app.core.security import create_access_token, pseudonym
from app.db.session import get_db
from app.schemas.api import ChangePasswordIn, LoginIn, RegisterIn, TokenOut
from app.services import business_service

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = get_logger(__name__)


def _limit(request: Request) -> str:
    """Per client address (from TRUSTED_PROXY_HOPS, so a forged X-Forwarded-For gets no fresh allowance)."""
    ip = client_ip(request)
    if not auth_limiter.allow(ip):
        raise HTTPException(429, "Too many attempts, try again in a minute")
    return ip


def _account_backoff(account: str, ip: str, operation: str) -> None:
    """Per account, whoever is asking: after repeated wrong passwords the account is locked for a growing time.
    Unknown emails are treated exactly like real ones, so this reveals nothing about which accounts exist."""
    wait = login_backoff.retry_after(account)
    if wait:
        retry_after = math.ceil(wait)
        log_event(logger, "auth.login_throttled", 30, operation=operation, status="throttled",
                  account=pseudonym(account), client_ip=ip, retry_after=retry_after)
        minutes = math.ceil(retry_after / 60)
        raise HTTPException(429, f"Too many failed sign-in attempts. Try again in {minutes} minute"
                                 f"{'s' if minutes != 1 else ''}.", headers={"Retry-After": str(retry_after)})


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
    ip = _limit(request)
    account = body.email.strip().lower()
    _account_backoff(account, ip, "auth.login")
    try:
        user, token = business_service.authenticate(db, body.email, body.password)
    except PermissionDenied:
        failures, lockout = login_backoff.failure(account)
        business_service.record_failed_login(db, account, client_ip=ip, failures=failures, lockout_seconds=lockout)
        db.commit()
        raise
    login_backoff.success(account)
    return TokenOut(access_token=token, business_id=user.business_id, user=_user(user))


@router.get("/me")
def me(ctx: TenantContext = Depends(get_tenant)):
    return {"user": _user(ctx.user), "business": {"id": str(ctx.business.id), "name": ctx.business.name,
                                                   "currency": ctx.business.currency},
            "features": {"dev_tools": settings.enable_dev_tools and not settings.is_production,
                         "registration_open": settings.registration_open}}


@router.post("/change-password", response_model=TokenOut)
def change_password(body: ChangePasswordIn, request: Request, ctx: TenantContext = Depends(get_tenant)):
    """Other devices are signed out; this one gets a new token. Wrong current passwords count against the account
    like failed sign-ins (a stolen session must not become a password-guessing oracle)."""
    ip = _limit(request)
    account = ctx.user.email.strip().lower()
    _account_backoff(account, ip, "auth.change_password")
    try:
        business_service.change_password(ctx.db, ctx.user, body.current_password, body.new_password)
    except PermissionDenied:
        failures, lockout = login_backoff.failure(account)
        log_event(logger, "auth.password_change_failed", 30, operation="auth.change_password", status="rejected",
                  account=pseudonym(account), client_ip=ip, failures=failures, lockout_seconds=round(lockout))
        raise
    ctx.db.commit()
    login_backoff.success(account)
    token = create_access_token(ctx.user.id, ctx.business_id, ctx.user.role, ctx.user.token_version)
    return TokenOut(access_token=token, business_id=ctx.business_id, user=_user(ctx.user))
