"""The generic commerce toolset. Identical for every tenant; behaviour differs only through
each business's data and configuration."""
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func

from app.core.errors import NotFoundError, ValidationError
from app.models import Product
from app.services.commerce_service import CartService, CheckoutService, DeliveryService, OrderService
from app.services.conversation_service import ConversationService
from app.services.knowledge_service import KnowledgeService
from app.services.payment_service import PaymentService
from app.services.product_service import ProductService
from app.tools.registry import Tool, ToolContext, register

# ---------------------------------------------------------------- helpers


def _product_dict(p: Product, position: int | None = None) -> dict[str, Any]:
    d = {"product_id": str(p.id), "name": p.name, "price": float(p.price), "currency": p.currency,
         "in_stock": p.stock_quantity > 0, "stock_quantity": p.stock_quantity, "sku": p.sku,
         "category": p.category.name if p.category else None}
    if p.description:
        d["description"] = p.description[:160]
    if position is not None:
        d = {"position": position, **d}
    return d


def resolve_product(ctx: ToolContext, ref: str) -> Product:
    """Resolve a product reference: list position from the last search ('2'), UUID, SKU or exact name.
    All lookups are tenant-scoped."""
    svc = ProductService(ctx.db, ctx.business_id)
    ref = (ref or "").strip().lstrip("#")
    last = (ctx.conversation.state or {}).get("last_products", [])
    if ref.isdigit() and 1 <= int(ref) <= len(last):
        p = svc.products.get(last[int(ref) - 1]["id"])
        if p:
            return p
    try:
        p = svc.products.get(uuid.UUID(ref))
        if p:
            return p
    except ValueError:
        pass
    p = svc.products.first(func.upper(Product.sku) == ref.upper()) or \
        svc.products.first(func.lower(Product.name) == ref.lower())
    if p:
        return p
    raise NotFoundError(f"Product '{ref}' not found. Search the catalog first.")


def _cart_payload(ctx: ToolContext, location: str | None = None) -> dict[str, Any]:
    carts = CartService(ctx.db, ctx.business_id)
    cart = carts.get_active(ctx.customer, ctx.conversation)
    return {"cart": carts.totals(cart, delivery_location=location).as_dict()}


def _order_dict(o) -> dict[str, Any]:
    return {"order_number": o.order_number, "status": o.status, "payment_status": o.payment_status,
            "currency": o.currency,
            "subtotal": float(o.subtotal), "delivery_fee": float(o.delivery_fee), "discount": float(o.discount),
            "total": float(o.total), "delivery_zone": o.delivery_zone_name,
            "items": [{"name": i.product_name, "quantity": i.quantity, "unit_price": float(i.unit_price),
                       "subtotal": float(i.subtotal)} for i in o.items],
            "created_at": o.created_at.isoformat() if o.created_at else None}


# ---------------------------------------------------------------- arg models
class Args(BaseModel):
    # Unknown or malformed arguments are an error the model must fix, never silently ignored.
    model_config = ConfigDict(extra="forbid")


class SearchArgs(Args):
    query: str = Field(..., description="Search terms in the catalog's language: translate the customer's words "
                                        "(e.g. Kinyarwanda 'inkweto z'umukara' or French 'baskets noires' -> "
                                        "'black sneakers')")
    max_price: float | None = Field(None, ge=0, description="Maximum unit price in the business currency")
    min_price: float | None = Field(None, ge=0)
    category: str | None = None
    limit: int = Field(5, ge=1, le=10)


class ProductRefArgs(Args):
    product_ref: str = Field(..., description="Position from the last search results (e.g. '2'), product_id, or SKU")


class NoArgs(Args):
    pass


class KnowledgeArgs(Args):
    query: str = Field(..., description="The customer's question about policies, delivery, returns, hours, etc.")


class AddToCartArgs(ProductRefArgs):
    quantity: int = Field(1, ge=1, le=50)


class TotalArgs(Args):
    delivery_location: str | None = Field(None, description="Customer's delivery area/address if known")


class CheckoutArgs(Args):
    delivery_address: str | None = Field(None, max_length=300, description="The customer's delivery address "
                                         "(area + street or landmark), exactly as they gave it. Required when "
                                         "delivery applies; never invent it.")
    notes: str | None = Field(None, max_length=500)


