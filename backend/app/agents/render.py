"""Deterministic WhatsApp-friendly rendering of tool results (used by the rules provider)."""
from typing import Any


def fmt(amount: float, currency: str) -> str:
    return f"{currency} {amount:,.0f}" if float(amount).is_integer() else f"{currency} {amount:,.2f}"


def render_cart(cart: dict[str, Any], *, with_delivery: bool = True) -> str:
    if not cart["lines"]:
        return "Your cart is empty. Tell me what you're looking for!"
    cur = cart["currency"]
    lines = ["🛒 Your cart:"]
    for i, l in enumerate(cart["lines"], 1):
        lines.append(f"{i}. {l['name']} x{l['quantity']} — {fmt(l['line_total'], cur)}")
    lines.append(f"Subtotal: {fmt(cart['subtotal'], cur)}")
    if with_delivery:
        if cart.get("delivery_zone"):
            lines.append(f"Delivery ({cart['delivery_zone']}): {fmt(cart['delivery_fee'], cur)}")
        if cart.get("discount"):
            lines.append(f"Discount: -{fmt(cart['discount'], cur)}")
        lines.append(f"Total: {fmt(cart['total'], cur)}")
        if cart.get("delivery_note"):
            lines.append(f"ℹ️ {cart['delivery_note']}")
    for issue in cart.get("issues") or []:
        lines.append(f"⚠️ {issue}")
    return "\n".join(lines)


def render_tool_result(tool: str, args: dict[str, Any], r: dict[str, Any]) -> str:
    if not r.get("ok"):
        return f"Sorry — {r.get('error', 'something went wrong')}"
    if tool == "search_products":
        if not r["products"]:
            return ("Sorry, I couldn't find anything matching that in our catalog. "
                    "Could you describe it differently?")
        out = [f"Here's what I found ({r['count']}):"]
        for p in r["products"]:
            stock = "in stock" if p["in_stock"] else "out of stock"
            out.append(f"{p['position']}. {p['name']} — {fmt(p['price'], p['currency'])} ({stock})")
        out.append("Reply e.g. \"add 2\" to add an item to your cart.")
        return "\n".join(out)
    if tool in ("get_product", "check_inventory"):
        p = r.get("product") or r
        price = f" — {fmt(p['price'], p['currency'])}" if "price" in p else ""
        return f"{p['name']}{price}. {'In stock' if p['in_stock'] else 'Out of stock'} ({p['stock_quantity']} available)."
    if tool == "add_to_cart":
        a = r["added"]
        return f"✅ Added {a['name']} x{a['quantity']} to your cart.\n\n" + render_cart(r["cart"], with_delivery=False)
    if tool in ("get_cart", "create_cart", "calculate_cart_total", "remove_from_cart"):
        prefix = "Removed. " if tool == "remove_from_cart" else ""
        return prefix + render_cart(r["cart"])
    if tool == "clear_cart":
        return "Your cart is now empty."
    if tool == "calculate_delivery":
        if not r["available"]:
            return r.get("message") or "Sorry, we don't deliver there."
        eta = f" (estimated {r['estimated_time']})" if r.get("estimated_time") else ""
        return f"Delivery to {r['zone']}: {fmt(r['fee'], r['currency'])}{eta}."
    if tool == "create_order":
        o = r["order"]
        items = "\n".join(f"- {i['name']} x{i['quantity']}: {fmt(i['subtotal'], o['currency'])}" for i in o["items"])
        tail = "Reply \"pay\" to pay with mobile money." if r.get("payment_enabled") else "We'll contact you to arrange payment."
        return (f"🧾 Order {o['order_number']} placed!\n{items}\nDelivery: {fmt(o['delivery_fee'], o['currency'])}\n"
                f"Total: {fmt(o['total'], o['currency'])}\n{tail}")
    if tool in ("get_order", "check_order_status"):
        o = r.get("order") or r
        pay = f" Payment: {r['payment_status']}." if r.get("payment_status") else ""
        return f"Order {o['order_number']} is {o['status'].replace('_', ' ')}. Total {fmt(o['total'], o['currency'])}.{pay}"
    if tool == "get_customer_orders":
        if not r["orders"]:
            return "You don't have any orders yet."
        return "Your recent orders:\n" + "\n".join(
            f"- {o['order_number']}: {o['status'].replace('_', ' ')} ({fmt(o['total'], o['currency'])})" for o in r["orders"])
    if tool == "initiate_payment":
        return (f"📲 I've sent a mobile money request of {fmt(r['amount'], r['currency'])} to {r['payer_phone']} "
                f"for order {r['order_number']}. Please approve it on your phone — I'll confirm as soon as the "
                "payment is received.")
    if tool == "handoff_to_human":
        return "I've passed your conversation to our team. A staff member will reply here shortly."
    if tool == "search_knowledge":
        if not r["results"]:
            return "I'm not sure about that. Would you like me to connect you with our team?"
        return r["results"][0]["content"]
    if tool == "get_business_information":
        parts = [r["name"]]
        if r.get("address"):
            parts.append(f"📍 {r['address']}")
        if r.get("phone"):
            parts.append(f"📞 {r['phone']}")
        if r.get("business_hours"):
            parts.append("🕐 " + ", ".join(f"{k}: {v}" for k, v in r["business_hours"].items()))
        if r.get("delivery_zones"):
            parts.append("🚚 Delivery: " + "; ".join(
                f"{z['name']} {fmt(z['fee'], r['currency'])}" for z in r["delivery_zones"]))
        return "\n".join(parts)
    return "Done."
