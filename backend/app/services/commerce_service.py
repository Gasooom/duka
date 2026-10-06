"""Deterministic commerce logic: delivery quotes, carts, totals, checkout confirmation, orders.

Nothing here trusts prices or totals from the LLM. Every number is read from the DB. No delivery zone or
address is ever assumed: an order needs the customer's own address, matched to a configured zone.

Order lifecycle:  cart -> CheckoutService.prepare() (server-rendered summary, fingerprinted)
                  -> customer replies YES in a later message -> CheckoutService.confirm() -> Order 'pending'
                  -> owner accepts -> ready / out_for_delivery -> delivered   (or cancelled)
Payment is tracked separately on Order.payment_status (see PaymentService).
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.models import Business, Cart, CartItem, Conversation, Customer, DeliveryZone, Message, Order, Product
from app.repositories.repos import (
    CartItemRepo,
    CartRepo,
    DeliveryZoneRepo,
    InventoryRepo,
    MessageRepo,
    OrderRepo,
    ProductRepo,
    SettingsRepo,
)

ZERO = Decimal("0")
CHECKOUT_TTL = timedelta(minutes=30)
# A YES only counts if the customer could actually have seen the summary.
DELIVERED_STATUSES = ("sent", "delivered", "read", "simulated")

ORDER_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"accepted", "cancelled"},            # waiting for the owner's review
    "accepted": {"ready", "out_for_delivery", "delivered", "cancelled"},
    "ready": {"out_for_delivery", "delivered", "cancelled"},
    "out_for_delivery": {"delivered"},
    "delivered": set(),
    "cancelled": set(),
}
OPEN_STATUSES = ("pending", "accepted", "ready", "out_for_delivery")


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
    code: str | None = None          # i18n error code for the customer-facing message
    params: dict = field(default_factory=dict)


class DeliveryService:
    def __init__(self, db: Session, business_id: uuid.UUID):
        self.db = db
        self.business_id = business_id
        self.zones = DeliveryZoneRepo(db, business_id)

    def match_zone(self, location: str | None) -> DeliveryZone | None:
        """The zone whose name or area appears in the location text (longest match). Never a default."""
        if not location:
            return None
        loc = f" {re.sub(r'[^a-z0-9]+', ' ', location.lower())} "
        best: tuple[int, DeliveryZone] | None = None
        for z in self.zones.list(where=[DeliveryZone.active.is_(True)]):
            for term in [z.name, *z.areas]:
                t = re.sub(r"[^a-z0-9]+", " ", term.lower()).strip()
                if t and f" {t} " in loc and (best is None or len(t) > best[0]):
                    best = (len(t), z)
        return best[1] if best else None

    def quote(self, location: str | None) -> DeliveryQuote:
        business = self.db.get(Business, self.business_id)
        if not business.delivery_enabled:
            return DeliveryQuote(available=False, message="Delivery is not offered; orders are for pickup.",
                                 code="pickup_only")
        if not location:
            return DeliveryQuote(available=False, message="Please share your delivery location to calculate the fee.",
                                 code="need_location")
        zone = self.match_zone(location)
        if zone is None:
            names = ", ".join(z.name for z in self.zones.list(where=[DeliveryZone.active.is_(True)])) or "none"
            return DeliveryQuote(available=False,
                                 message=f"No delivery zone covers '{location}'. Available zones: {names}.",
                                 code="no_delivery_zone", params={"location": location, "zones": names})
        return DeliveryQuote(available=True, zone_id=str(zone.id), zone_name=zone.name, fee=zone.fee,
                             estimated_time=zone.estimated_time, matched_location=True)


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
    # True when delivery applies but no zone is known yet: `total` then EXCLUDES delivery.
    delivery_pending: bool = False
    delivery_note_code: str | None = None     # i18n code of delivery_note ("delivery_pending" or a quote code)
    delivery_note_params: dict = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    issue_details: list[dict] = field(default_factory=list)  # [{"name", "qty", "active"}] for localised rendering

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
        where = (Cart.customer_id == customer.id, Cart.status == "active")
        cart = self.carts.first(*where)
        if cart is None and create:
            # Race-safe: the partial unique index uq_carts_active allows one active cart per customer.
            self.db.execute(pg_insert(Cart).values(
                id=uuid.uuid4(), business_id=self.business_id, customer_id=customer.id,
                conversation_id=conversation.id if conversation else None, status="active",
            ).on_conflict_do_nothing(index_elements=["business_id", "customer_id"], index_where=text("status = 'active'")))
            cart = self.carts.first(*where)
        return cart

    def add_item(self, cart: Cart, product: Product, quantity: int = 1) -> CartItem:
        if product.business_id != self.business_id:
            raise NotFoundError("Product not found")
        if quantity < 1:
            raise ValidationError("Quantity must be at least 1")
        if not product.active:
            raise ValidationError(f"{product.name} is not available", code="product_unavailable",
                                  params={"name": product.name})
        settings = SettingsRepo(self.db, self.business_id).first()
        max_q = settings.max_order_quantity if settings else 20
        item = self.items.first(CartItem.cart_id == cart.id, CartItem.product_id == product.id)
        new_qty = (item.quantity if item else 0) + quantity
        if new_qty > max_q:
            raise ValidationError(f"Maximum {max_q} units per product", code="max_quantity", params={"max": max_q})
        if new_qty > product.stock_quantity:
            raise ValidationError(f"Only {product.stock_quantity} unit(s) of {product.name} in stock",
                                  code="out_of_stock", params={"qty": product.stock_quantity, "name": product.name})
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
            raise NotFoundError("That product is not in the cart", code="not_in_cart")
        if quantity <= 0:
            self.items.delete(item)
        else:
            product = self.products.get_or_404(product_id)
            if quantity > product.stock_quantity:
                raise ValidationError(f"Only {product.stock_quantity} unit(s) of {product.name} in stock",
                                      code="out_of_stock",
                                      params={"qty": product.stock_quantity, "name": product.name})
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
                t.issue_details.append({"name": p.name, "qty": p.stock_quantity, "active": p.active})
            t.subtotal += line_total
        if business.delivery_enabled and t.lines:
            delivery = DeliveryService(self.db, self.business_id)
            zone = None
            if delivery_location:
                q = delivery.quote(delivery_location)
                if q.available:
                    zone = delivery.zones.get(q.zone_id)
                    cart.delivery_zone_id = zone.id  # remembered for the quote; the address comes at checkout
                    self.db.flush()
                else:
                    t.delivery_note, t.delivery_note_code, t.delivery_note_params = q.message, q.code, q.params
            elif cart.delivery_zone_id:
                zone = delivery.zones.get(cart.delivery_zone_id)
            if zone:
                t.delivery_fee = zone.fee
                t.delivery_zone = zone.name
            else:
                t.delivery_pending = True
                t.delivery_note_code = t.delivery_note_code or "delivery_pending"
                t.delivery_note = t.delivery_note or ("Delivery fee depends on your area. "
                                                      "Share your delivery location for the exact total.")
        t.total = t.subtotal + t.delivery_fee - t.discount
        return t


# ---------------------------------------------------------------- checkout (explicit confirmation)
class CheckoutChanged(ValidationError):
    code = "checkout_changed"


@dataclass
class CheckoutSummary:
    cart_id: uuid.UUID
    text: str
    totals: CartTotals
    delivery_address: str | None


def _fingerprint(totals: CartTotals, cart: Cart) -> str:
    data = [[(line.product_id, line.quantity, str(line.unit_price)) for line in totals.lines],
            str(totals.delivery_fee), str(cart.delivery_zone_id), cart.delivery_address, str(totals.total)]
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()


def render_summary(totals: CartTotals, delivery_address: str | None, lang: str = "en") -> str:
    """The exact summary the customer confirms. Wording follows the conversation language; every fact (names,
    quantities, prices, totals, address) is formatted identically in all languages."""
    from app.i18n import t
    cur = totals.currency
    lines = [t("summary_title", lang)]
    for i, line in enumerate(totals.lines, 1):
        lines.append(f"{i}. {line.name} x{line.quantity} @ {cur} {money(line.unit_price)} = {cur} {money(line.line_total)}")
    lines.append(f"{t('subtotal', lang)}: {cur} {money(totals.subtotal)}")
    if totals.delivery_zone:
        lines.append(f"{t('delivery', lang)} ({totals.delivery_zone}): {cur} {money(totals.delivery_fee)}")
    if totals.discount:
        lines.append(f"{t('discount', lang)}: -{cur} {money(totals.discount)}")
    lines.append(f"{t('total', lang)}: {cur} {money(totals.total)}")
    lines.append(t("deliver_to", lang, address=delivery_address) if delivery_address else t("pickup", lang))
    lines.append(t("confirm_prompt", lang))
    return "\n".join(lines)


class CheckoutService:
    """The only way an order is created from a conversation. The agent can *prepare* a checkout; the order is
    placed only when a later customer message explicitly confirms the delivered, unchanged summary."""

    def __init__(self, db: Session, business_id: uuid.UUID):
        self.db = db
        self.business_id = business_id
        self.carts = CartService(db, business_id)

    def prepare(self, customer: Customer, conv: Conversation, *, delivery_address: str | None = None,
                notes: str | None = None, language: str = "en") -> CheckoutSummary:
        business = self.db.get(Business, self.business_id)
        cart = self.carts.get_active(customer, conv, create=False)
        if cart is None or not cart.items:
            raise ValidationError("The cart is empty. Add products before checking out.", code="cart_empty")
        if business.delivery_enabled:
            address = (delivery_address or "").strip() or (cart.delivery_address or "")
            if len(address) < 3:
                raise ValidationError("Please share your delivery address (area and street or a landmark) "
                                      "so I can prepare your order.", code="address_required")
            quote = DeliveryService(self.db, self.business_id).quote(address)
            if not quote.available:
                raise ValidationError(quote.message, code=quote.code, params=quote.params)
            cart.delivery_zone_id = uuid.UUID(quote.zone_id)
            cart.delivery_address = address[:300]
        else:
            cart.delivery_zone_id, cart.delivery_address = None, None
        self.db.flush()
        totals = self.carts.totals(cart)
        if totals.issues:
            raise ValidationError("; ".join(totals.issues), code="stock_issues",
                                  params={"issues": totals.issue_details})
        now = datetime.now(timezone.utc)
        cart.checkout = {"fingerprint": _fingerprint(totals, cart), "prepared_at": now.isoformat(),
                         "expires_at": (now + CHECKOUT_TTL).isoformat(), "notes": notes, "summary_message_id": None}
        self.db.flush()
        return CheckoutSummary(cart.id, render_summary(totals, cart.delivery_address, language), totals,
                               cart.delivery_address)

    def attach_summary_message(self, cart_id: uuid.UUID, message_id: uuid.UUID) -> None:
        cart = self.carts.carts.get(cart_id)
        if cart is not None and cart.checkout:
            cart.checkout = {**cart.checkout, "summary_message_id": str(message_id)}

    def pending(self, customer: Customer) -> Cart | None:
        cart = self.carts.get_active(customer, create=False)
        return cart if cart is not None and cart.checkout else None

    def cancel(self, customer: Customer) -> bool:
        cart = self.pending(customer)
        if cart is None:
            return False
        cart.checkout = None
        return True

    def confirm(self, customer: Customer, conv: Conversation, confirmation: Message) -> Order:
        cart = self.pending(customer)
        if cart is None:
            raise ValidationError("There is no order summary waiting for confirmation.", code="no_checkout")
        checkout = cart.checkout
        if datetime.fromisoformat(checkout["expires_at"]) < datetime.now(timezone.utc):
            cart.checkout = None
            raise CheckoutChanged("That order summary has expired.", code="checkout_expired")
        summary = MessageRepo(self.db, self.business_id).get(checkout["summary_message_id"]) \
            if checkout.get("summary_message_id") else None
        if summary is None or summary.delivery_status not in DELIVERED_STATUSES \
                or summary.created_at >= confirmation.created_at:
            raise ValidationError("The order summary was not delivered yet.", code="summary_not_delivered")
        # The YES must answer the summary. Live: after the summary the assistant asked "add the t-shirt to your
        # cart?", the customer said "yes" and the old summary (without the t-shirt) was ordered.
        if MessageRepo(self.db, self.business_id).first(
                Message.conversation_id == summary.conversation_id, Message.role.in_(("assistant", "human_agent")),
                Message.created_at > summary.created_at, Message.created_at < confirmation.created_at):
            cart.checkout = None
            raise CheckoutChanged("The conversation moved on after the summary.", code="summary_superseded")
        totals = self.carts.totals(cart)
        if totals.issues or _fingerprint(totals, cart) != checkout["fingerprint"]:
            cart.checkout = None
            raise CheckoutChanged("The cart, prices, stock or delivery changed since the summary.")
        order = OrderService(self.db, self.business_id).create_from_cart(customer, conv, notes=checkout.get("notes"))
        order.confirmation_message_id = confirmation.id
        order.confirmed_at = datetime.now(timezone.utc)
        self.db.flush()
        return order


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
                         notes: str | None = None) -> Order:
        """Internal: call through CheckoutService.confirm() for conversational orders. Uses the cart's own
        delivery zone + address (set at checkout); never assumes one."""
        from app.models import OrderItem
        business = self.db.get(Business, self.business_id)
        carts = CartService(self.db, self.business_id)
        cart = carts.get_active(customer, conversation, create=False)
        if cart is None or not cart.items:
            raise ValidationError("The cart is empty")
        totals = carts.totals(cart)
        if business.delivery_enabled and (totals.delivery_zone is None or not cart.delivery_address):
            raise ValidationError("A delivery address in a delivery zone is required before ordering")

        order = self.orders.add(
            order_number=self._next_number(business), customer_id=customer.id,
            conversation_id=conversation.id if conversation else None, status="pending", payment_status="unpaid",
            currency=business.currency, subtotal=ZERO, delivery_fee=totals.delivery_fee, discount=totals.discount,
            total=ZERO, delivery_zone_name=totals.delivery_zone, delivery_address=cart.delivery_address, notes=notes,
        )
        subtotal = ZERO
        # Lock product rows in a stable order to avoid deadlocks, then validate + decrement stock.
        product_ids = sorted(i.product_id for i in cart.items)
        locked = {p.id: p for p in self.db.scalars(
            self.products.query().where(Product.id.in_(product_ids)).order_by(Product.id).with_for_update(of=Product)).all()}
        for item in cart.items:
            p = locked.get(item.product_id)
            if p is None or not p.active:
                raise ValidationError("A product in the cart is no longer available")
            if p.stock_quantity < item.quantity:
                raise ConflictError(f"Only {p.stock_quantity} unit(s) of {p.name} left in stock", code="out_of_stock",
                                    params={"qty": p.stock_quantity, "name": p.name})
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
        cart.checkout = None
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
            raise NotFoundError(f"Order {number} not found", code="order_not_found", params={"number": number})
        return order

    def for_customer(self, customer: Customer, limit: int = 10) -> list[Order]:
        return self.orders.list(where=[Order.customer_id == customer.id], order_by=[Order.created_at.desc()],
                                limit=limit)

    def latest_unpaid(self, customer: Customer) -> Order | None:
        rows = self.orders.list(where=[Order.customer_id == customer.id, Order.status.in_(OPEN_STATUSES),
                                       Order.payment_status != "paid"],
                                order_by=[Order.created_at.desc()], limit=1)
        return rows[0] if rows else None

    def list(self, *, status: str | None = None, limit: int = 200) -> list[Order]:
        where = [Order.status == status] if status else []
        return self.orders.list(where=where, order_by=[Order.created_at.desc()], limit=limit)

    def transition(self, order: Order, new_status: str, *, reason: str | None = None) -> Order:
        if new_status not in ORDER_TRANSITIONS:
            raise ValidationError(f"Unknown status '{new_status}'")
        if new_status not in ORDER_TRANSITIONS[order.status]:
            raise ValidationError(f"Cannot change order from {order.status} to {new_status}")
        now = datetime.now(timezone.utc)
        if new_status == "cancelled":
            self._restock(order)
            order.cancelled_at, order.cancel_reason = now, (reason or None)
        elif new_status == "accepted":
            order.accepted_at = now
        order.status = new_status
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
            Order.payment_status == "paid", Order.status != "cancelled")
        return Decimal(self.db.scalar(stmt) or 0)