class OrderNumberArgs(Args):
    order_number: str | None = Field(None, description="Order number like KF-00012. Omit for the latest order.")


class DeliveryArgs(Args):
    location: str = Field(..., description="Area, neighbourhood or city")


class PaymentArgs(Args):
    order_number: str | None = Field(None, description="Order to pay. Omit to pay the latest unpaid order.")
    phone_number: str | None = Field(None, description="Mobile money number. Omit to use the customer's WhatsApp number.")


class PaymentReferenceArgs(Args):
    reference: str = Field(..., min_length=4, max_length=128,
                           description="The transaction ID/reference the customer sent after paying")
    order_number: str | None = Field(None, description="Omit for the latest unpaid order.")


class HandoffArgs(Args):
    reason: str = Field(..., max_length=300)


# ---------------------------------------------------------------- catalog tools
def search_products(ctx: ToolContext, a: SearchArgs) -> dict[str, Any]:
    hits = ProductService(ctx.db, ctx.business_id).search(a.query, max_price=a.max_price, min_price=a.min_price,
                                                          category=a.category, limit=a.limit)
    products = [_product_dict(h.product, i + 1) for i, h in enumerate(hits)]
    ConversationService(ctx.db, ctx.business_id).set_state(
        ctx.conversation, last_products=[{"id": p["product_id"], "name": p["name"]} for p in products])
    return {"count": len(products), "products": products,
            "note": None if products else "No matching products in the catalog. Do not suggest items not listed."}


def get_product(ctx: ToolContext, a: ProductRefArgs) -> dict[str, Any]:
    return {"product": _product_dict(resolve_product(ctx, a.product_ref))}


def check_inventory(ctx: ToolContext, a: ProductRefArgs) -> dict[str, Any]:
    p = resolve_product(ctx, a.product_ref)
    return {"product_id": str(p.id), "name": p.name, "stock_quantity": p.stock_quantity,
            "in_stock": p.stock_quantity > 0 and p.active}


def get_business_information(ctx: ToolContext, a: NoArgs) -> dict[str, Any]:
    b = ctx.business
    zones = DeliveryService(ctx.db, ctx.business_id).zones.list()
    from app.services.hours import is_open, next_opening
    return {"name": b.name, "description": b.description, "type": b.business_type, "phone": b.phone,
            "address": b.address, "currency": b.currency, "business_hours": b.business_hours,
            "open_now": is_open(b.business_hours, b.timezone),
            "next_opening": next_opening(b.business_hours, b.timezone),
            "delivery_enabled": b.delivery_enabled, "payment_enabled": b.payment_enabled,
            "delivery_zones": [{"name": z.name, "fee": float(z.fee), "estimated_time": z.estimated_time,
                                "areas": z.areas} for z in zones if z.active] if b.delivery_enabled else []}


def search_knowledge(ctx: ToolContext, a: KnowledgeArgs) -> dict[str, Any]:
    hits = KnowledgeService(ctx.db, ctx.business_id).search(a.query)
    return {"results": [{"source": h.document_title, "content": h.content} for h in hits],
            "note": None if hits else "No relevant information found. Say you are not sure and offer help."}


# ---------------------------------------------------------------- cart tools
def create_cart(ctx: ToolContext, a: NoArgs) -> dict[str, Any]:
    return _cart_payload(ctx)


def get_cart(ctx: ToolContext, a: NoArgs) -> dict[str, Any]:
    return _cart_payload(ctx)


def add_to_cart(ctx: ToolContext, a: AddToCartArgs) -> dict[str, Any]:
    p = resolve_product(ctx, a.product_ref)
    carts = CartService(ctx.db, ctx.business_id)
    cart = carts.get_active(ctx.customer, ctx.conversation)
    carts.add_item(cart, p, a.quantity)
    return {"added": {"name": p.name, "quantity": a.quantity, "unit_price": float(p.price)}, **_cart_payload(ctx)}


def remove_from_cart(ctx: ToolContext, a: ProductRefArgs) -> dict[str, Any]:
    carts = CartService(ctx.db, ctx.business_id)
    cart = carts.get_active(ctx.customer, ctx.conversation)
    ref = a.product_ref.strip()
    # Allow positions within the cart too ("remove the first item")
    if ref.isdigit() and 1 <= int(ref) <= len(cart.items) and not (ctx.conversation.state or {}).get("last_products"):
        product_id = cart.items[int(ref) - 1].product_id
    else:
        product_id = resolve_product(ctx, ref).id
    carts.remove_item(cart, product_id)
    return {"removed": True, **_cart_payload(ctx)}


