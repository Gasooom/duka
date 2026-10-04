"""Deterministic commerce logic: delivery quotes, carts, totals, orders.

Nothing here trusts prices or totals from the LLM. Every number is read from the DB.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.models import Business, Cart, CartItem, Conversation, Customer, DeliveryZone, Order, Product
from app.repositories.repos import (
    CartItemRepo,
    CartRepo,
    DeliveryZoneRepo,
    InventoryRepo,
    OrderRepo,
    ProductRepo,
    SettingsRepo,
)

ZERO = Decimal("0")

ORDER_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"awaiting_payment", "processing", "cancelled"},
    "awaiting_payment": {"paid", "cancelled"},
    "paid": {"processing", "ready", "out_for_delivery"},
    "processing": {"ready", "out_for_delivery", "cancelled"},
    "ready": {"out_for_delivery", "delivered"},
    "out_for_delivery": {"delivered"},
    "delivered": set(),
    "cancelled": set(),
}
# 'paid' can only be set by a provider-confirmed payment, never by an admin or the agent.
ADMIN_FORBIDDEN_TARGETS = {"paid", "awaiting_payment"}


def money(v: Decimal | float | int) -> str:
    return f"{Decimal(v):,.0f}" if Decimal(v) == Decimal(v).to_integral() else f"{Decimal(v):,.2f}"


# ---------------------------------------------------------------- delivery
@dataclass
class DeliveryQuote:
    available: bool
    zone_id: str | None = None
    zone_name: str | None = None
    fee: Decimal = ZERO
    estimated_time: str | None = None
    matched_location: bool = False
    message: str | None = None


class DeliveryService:
    def __init__(self, db: Session, business_id: uuid.UUID):
        self.db = db
        self.business_id = business_id
        self.zones = DeliveryZoneRepo(db, business_id)

    def match_zone(self, location: str | None) -> tuple[DeliveryZone | None, bool]:
        active = self.zones.list(where=[DeliveryZone.active.is_(True)])
        if location:
            loc = f" {re.sub(r'[^a-z0-9]+', ' ', location.lower())} "
            best: tuple[int, DeliveryZone] | None = None
            for z in active:
                for term in [z.name, *z.areas]:
                    t = re.sub(r"[^a-z0-9]+", " ", term.lower()).strip()
                    if t and f" {t} " in loc and (best is None or len(t) > best[0]):
                        best = (len(t), z)
            if best:
                return best[1], True
        default = next((z for z in active if z.is_default), None)
        return default, False

    def quote(self, location: str | None) -> DeliveryQuote:
        business = self.db.get(Business, self.business_id)
        if not business.delivery_enabled:
            return DeliveryQuote(available=False, message="Delivery is not offered; orders are for pickup.")
        zone, matched = self.match_zone(location)
        if location and not matched:
            return DeliveryQuote(available=False, message=f"No delivery zone covers '{location}'. "
                                 f"Available zones: {', '.join(z.name for z in self.zones.list()) or 'none'}.")
        if zone is None:
            return DeliveryQuote(available=False, message="Please share your delivery location to calculate the fee.")
        return DeliveryQuote(available=True, zone_id=str(zone.id), zone_name=zone.name, fee=zone.fee,
                             estimated_time=zone.estimated_time, matched_location=matched)


# ---------------------------------------------------------------- cart
@dataclass
class CartLine:
    product_id: str
    name: str
    sku: str
    unit_price: Decimal
    quantity: int
    line_total: Decimal
    in_stock: bool
    available_stock: int


@dataclass
class CartTotals:
    cart_id: str
    currency: str
    lines: list[CartLine] = field(default_factory=list)
    subtotal: Decimal = ZERO
    delivery_fee: Decimal = ZERO
    discount: Decimal = ZERO
    total: Decimal = ZERO
    delivery_zone: str | None = None
    delivery_note: str | None = None
    issues: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("subtotal", "delivery_fee", "discount", "total"):
            d[k] = float(d[k])
        for line in d["lines"]:
            line["unit_price"] = float(line["unit_price"])
            line["line_total"] = float(line["line_total"])
        return d


class CartService:
    def __init__(self, db: Session, business_id: uuid.UUID):
        self.db = db
        self.business_id = business_id
        self.carts = CartRepo(db, business_id)
        self.items = CartItemRepo(db, business_id)
        self.products = ProductRepo(db, business_id)

    def _business(self) -> Business:
        return self.db.get(Business, self.business_id)

    def get_active(self, customer: Customer, conversation: Conversation | None = None, *, create: bool = True) -> Cart | None:
        cart = self.carts.first(Cart.customer_id == customer.id, Cart.status == "active")
        if cart is None and create:
            cart = self.carts.add(customer_id=customer.id, conversation_id=conversation.id if conversation else None,
                                  status="active")
        return cart

    def add_item(self, cart: Cart, product: Product, quantity: int = 1) -> CartItem:
        if product.business_id != self.business_id:
            raise NotFoundError("Product not found")
        if quantity < 1:
            raise ValidationError("Quantity must be at least 1")
        if not product.active:
            raise ValidationError(f"{product.name} is not available")
        settings = SettingsRepo(self.db, self.business_id).first()
        max_q = settings.max_order_quantity if settings else 20
        item = self.items.first(CartItem.cart_id == cart.id, CartItem.product_id == product.id)
        new_qty = (item.quantity if item else 0) + quantity
        if new_qty > max_q:
            raise ValidationError(f"Maximum {max_q} units per product")
        if new_qty > product.stock_quantity:
            raise ValidationError(f"Only {product.stock_quantity} unit(s) of {product.name} in stock")
        if item:
            item.quantity = new_qty
        else:
            item = self.items.add(cart_id=cart.id, product_id=product.id, quantity=quantity)
        self.db.flush()
        self.db.refresh(cart)
        return item

    def set_quantity(self, cart: Cart, product_id: uuid.UUID, quantity: int) -> None:
        item = self.items.first(CartItem.cart_id == cart.id, CartItem.product_id == product_id)
        if not item:
            raise NotFoundError("That product is not in the cart")
        if quantity <= 0:
            self.items.delete(item)
        else:
            product = self.products.get_or_404(product_id)
            if quantity > product.stock_quantity:
                raise ValidationError(f"Only {product.stock_quantity} unit(s) of {product.name} in stock")
            item.quantity = quantity
        self.db.flush()
        self.db.refresh(cart)

    def remove_item(self, cart: Cart, product_id: uuid.UUID) -> None:
        self.set_quantity(cart, product_id, 0)

    def clear(self, cart: Cart) -> None:
        for item in list(cart.items):
            self.items.delete(item)
        self.db.flush()
        self.db.refresh(cart)

    def set_delivery(self, cart: Cart, zone: DeliveryZone | None, address: str | None = None) -> None:
        cart.delivery_zone_id = zone.id if zone else None
        if address:
            cart.delivery_address = address[:300]
        self.db.flush()

    def totals(self, cart: Cart, *, delivery_location: str | None = None) -> CartTotals:
        business = self._business()
        t = CartTotals(cart_id=str(cart.id), currency=business.currency)
        for item in cart.items:
            p = self.products.get(item.product_id)
            if p is None:
                continue
            line_total = p.price * item.quantity
            ok = p.active and p.stock_quantity >= item.quantity
            t.lines.append(CartLine(product_id=str(p.id), name=p.name, sku=p.sku, unit_price=p.price,
                                    quantity=item.quantity, line_total=line_total, in_stock=ok,
                                    available_stock=p.stock_quantity))
            if not ok:
                t.issues.append(f"{p.name}: only {p.stock_quantity} in stock" if p.active else f"{p.name} is unavailable")
            t.subtotal += line_total
        if business.delivery_enabled and t.lines:
            delivery = DeliveryService(self.db, self.business_id)
            zone = None
            if delivery_location:
                q = delivery.quote(delivery_location)
                if q.available:
                    zone = delivery.zones.get(q.zone_id)
                    self.set_delivery(cart, zone, delivery_location)
                else:
                    t.delivery_note = q.message
            elif cart.delivery_zone_id:
                zone = delivery.zones.get(cart.delivery_zone_id)
            else:
                zone, _ = delivery.match_zone(None)
                if zone:
                    t.delivery_note = f"Assuming default delivery zone '{zone.name}'. Share your location for an exact fee."
            if zone:
                t.delivery_fee = zone.fee
                t.delivery_zone = zone.name
        t.total = t.subtotal + t.delivery_fee - t.discount
        return t


# ---------------------------------------------------------------- orders
class OrderService:
    def __init__(self, db: Session, business_id: uuid.UUID):
        self.db = db
        self.business_id = business_id
        self.orders = OrderRepo(db, business_id)
        self.products = ProductRepo(db, business_id)
        self.inventory = InventoryRepo(db, business_id)

    def _next_number(self, business: Business) -> str:
        # Lock the tenant row to serialise order-number allocation for this business only. NO KEY UPDATE, not
        # UPDATE: every insert in this transaction already holds FOR KEY SHARE on the business row (business_id
        # FK), and two checkouts upgrading those to FOR UPDATE deadlock each other.
        self.db.execute(select(Business.id).where(Business.id == business.id).with_for_update(key_share=True))
        n = self.orders.count() + 1
        while self.orders.first(Order.order_number == f"{business.order_prefix}-{n:05d}"):
            n += 1
        return f"{business.order_prefix}-{n:05d}"

    def create_from_cart(self, customer: Customer, conversation: Conversation | None, *,
                         delivery_location: str | None = None, notes: str | None = None) -> Order:
        business = self.db.get(Business, self.business_id)
        carts = CartService(self.db, self.business_id)
        cart = carts.get_active(customer, conversation, create=False)
        if cart is None or not cart.items:
            raise ValidationError("The cart is empty")
        totals = carts.totals(cart, delivery_location=delivery_location)
        if business.delivery_enabled and totals.delivery_zone is None:
            raise ValidationError(totals.delivery_note or "A delivery location is required before ordering")

        order = self.orders.add(
            order_number=self._next_number(business), customer_id=customer.id,
            conversation_id=conversation.id if conversation else None, status="pending", currency=business.currency,
            subtotal=ZERO, delivery_fee=totals.delivery_fee, discount=totals.discount, total=ZERO,
            delivery_zone_name=totals.delivery_zone, delivery_address=cart.delivery_address, notes=notes,
        )
        subtotal = ZERO
        from app.models import OrderItem
        # Lock product rows in a stable order to avoid deadlocks, then validate + decrement stock.
        product_ids = sorted(i.product_id for i in cart.items)
        locked = {p.id: p for p in self.db.scalars(
            self.products.query().where(Product.id.in_(product_ids)).order_by(Product.id).with_for_update(of=Product)).all()}
        for item in cart.items:
            p = locked.get(item.product_id)
            if p is None or not p.active:
                raise ValidationError("A product in the cart is no longer available")
            if p.stock_quantity < item.quantity:
                raise ConflictError(f"Only {p.stock_quantity} unit(s) of {p.name} left in stock")
            line = p.price * item.quantity
            self.db.add(OrderItem(business_id=self.business_id, order_id=order.id, product_id=p.id,
                                  product_name=p.name, sku=p.sku, unit_price=p.price, quantity=item.quantity,
                                  subtotal=line))
            p.stock_quantity -= item.quantity
            self.inventory.add(product_id=p.id, change=-item.quantity, balance_after=p.stock_quantity,
                               reason="order", reference=order.order_number)
            subtotal += line
        order.subtotal = subtotal
        order.total = subtotal + order.delivery_fee - order.discount
        cart.status = "converted"
        self.db.flush()
        self.db.refresh(order)
        return order

    def get(self, order_id: uuid.UUID) -> Order:
        return self.orders.get_or_404(order_id)

    def get_by_number(self, number: str, *, customer: Customer | None = None) -> Order:
        where = [func.upper(Order.order_number) == number.strip().upper()]
        if customer is not None:
            where.append(Order.customer_id == customer.id)
        order = self.orders.first(*where)
        if not order:
            raise NotFoundError(f"Order {number} not found")
        return order

    def for_customer(self, customer: Customer, limit: int = 10) -> list[Order]:
        return self.orders.list(where=[Order.customer_id == customer.id], order_by=[Order.created_at.desc()],
                                limit=limit)

    def latest_unpaid(self, customer: Customer) -> Order | None:
        rows = self.orders.list(where=[Order.customer_id == customer.id,
                                       Order.status.in_(("pending", "awaiting_payment"))],
                                order_by=[Order.created_at.desc()], limit=1)
        return rows[0] if rows else None

    def list(self, *, status: str | None = None, limit: int = 200) -> list[Order]:
        where = [Order.status == status] if status else []
        return self.orders.list(where=where, order_by=[Order.created_at.desc()], limit=limit)

    def transition(self, order: Order, new_status: str, *, actor: str = "system") -> Order:
        if new_status not in ORDER_TRANSITIONS:
            raise ValidationError(f"Unknown status '{new_status}'")
        if actor == "admin" and new_status in ADMIN_FORBIDDEN_TARGETS:
            raise ValidationError("'paid' can only be set by a confirmed payment")
        if new_status not in ORDER_TRANSITIONS[order.status]:
            raise ValidationError(f"Cannot change order from {order.status} to {new_status}")
        if new_status == "cancelled":
            self._restock(order)
        order.status = new_status
        if new_status == "paid":
            order.paid_at = datetime.now(timezone.utc)
        self.db.flush()
        return order

    def _restock(self, order: Order) -> None:
        for item in order.items:
            if item.product_id:
                p = self.products.get(item.product_id, for_update=True)
                if p:
                    p.stock_quantity += item.quantity
                    self.inventory.add(product_id=p.id, change=item.quantity, balance_after=p.stock_quantity,
                                       reason="order_cancelled", reference=order.order_number)

    def revenue(self) -> Decimal:
        stmt = self.orders.select(func.coalesce(func.sum(Order.total), 0)).where(
            Order.status.in_(("paid", "processing", "ready", "out_for_delivery", "delivered")))
        return Decimal(self.db.scalar(stmt) or 0)
