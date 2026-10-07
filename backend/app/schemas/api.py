"""Pydantic request/response contracts for the admin API."""
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, computed_field, field_serializer, field_validator

from app.core.config import settings


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- auth
class RegisterIn(BaseModel):
    business_name: str = Field(..., min_length=2, max_length=200)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    full_name: str | None = Field(None, max_length=200)
    business_type: str = Field("retail", max_length=50)
    currency: str = Field("RWF", min_length=3, max_length=3)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(..., max_length=128)
    new_password: str = Field(..., min_length=8, max_length=128)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    business_id: uuid.UUID
    user: dict[str, Any]


# ---------------------------------------------------------------- business
class BusinessOut(ORM):
    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    business_type: str
    logo_url: str | None
    phone: str | None
    address: str | None
    currency: str
    timezone: str
    language: str
    business_hours: dict
    order_prefix: str
    delivery_enabled: bool
    payment_enabled: bool
    human_handoff_enabled: bool


class BusinessPatch(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=200)
    description: str | None = Field(None, max_length=2000)
    business_type: str | None = Field(None, max_length=50)
    logo_url: str | None = Field(None, max_length=500)
    phone: str | None = Field(None, max_length=40)
    address: str | None = Field(None, max_length=300)
    currency: str | None = Field(None, min_length=3, max_length=3)
    timezone: str | None = Field(None, max_length=64)
    language: str | None = Field(None, max_length=10)
    business_hours: dict[str, str] | None = None
    order_prefix: str | None = Field(None, pattern=r"^[A-Za-z]{1,6}$")
    delivery_enabled: bool | None = None
    payment_enabled: bool | None = None
    human_handoff_enabled: bool | None = None


class AgentConfigOut(ORM):
    system_prompt: str | None
    tone: str
    language: str
    greeting: str
    fallback_message: str
    business_rules: str | None
    model: str | None
    temperature: Decimal
    max_history_messages: int

    @field_serializer("temperature")
    def _t(self, v: Decimal) -> float:
        return float(v)

    @computed_field
    def default_model(self) -> str:
        return settings.llm_model

    @computed_field
    def available_models(self) -> list[str]:
        """The models this platform allows (LLM_MODEL + LLM_ALLOWED_MODELS); `model` must be one of them."""
        return settings.allowed_llm_models


class AgentConfigPatch(BaseModel):
    system_prompt: str | None = Field(None, max_length=4000)
    tone: str | None = Field(None, max_length=100)
    language: str | None = Field(None, max_length=10)
    greeting: str | None = Field(None, max_length=1000)
    fallback_message: str | None = Field(None, max_length=1000)
    business_rules: str | None = Field(None, max_length=4000)
    model: str | None = Field(None, max_length=100)
    temperature: float | None = Field(None, ge=0, le=1.5)
    max_history_messages: int | None = Field(None, ge=2, le=30)

    @field_validator("model")
    @classmethod
    def _allowed_model(cls, v: str | None) -> str | None:
        """Only models the platform operator allows: a typo or an expensive model is refused when the setting is
        saved, not discovered as failed replies. Blank = the platform default."""
        v = (v or "").strip() or None
        if v is not None and v not in settings.allowed_llm_models:
            raise ValueError(f"Model '{v}' is not available on this platform. "
                             f"Choose one of: {', '.join(settings.allowed_llm_models)} (or leave blank for the default)")
        return v


class SettingsOut(ORM):
    ai_enabled: bool
    payment_provider: str
    payment_instructions: str | None
    owner_notification_phone: str | None
    owner_notification_template: str | None
    owner_notification_template_language: str
    low_stock_threshold: int
    max_order_quantity: int


class SettingsPatch(BaseModel):
    ai_enabled: bool | None = None
    payment_provider: str | None = Field(None, pattern=r"^(manual|mock|momo)$")
    payment_instructions: str | None = Field(None, max_length=1000)
    owner_notification_phone: str | None = Field(None, max_length=32)
    owner_notification_template: str | None = Field(None, max_length=100, pattern=r"^[a-z0-9_]*$")
    owner_notification_template_language: str | None = Field(None, max_length=10)
    low_stock_threshold: int | None = Field(None, ge=0, le=10000)
    max_order_quantity: int | None = Field(None, ge=1, le=1000)


class DeliveryZoneIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    fee: Decimal = Field(..., ge=0)
    areas: list[str] = []
    estimated_time: str | None = Field(None, max_length=80)
    is_default: bool = False
    active: bool = True


class DeliveryZoneOut(ORM):
    id: uuid.UUID
    name: str
    fee: Decimal
    areas: list[str]
    estimated_time: str | None
    is_default: bool
    active: bool

    @field_serializer("fee")
    def _f(self, v: Decimal) -> float:
        return float(v)


class WhatsAppConnectIn(BaseModel):
    phone_number_id: str = Field(..., min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    display_phone_number: str | None = Field(None, max_length=40)
    waba_id: str | None = Field(None, max_length=64)
    access_token: str | None = Field(None, max_length=1000)
    mode: str = Field("dev", pattern=r"^(cloud|dev)$")


class WhatsAppAccountOut(ORM):
    id: uuid.UUID
    phone_number_id: str
    display_phone_number: str | None
    waba_id: str | None
    mode: str
    is_active: bool
    has_access_token: bool = False


# ---------------------------------------------------------------- products
class ProductIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(None, max_length=5000)
    price: Decimal = Field(..., ge=0)
    currency: str | None = Field(None, min_length=3, max_length=3)
    sku: str | None = Field(None, max_length=80)
    category: str | None = Field(None, max_length=120)
    stock_quantity: int = Field(0, ge=0)
    image_url: str | None = Field(None, max_length=500)
    active: bool = True
    metadata: dict[str, Any] | None = None


class ProductPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=5000)
    price: Decimal | None = Field(None, ge=0)
    sku: str | None = Field(None, max_length=80)
    category: str | None = Field(None, max_length=120)
    stock_quantity: int | None = Field(None, ge=0)
    image_url: str | None = Field(None, max_length=500)
    active: bool | None = None
    metadata: dict[str, Any] | None = None


class ProductOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    price: float
    currency: str
    sku: str
    category: str | None
    stock_quantity: int
    image_url: str | None
    active: bool
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, p) -> "ProductOut":
        return cls(id=p.id, name=p.name, description=p.description, price=float(p.price), currency=p.currency,
                   sku=p.sku, category=p.category.name if p.category else None, stock_quantity=p.stock_quantity,
                   image_url=p.image_url, active=p.active, metadata=p.attributes or {}, created_at=p.created_at,
                   updated_at=p.updated_at)


class StockAdjustIn(BaseModel):
    change: int = Field(..., ge=-100000, le=100000)
    reason: str = Field("adjustment", max_length=40)


# ---------------------------------------------------------------- crm
class CustomerOut(ORM):
    id: uuid.UUID
    whatsapp_number: str
    name: str | None
    email: str | None
    created_at: datetime


class MessageOut(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    metadata: dict[str, Any]
    delivery_status: str | None
    agent_run_id: uuid.UUID | None
    created_at: datetime

    @classmethod
    def of(cls, m) -> "MessageOut":
        return cls(id=m.id, role=m.role, content=m.content, metadata=m.attributes or {},
                   delivery_status=m.delivery_status, agent_run_id=m.agent_run_id, created_at=m.created_at)


class AgentRunOut(ORM):
    id: uuid.UUID
    trigger_message_id: uuid.UUID | None
    provider: str
    model: str | None
    status: str
    input_text: str | None
    steps: list
    response_text: str | None
    error: str | None
    latency_ms: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    llm_calls: int
    created_at: datetime


class HumanReplyIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


class SimulateIn(BaseModel):
    from_number: str = Field("250788000111", pattern=r"^\+?\d{7,15}$")
    text: str = Field(..., min_length=1, max_length=4000)
    name: str | None = Field("Test Customer", max_length=100)


# ---------------------------------------------------------------- orders
class OrderItemOut(ORM):
    product_id: uuid.UUID | None
    product_name: str
    sku: str | None
    unit_price: Decimal
    quantity: int
    subtotal: Decimal

    @field_serializer("unit_price", "subtotal")
    def _n(self, v: Decimal) -> float:
        return float(v)


class PaymentOut(ORM):
    id: uuid.UUID
    provider: str
    provider_reference: str
    amount: Decimal
    currency: str
    status: str
    payer_phone: str | None
    failure_reason: str | None
    created_at: datetime
    confirmed_at: datetime | None
    method: str | None
    external_reference: str | None
    confirmation_source: str | None  # provider | owner
    confirmed_by_user_id: uuid.UUID | None
    note: str | None

    @field_serializer("amount")
    def _n(self, v: Decimal) -> float:
        return float(v)


class OrderOut(ORM):
    id: uuid.UUID
    order_number: str
    customer_id: uuid.UUID
    conversation_id: uuid.UUID | None
    status: str
    currency: str
    subtotal: Decimal
    delivery_fee: Decimal
    discount: Decimal
    total: Decimal
    delivery_zone_name: str | None
    delivery_address: str | None
    notes: str | None
    created_at: datetime
    payment_status: str
    paid_at: datetime | None
    confirmed_at: datetime | None
    confirmation_message_id: uuid.UUID | None
    accepted_at: datetime | None
    cancelled_at: datetime | None
    cancel_reason: str | None
    items: list[OrderItemOut] = []

    @field_serializer("subtotal", "delivery_fee", "discount", "total")
    def _n(self, v: Decimal) -> float:
        return float(v)


class OrderStatusIn(BaseModel):
    status: str
    reason: str | None = Field(None, max_length=500)


class ManualPaymentIn(BaseModel):
    method: str = Field(..., pattern=r"^(momo|cash|bank|other)$")
    reference: str | None = Field(None, max_length=128)
    note: str | None = Field(None, max_length=500)


class VoidPaymentIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=500)


class ReturnToAiIn(BaseModel):
    message: str | None = Field(None, max_length=4000)


class AuditEventOut(ORM):
    id: uuid.UUID
    actor_type: str
    actor_user_id: uuid.UUID | None
    action: str
    entity_type: str
    entity_id: uuid.UUID | None
    data: dict[str, Any]
    created_at: datetime


class NotificationOut(ORM):
    id: uuid.UUID
    kind: str
    recipient: str | None
    body: str
    entity_type: str | None
    entity_id: uuid.UUID | None
    status: str
    error: str | None
    created_at: datetime


class SimulatePaymentIn(BaseModel):
    status: str = Field("successful", pattern=r"^(successful|failed)$")


# ---------------------------------------------------------------- knowledge
class KnowledgeIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1, max_length=200_000)


class KnowledgeOut(ORM):
    id: uuid.UUID
    title: str
    source_type: str
    chunk_count: int
    created_at: datetime
    content: str