def clear_cart(ctx: ToolContext, a: NoArgs) -> dict[str, Any]:
    carts = CartService(ctx.db, ctx.business_id)
    carts.clear(carts.get_active(ctx.customer, ctx.conversation))
    return {"cleared": True}


def calculate_cart_total(ctx: ToolContext, a: TotalArgs) -> dict[str, Any]:
    return _cart_payload(ctx, a.delivery_location)


def calculate_delivery(ctx: ToolContext, a: DeliveryArgs) -> dict[str, Any]:
    q = DeliveryService(ctx.db, ctx.business_id).quote(a.location)
    return {"available": q.available, "zone": q.zone_name, "fee": float(q.fee), "currency": ctx.business.currency,
            "estimated_time": q.estimated_time, "message": q.message}


# ---------------------------------------------------------------- order tools
def prepare_checkout(ctx: ToolContext, a: CheckoutArgs) -> dict[str, Any]:
    """Prepares the summary the customer must confirm. It does NOT place the order: the system sends the
    summary itself and places the order only when the customer replies YES in their next message."""
    summary = CheckoutService(ctx.db, ctx.business_id).prepare(ctx.customer, ctx.conversation,
                                                                delivery_address=a.delivery_address, notes=a.notes)
    return {"summary_text": summary.text, "cart": summary.totals.as_dict(), "delivery_address": summary.delivery_address,
            "awaiting_customer_confirmation": True,
            "note": "The order is NOT placed. The system sends this summary to the customer, who must reply YES."}


def _find_order(ctx: ToolContext, number: str | None):
    svc = OrderService(ctx.db, ctx.business_id)
    if number:
        return svc.get_by_number(number, customer=ctx.customer)  # customers only see their own orders
    orders = svc.for_customer(ctx.customer, limit=1)
    if not orders:
        raise NotFoundError("You have no orders yet")
    return orders[0]


def get_order(ctx: ToolContext, a: OrderNumberArgs) -> dict[str, Any]:
    return {"order": _order_dict(_find_order(ctx, a.order_number))}


def get_customer_orders(ctx: ToolContext, a: NoArgs) -> dict[str, Any]:
    orders = OrderService(ctx.db, ctx.business_id).for_customer(ctx.customer, limit=5)
    return {"orders": [{"order_number": o.order_number, "status": o.status, "total": float(o.total),
                        "currency": o.currency} for o in orders]}


def check_order_status(ctx: ToolContext, a: OrderNumberArgs) -> dict[str, Any]:
    order = _find_order(ctx, a.order_number)
    payments = PaymentService(ctx.db, ctx.business_id)
    latest = next(iter(payments.for_order(order)), None)
    if latest and latest.status == "pending" and latest.provider != "mock":
        from app.workflows.payments import refresh_and_notify
        try:
            refresh_and_notify(ctx.db, latest)  # poll provider; never guess
        except Exception:
            pass
        ctx.db.refresh(order)
    return {"order_number": order.order_number, "status": order.status, "total": float(order.total),
            "currency": order.currency, "payment_status": order.payment_status}


def initiate_payment(ctx: ToolContext, a: PaymentArgs) -> dict[str, Any]:
    svc = OrderService(ctx.db, ctx.business_id)
    order = svc.get_by_number(a.order_number, customer=ctx.customer) if a.order_number \
        else svc.latest_unpaid(ctx.customer)
    if order is None:
        raise ValidationError("There is no unpaid order. Place an order first.")
    payments = PaymentService(ctx.db, ctx.business_id)
    if payments.provider_name() == "manual":
        from app.workflows.orders import payment_instructions
        payments._payable(order)
        instructions = payment_instructions(ctx.db, ctx.business)
        return {"order_number": order.order_number, "amount": float(order.total), "currency": order.currency,
                "provider": "manual", "payment_status": order.payment_status,
                "instructions": instructions or "The shop will share how to pay. Do not invent payment details.",
                "note": "No payment request was sent. Only the shop can confirm a payment."}
    payment = payments.initiate(order, a.phone_number or ctx.customer.whatsapp_number)
    return {"order_number": order.order_number, "amount": float(payment.amount), "currency": payment.currency,
            "payment_status": payment.status, "provider": payment.provider,
            "payer_phone": f"***{payment.payer_phone[-3:]}" if payment.payer_phone else None,  # PII minimisation
            "instructions": "A payment request was sent. The customer must approve it on their phone. "
                            "The order is NOT paid until the provider confirms."}


