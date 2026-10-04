from decimal import Decimal

from sqlalchemy import Boolean, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class Business(IdMixin, TimestampMixin, Base):
    """The tenant. Everything else hangs off business_id."""

    __tablename__ = "businesses"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    business_type: Mapped[str] = mapped_column(String(50), default="retail", nullable=False)
    logo_url: Mapped[str | None] = mapped_column(String(500))
    phone: Mapped[str | None] = mapped_column(String(40))
    address: Mapped[str | None] = mapped_column(String(300))
    currency: Mapped[str] = mapped_column(String(3), default="RWF", nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Africa/Kigali", nullable=False)
    language: Mapped[str] = mapped_column(String(10), default="en", nullable=False)
    business_hours: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    order_prefix: Mapped[str] = mapped_column(String(6), default="ORD", nullable=False)
    delivery_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    payment_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    human_handoff_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    settings: Mapped["BusinessSettings"] = relationship(back_populates="business", uselist=False, lazy="joined")
    agent_config: Mapped["AgentConfig"] = relationship(back_populates="business", uselist=False, lazy="joined")


class User(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="owner", nullable=False)  # owner | staff
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class BusinessSettings(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "business_settings"
    __table_args__ = (UniqueConstraint("business_id"),)

    # manual (owner records payments; default) | momo | mock (development only)
    # Business-wide switch: when False the assistant sends nothing; messages wait for the team in the inbox.
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    payment_provider: Mapped[str] = mapped_column(String(30), default="manual", nullable=False)
    payment_instructions: Mapped[str | None] = mapped_column(Text)  # e.g. "MoMo 0788... (Name), send the ref"
    # Where new orders / handoffs are announced. Outside WhatsApp's 24h window Meta requires an approved
    # template: set its name (one body parameter = the notification text) and language.
    owner_notification_phone: Mapped[str | None] = mapped_column(String(32))
    owner_notification_template: Mapped[str | None] = mapped_column(String(100))
    owner_notification_template_language: Mapped[str] = mapped_column(String(10), default="en",
                                                                       server_default="en", nullable=False)
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    max_order_quantity: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    extra: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    business: Mapped[Business] = relationship(back_populates="settings")


class AgentConfig(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "agent_configs"
    __table_args__ = (UniqueConstraint("business_id"),)

    system_prompt: Mapped[str | None] = mapped_column(Text)
    tone: Mapped[str] = mapped_column(String(100), default="friendly and concise", nullable=False)
    language: Mapped[str] = mapped_column(String(10), default="en", nullable=False)
    greeting: Mapped[str] = mapped_column(Text, default="Hello! How can I help you today?", nullable=False)
    fallback_message: Mapped[str] = mapped_column(
        Text,
        default="Sorry, I'm having trouble processing that right now. Please try again or contact support.",
        nullable=False,
    )
    business_rules: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(100))  # optional per-tenant model override
    temperature: Mapped[Decimal] = mapped_column(Numeric(3, 2), default=Decimal("0.20"), nullable=False)
    max_history_messages: Mapped[int] = mapped_column(Integer, default=8, nullable=False)

    business: Mapped[Business] = relationship(back_populates="agent_config")


class WhatsAppAccount(IdMixin, TimestampMixin, TenantMixin, Base):
    """Maps a WhatsApp Cloud API phone_number_id to exactly one business."""

    __tablename__ = "whatsapp_accounts"

    phone_number_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_phone_number: Mapped[str | None] = mapped_column(String(40))
    waba_id: Mapped[str | None] = mapped_column(String(64))
    access_token_encrypted: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(10), default="dev", nullable=False)  # cloud | dev
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class DeliveryZone(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "delivery_zones"
    __table_args__ = (UniqueConstraint("business_id", "name"),)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    fee: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    areas: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    estimated_time: Mapped[str | None] = mapped_column(String(80))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

