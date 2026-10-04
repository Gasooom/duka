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

# Fulfilment status. 'pending' = confirmed by the customer, waiting for the owner's review.
ORDER_STATUSES = ("pending", "accepted", "ready", "out_for_delivery", "delivered", "cancelled")
# Money, tracked separately: an owner can accept an unpaid (e.g. cash on delivery) order, and a payment can be
# recorded after delivery. 'pending' = a provider request or a customer-reported reference awaits confirmation.
ORDER_PAYMENT_STATUSES = ("unpaid", "pending", "paid")
PAYMENT_STATUSES = ("pending", "successful", "failed", "cancelled", "voided")


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
    # Server-prepared order summary awaiting the customer's explicit YES (see CheckoutService). None otherwise.
    checkout: Mapped[dict | None] = mapped_column(JSONB)

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
    payment_status: Mapped[str] = mapped_column(String(12), default="unpaid", server_default="unpaid", nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Evidence of the explicit confirmation: the customer message that said YES to the summary.
    confirmation_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)

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
    """provider: manual | mock | momo. A successful payment is confirmed either by the provider
    (confirmation_source='provider') or by an owner's audited manual record ('owner'). Never by the agent."""

    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("provider", "provider_reference"),
        # The same MoMo transaction id / receipt cannot settle two orders of one business.
        Index("uq_payments_external_reference", "business_id", "external_reference", unique=True,
              postgresql_where=text("status = 'successful' AND external_reference IS NOT NULL")),
    )

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
    method: Mapped[str | None] = mapped_column(String(20))  # manual: momo | cash | bank | other
    external_reference: Mapped[str | None] = mapped_column(String(128))  # e.g. MoMo transaction id
    confirmation_source: Mapped[str | None] = mapped_column(String(10))  # provider | owner
    confirmed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    note: Mapped[str | None] = mapped_column(Text)