def submit_payment_reference(ctx: ToolContext, a: PaymentReferenceArgs) -> dict[str, Any]:
    from app.workflows.orders import customer_reported_payment
    svc = OrderService(ctx.db, ctx.business_id)
    order = svc.get_by_number(a.order_number, customer=ctx.customer) if a.order_number \
        else svc.latest_unpaid(ctx.customer)
    if order is None:
        raise ValidationError("There is no unpaid order.")
    customer_reported_payment(ctx.db, ctx.business_id, ctx.customer, order, a.reference)
    return {"order_number": order.order_number, "payment_status": "pending",
            "note": "Recorded for the shop to verify. The order is NOT paid until the shop confirms."}


def handoff_to_human(ctx: ToolContext, a: HandoffArgs) -> dict[str, Any]:
    from app.workflows.handoff import request_human
    request_human(ctx.db, ctx.business, ctx.conversation, a.reason)
    return {"handed_off": True}


# ---------------------------------------------------------------- registration
def _delivery(b): return b.delivery_enabled
def _payment(b): return b.payment_enabled
def _handoff(b): return b.human_handoff_enabled


for _t in [
    Tool("search_products", "Search the catalog. ALWAYS use before mentioning any product, price or availability.",
         SearchArgs, search_products),
    Tool("get_product", "Get details for one product.", ProductRefArgs, get_product),
    Tool("check_inventory", "Check current stock for a product.", ProductRefArgs, check_inventory),
    Tool("get_business_information", "Business details: hours, address, contact, delivery zones.", NoArgs,
         get_business_information),
    Tool("search_knowledge", "Search business FAQs/policies (returns, delivery policy, warranty). "
         "Not for products/prices/orders.", KnowledgeArgs, search_knowledge),
    Tool("create_cart", "Ensure the customer has an active cart.", NoArgs, create_cart, mutates=True),
    Tool("get_cart", "Show the cart with exact DB prices and totals.", NoArgs, get_cart),
    Tool("add_to_cart", "Add a product to the cart.", AddToCartArgs, add_to_cart, mutates=True),
    Tool("remove_from_cart", "Remove a product from the cart.", ProductRefArgs, remove_from_cart, mutates=True),
    Tool("clear_cart", "Empty the cart.", NoArgs, clear_cart, mutates=True),
    Tool("calculate_cart_total", "Exact subtotal, delivery fee, discount and total.", TotalArgs,
         calculate_cart_total),
    Tool("calculate_delivery", "Delivery fee and ETA for a location.", DeliveryArgs, calculate_delivery,
         enabled_if=_delivery),
    Tool("prepare_checkout", "When the customer wants to order: prepare the order summary for them to confirm. "
         "Needs their delivery address when delivery applies. It does not place the order.", CheckoutArgs,
         prepare_checkout, mutates=True),
    Tool("get_order", "Get an order's details.", OrderNumberArgs, get_order),
    Tool("get_customer_orders", "List the customer's recent orders.", NoArgs, get_customer_orders),
    Tool("check_order_status", "Current status of an order and its payment.", OrderNumberArgs, check_order_status),
    Tool("initiate_payment", "How to pay an order (payment instructions or a mobile-money request).", PaymentArgs,
         initiate_payment, mutates=True, enabled_if=_payment),
    Tool("submit_payment_reference", "Record the transaction reference a customer sends after paying, for the "
         "shop to verify. It never marks the order paid.", PaymentReferenceArgs, submit_payment_reference,
         mutates=True, enabled_if=_payment),
    Tool("handoff_to_human", "Transfer to a human staff member (complaints, explicit request, unresolved issues).",
         HandoffArgs, handoff_to_human, mutates=True, enabled_if=_handoff),
]:
    register(_t)
