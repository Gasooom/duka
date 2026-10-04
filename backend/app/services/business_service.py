"""Business onboarding, authentication and tenant configuration."""
import re
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from app.core.security import create_access_token, encrypt_secret, hash_password, verify_password
from app.models import AgentConfig, Business, BusinessSettings, User, WhatsAppAccount
from app.repositories.repos import AgentConfigRepo, DeliveryZoneRepo, SettingsRepo, WhatsAppAccountRepo

BUSINESS_FIELDS = {
    "name", "description", "business_type", "logo_url", "phone", "address", "currency", "timezone", "language",
    "business_hours", "delivery_enabled", "payment_enabled", "human_handoff_enabled", "order_prefix",
}
AGENT_FIELDS = {"system_prompt", "tone", "language", "greeting", "fallback_message", "business_rules", "model",
                "temperature", "max_history_messages"}
SETTINGS_FIELDS = {"payment_provider", "payment_instructions", "owner_notification_phone",
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
    email = email.strip().lower()
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
    token = create_access_token(user.id, business.id, user.role)
    return business, user, token


def authenticate(db: Session, email: str, password: str) -> tuple[User, str]:
    user = db.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        raise PermissionDenied("Invalid email or password", code="invalid_credentials")
    return user, create_access_token(user.id, user.business_id, user.role)


def get_business(db: Session, business_id: uuid.UUID) -> Business:
    business = db.get(Business, business_id)
    if not business:
        raise NotFoundError("Business not found")
    return business


class BusinessConfigService:
    def __init__(self, db: Session, business_id: uuid.UUID):
        self.db = db
        self.business_id = business_id

    @property
    def business(self) -> Business:
        return get_business(self.db, self.business_id)

    def update_business(self, data: dict[str, Any]) -> Business:
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
        for k, v in data.items():
            if k in AGENT_FIELDS:
                setattr(cfg, k, Decimal(str(v)) if k == "temperature" and v is not None else v)
        self.db.flush()
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
        for k, v in data.items():
            if k in SETTINGS_FIELDS and v is not None:
                setattr(s, k, (v.strip() or None) if isinstance(v, str) and k != "payment_provider" else v)
        self.db.flush()
        return s

    # WhatsApp ---------------------------------------------------------
    def connect_whatsapp(self, *, phone_number_id: str, display_phone_number: str | None, waba_id: str | None,
                         access_token: str | None, mode: str) -> WhatsAppAccount:
        if mode not in ("cloud", "dev"):
            raise ValidationError("mode must be 'cloud' or 'dev'")
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
        acct = other or repo.add(phone_number_id=phone_number_id, mode=mode)
        repo.update(acct, display_phone_number=display_phone_number, waba_id=waba_id, mode=mode, is_active=True)
        if access_token:
            acct.access_token_encrypted = encrypt_secret(access_token)
        self.db.flush()
        return acct

    def whatsapp_accounts(self) -> list[WhatsAppAccount]:
        return WhatsAppAccountRepo(self.db, self.business_id).list(order_by=[WhatsAppAccount.created_at])

    def delete_whatsapp(self, account_id: uuid.UUID) -> None:
        repo = WhatsAppAccountRepo(self.db, self.business_id)
        repo.delete(repo.get_or_404(account_id))

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
