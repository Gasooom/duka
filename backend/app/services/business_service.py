"""Business onboarding, authentication and tenant configuration."""
import re
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from app.core.logging import get_logger, log_event
from app.core.security import (
    create_access_token,
    encrypt_secret,
    hash_password,
    pseudonym,
    verify_password,
    verify_password_or_dummy,
)
from app.models import AgentConfig, Business, BusinessSettings, User, WhatsAppAccount
from app.repositories.repos import AgentConfigRepo, DeliveryZoneRepo, SettingsRepo, WhatsAppAccountRepo
from app.services import audit_service

logger = get_logger(__name__)

BUSINESS_FIELDS = {
    "name", "description", "business_type", "logo_url", "phone", "address", "currency", "timezone", "language",
    "business_hours", "delivery_enabled", "payment_enabled", "human_handoff_enabled", "order_prefix",
}
AGENT_FIELDS = {"system_prompt", "tone", "language", "greeting", "fallback_message", "business_rules", "model",
                "temperature", "max_history_messages"}
SETTINGS_FIELDS = {"ai_enabled", "payment_provider", "payment_instructions", "owner_notification_phone",
                   "owner_notification_template", "owner_notification_template_language", "low_stock_threshold",
                   "max_order_quantity"}


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "business"
    return slug


def _order_prefix(name: str) -> str:
    letters = "".join(w[0] for w in re.findall(r"[A-Za-z]+", re.sub(r"['’]", "", name)))[:3].upper()
    return letters or "ORD"


def register_business(db: Session, *, business_name: str, email: str, password: str, full_name: str | None = None,
                      business_type: str = "retail", currency: str = "RWF") -> tuple[Business, User, str]:
    from email_validator import EmailNotValidError, validate_email
    try:  # same rules as the login API, so no account is created that can never sign in
        email = validate_email(email.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError as exc:
        raise ValidationError(f"Invalid email: {exc}")
    if len(password) < 8:
        raise ValidationError("Password must be at least 8 characters")
    if db.scalar(select(User).where(func.lower(User.email) == email)):
        raise ConflictError("An account with this email already exists")
    slug = _slugify(business_name)
    if db.scalar(select(Business).where(Business.slug == slug)):
        slug = f"{slug}-{uuid.uuid4().hex[:6]}"
    business = Business(name=business_name.strip(), slug=slug, business_type=business_type,
                        currency=currency.upper(), order_prefix=_order_prefix(business_name))
    db.add(business)
    db.flush()
    db.add(BusinessSettings(business_id=business.id))
    db.add(AgentConfig(business_id=business.id,
                       greeting=f"Hello! Welcome to {business.name}. How can I help you today?"))
    user = User(business_id=business.id, email=email, password_hash=hash_password(password),
                full_name=full_name, role="owner")
    db.add(user)
    db.flush()
    token = create_access_token(user.id, business.id, user.role, user.token_version or 0)
    return business, user, token


def find_user(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))


def authenticate(db: Session, email: str, password: str) -> tuple[User, str]:
    user = find_user(db, email)
    valid = verify_password_or_dummy(password, user.password_hash if user else None)
    if not user or not user.is_active or not valid:
        raise PermissionDenied("Invalid email or password", code="invalid_credentials")
    return user, create_access_token(user.id, user.business_id, user.role, user.token_version)


def record_failed_login(db: Session, email: str, *, client_ip: str, failures: int, lockout_seconds: float) -> None:
    """Security trail of a failed sign-in: a log line for every attempt (the email only as a pseudonym, never the
    password) and, when the email belongs to an account, an audit event in that account's business. The caller
    commits."""
    user = find_user(db, email)
    log_event(logger, "auth.login_failed", 30, operation="auth.login", status="rejected", account=pseudonym(email),
              known_account=user is not None, client_ip=client_ip, failures=failures,
              lockout_seconds=round(lockout_seconds))
    if user is not None:
        audit_service.record(db, user.business_id, "auth.login_failed", "user", user.id, actor_type="anonymous",
                             client_ip=client_ip, failures=failures, lockout_seconds=round(lockout_seconds))


