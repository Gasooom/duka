import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin

ORDER_STATUSES = (
    "pending", "awaiting_payment", "paid", "processing", "ready", "out_for_delivery", "delivered", "cancelled",
)
PAYMENT_STATUSES = ("pending", "successful", "failed", "cancelled")


class Cart(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "carts"
    # At most one active cart per customer, even under concurrent messages.
    __table_args__ = (Index("uq_carts_active", "business_id", "customer_id", unique=True,
                            postgresql_where=text("status = 'active'")),)

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)  # active|converted|abandoned
    delivery_zone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("delivery_zones.id", ondelete="SET NULL")
    )
    delivery_address: Mapped[str | None] = mapped_column(String(300))

    items: Mapped[list["CartItem"]] = relationship(
        back_populates="cart", cascade="all, delete-orphan", lazy="selectin", order_by="CartItem.created_at"
    )


class CartItem(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "cart_items"
    __table_args__ = (UniqueConstraint("cart_id", "product_id"), CheckConstraint("quantity > 0", name="ck_cart_qty"))

    cart_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("carts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    cart: Mapped[Cart] = relationship(back_populates="items")


class Order(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("business_id", "order_number"),)

    order_number: Mapped[str] = mapped_column(String(30), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    delivery_fee: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    discount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    delivery_zone_name: Mapped[str | None] = mapped_column(String(120))
    delivery_address: Mapped[str | None] = mapped_column(String(300))
    notes: Mapped[str | None] = mapped_column(Text)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan", lazy="selectin")


class OrderItem(IdMixin, TimestampMixin, TenantMixin, Base):
    """Snapshot of the product at purchase time; never re-read current product price."""

    __tablename__ = "order_items"

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), index=True, nullable=False
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id", ondelete="SET NULL"))
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    sku: Mapped[str | None] = mapped_column(String(80))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")


class Payment(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "payments"
    __table_args__ = (UniqueConstraint("provider", "provider_reference"),)

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    provider_reference: Mapped[str] = mapped_column(String(128), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    payer_phone: Mapped[str | None] = mapped_column(String(32))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