def change_password(db: Session, user: User, current: str, new: str) -> None:
    if not verify_password(current, user.password_hash):
        raise PermissionDenied("Current password is incorrect", code="invalid_credentials")
    set_password(user, new)
    audit_service.record(db, user.business_id, "auth.password_changed", "user", user.id, user=user,
                         method="self_service", other_sessions_signed_out=True)


def set_password(user: User, new: str) -> None:
    if len(new) < 8:
        raise ValidationError("Password must be at least 8 characters")
    user.password_hash = hash_password(new)
    user.token_version = (user.token_version or 0) + 1  # sign out every existing session


def get_business(db: Session, business_id: uuid.UUID) -> Business:
    business = db.get(Business, business_id)
    if not business:
        raise NotFoundError("Business not found")
    return business


class BusinessConfigService:
    def __init__(self, db: Session, business_id: uuid.UUID, actor: User | None = None):
        self.db = db
        self.business_id = business_id
        self.actor = actor  # the signed-in user making the change (audit trail); None = system, e.g. the seed

    @property
    def business(self) -> Business:
        return get_business(self.db, self.business_id)

    def update_business(self, data: dict[str, Any]) -> Business:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        from app.services.hours import parse_hours
        if data.get("business_hours") is not None:
            try:
                parse_hours(data["business_hours"])
            except ValueError as exc:
                raise ValidationError(f"Business hours: {exc}")
        if data.get("timezone"):
            try:
                ZoneInfo(data["timezone"])
            except (ZoneInfoNotFoundError, ValueError):
                raise ValidationError(f"Unknown timezone '{data['timezone']}' (e.g. Africa/Kigali)")
        b = self.business
        for k, v in data.items():
            if k in BUSINESS_FIELDS and v is not None:
                setattr(b, k, v.upper() if k in ("currency", "order_prefix") else v)
        self.db.flush()
        return b

    def agent_config(self) -> AgentConfig:
        repo = AgentConfigRepo(self.db, self.business_id)
        cfg = repo.first()
        return cfg or repo.add()

    def update_agent_config(self, data: dict[str, Any]) -> AgentConfig:
        cfg = self.agent_config()
        before = {k: getattr(cfg, k) for k in AGENT_FIELDS}
        for k, v in data.items():
            if k in AGENT_FIELDS:
                setattr(cfg, k, Decimal(str(v)) if k == "temperature" and v is not None else v)
        self.db.flush()
        changed = audit_service.changes(before, {k: getattr(cfg, k) for k in AGENT_FIELDS})
        if changed:
            audit_service.record(self.db, self.business_id, "agent_config.updated", "agent_config", cfg.id,
                                 user=self.actor, changes=changed)
        return cfg

    def settings(self) -> BusinessSettings:
        repo = SettingsRepo(self.db, self.business_id)
        s = repo.first()
        return s or repo.add()

    def update_settings(self, data: dict[str, Any]) -> BusinessSettings:
        from app.core.config import settings as app_settings
        from app.services.conversation_service import normalize_phone
        s = self.settings()
        provider = data.get("payment_provider")
        if provider not in (None, "manual", "mock", "momo"):
            raise ValidationError("payment_provider must be 'manual', 'momo' or 'mock'")
        if provider == "mock" and app_settings.is_production:
            raise ValidationError("The mock payment provider is for development only")
        if provider == "momo" and not (app_settings.momo_subscription_key and app_settings.momo_api_user
                                       and app_settings.momo_api_key):
            raise ValidationError("MTN MoMo is not configured on this platform yet; use manual payments")
        if data.get("owner_notification_phone"):
            data["owner_notification_phone"] = normalize_phone(data["owner_notification_phone"])
        before = {k: getattr(s, k) for k in SETTINGS_FIELDS}
        for k, v in data.items():
            if k in SETTINGS_FIELDS and v is not None:
                setattr(s, k, (v.strip() or None) if isinstance(v, str) and k != "payment_provider" else v)
        self.db.flush()
        changed = audit_service.changes(before, {k: getattr(s, k) for k in SETTINGS_FIELDS})
        # Where customers are told to send money is what an attacker would change: its own, searchable event.
        instructions = changed.pop("payment_instructions", None)
        if instructions:
            audit_service.record(self.db, self.business_id, "settings.payment_instructions_changed",
                                 "business_settings", s.id, user=self.actor, **instructions)
        if changed:
            audit_service.record(self.db, self.business_id, "settings.updated", "business_settings", s.id,
                                 user=self.actor, changes=changed)
        return s

    # WhatsApp ---------------------------------------------------------
    def connect_whatsapp(self, *, phone_number_id: str, display_phone_number: str | None, waba_id: str | None,
                         access_token: str | None, mode: str) -> WhatsAppAccount:
        if mode not in ("cloud", "dev"):
            raise ValidationError("mode must be 'cloud' or 'dev'")
        from app.core.config import settings as app_settings
        if mode == "dev" and app_settings.is_production:
            raise ValidationError("Simulated (dev) WhatsApp numbers are not available in production")
        if mode == "cloud" and not access_token:
            existing = WhatsAppAccountRepo(self.db, self.business_id).first(
                WhatsAppAccount.phone_number_id == phone_number_id)
            if not existing or not existing.access_token_encrypted:
                raise ValidationError("access_token is required for cloud mode")
        # phone_number_id is globally unique: refuse to hijack another tenant's number
        other = self.db.scalar(select(WhatsAppAccount).where(WhatsAppAccount.phone_number_id == phone_number_id))
        if other and other.business_id != self.business_id:
            raise ConflictError("This WhatsApp phone number is already connected to another business")
        repo = WhatsAppAccountRepo(self.db, self.business_id)
        had_token = bool(other and other.access_token_encrypted)
        acct = other or repo.add(phone_number_id=phone_number_id, mode=mode)
        repo.update(acct, display_phone_number=display_phone_number, waba_id=waba_id, mode=mode, is_active=True)
        if access_token:
            acct.access_token_encrypted = encrypt_secret(access_token)
        self.db.flush()
        # Whether the access token was set or replaced, never the token (not even encrypted).
        audit_service.record(self.db, self.business_id, "whatsapp.connected", "whatsapp_account", acct.id,
                             user=self.actor, phone_number_id=phone_number_id,
                             display_phone_number=display_phone_number, mode=mode, reconnected=other is not None,
                             credential=("replaced" if had_token else "set") if access_token else "unchanged")
        return acct

    def whatsapp_accounts(self) -> list[WhatsAppAccount]:
        return WhatsAppAccountRepo(self.db, self.business_id).list(order_by=[WhatsAppAccount.created_at])

    def delete_whatsapp(self, account_id: uuid.UUID) -> None:
        repo = WhatsAppAccountRepo(self.db, self.business_id)
        acct = repo.get_or_404(account_id)
        audit_service.record(self.db, self.business_id, "whatsapp.disconnected", "whatsapp_account", acct.id,
                             user=self.actor, phone_number_id=acct.phone_number_id, mode=acct.mode)
        repo.delete(acct)

    # Delivery zones ------------------------------------------------------
    def delivery_zones(self):
        from app.models import DeliveryZone
        return DeliveryZoneRepo(self.db, self.business_id).list(order_by=[DeliveryZone.fee])

    def upsert_delivery_zone(self, data: dict[str, Any], zone_id: uuid.UUID | None = None):
        repo = DeliveryZoneRepo(self.db, self.business_id)
        areas = [a.strip() for a in (data.get("areas") or []) if a and a.strip()]
        fields = dict(name=data["name"].strip(), fee=Decimal(str(data["fee"])), areas=areas,
                      estimated_time=data.get("estimated_time"), is_default=bool(data.get("is_default")),
                      active=data.get("active", True))
        if fields["fee"] < 0:
            raise ValidationError("Delivery fee cannot be negative")
        if fields["is_default"]:
            for z in repo.list():
                if z.id != zone_id:
                    z.is_default = False
        if zone_id:
            return repo.update(repo.get_or_404(zone_id), **fields)
        return repo.add(**fields)

    def delete_delivery_zone(self, zone_id: uuid.UUID) -> None:
        repo = DeliveryZoneRepo(self.db, self.business_id)
        repo.delete(repo.get_or_404(zone_id))
