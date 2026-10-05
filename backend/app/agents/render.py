"""Deterministic WhatsApp-friendly rendering of tool results, in the conversation language.

Used by the offline rules provider and as the fallback whenever a model reply fails the grounding check.
Only wording is localised (app/i18n.py); names, prices (`fmt`), quantities, order numbers and owner-written
text are inserted exactly as the tools returned them."""
from typing import Any

from app.i18n import error_text, payment_text, status_text, t


def fmt(amount: float, currency: str) -> str:
    return f"{currency} {amount:,.0f}" if float(amount).is_integer() else f"{currency} {amount:,.2f}"


def _note(cart: dict[str, Any], lang: str) -> str | None:
    code = cart.get("delivery_note_code")
    if code == "delivery_pending":
        return t("delivery_pending", lang)
    if code:
        return error_text(code, cart.get("delivery_note_params"), cart.get("delivery_note") or "", lang)
    return cart.get("delivery_note")


def _issues(cart: dict[str, Any], lang: str) -> list[str]:
    details = cart.get("issue_details")
    if not details:
        return list(cart.get("issues") or [])
    return [t("stock_issue", lang, name=d["name"], qty=d["qty"]) if d["active"]
            else t("unavailable_issue", lang, name=d["name"]) for d in details]


def render_cart(cart: dict[str, Any], *, with_delivery: bool = True, lang: str = "en") -> str:
    if not cart["lines"]:
        return t("cart_empty", lang)
    cur = cart["currency"]
    lines = [t("cart_title", lang)]
    for i, line in enumerate(cart["lines"], 1):
        lines.append(f"{i}. {line['name']} x{line['quantity']} — {fmt(line['line_total'], cur)}")
    lines.append(f"{t('subtotal', lang)}: {fmt(cart['subtotal'], cur)}")
    if with_delivery:
        if cart.get("delivery_zone"):
            lines.append(f"{t('delivery', lang)} ({cart['delivery_zone']}): {fmt(cart['delivery_fee'], cur)}")
        if cart.get("discount"):
            lines.append(f"{t('discount', lang)}: -{fmt(cart['discount'], cur)}")
        label = "total_before_delivery" if cart.get("delivery_pending") else "total"
        lines.append(f"{t(label, lang)}: {fmt(cart['total'], cur)}")
        note = _note(cart, lang)
        if note:
            lines.append(f"ℹ️ {note}")
    for issue in _issues(cart, lang):
        lines.append(f"⚠️ {issue}")
    return "\n".join(lines)


def render_error(r: dict[str, Any], lang: str) -> str:
    if r.get("error_code") == "stock_issues":
        message = "; ".join(_issues({"issue_details": (r.get("error_params") or {}).get("issues")}, lang))
    else:
        message = error_text(r.get("error_code"), r.get("error_params"), r.get("error", "something went wrong"), lang)
    return t("sorry_error", lang, error=message)


def render_tool_result(tool: str, args: dict[str, Any], r: dict[str, Any], lang: str = "en") -> str:
    if not r.get("ok"):
        return render_error(r, lang)
    if tool == "search_products":
        if not r["products"]:
            return t("search_none", lang)
        partial = any(p.get("missing") for p in r["products"])
        out = [t("search_partial" if partial else "search_found", lang, count=r["count"])]
        for p in r["products"]:
            stock = t("in_stock" if p["in_stock"] else "out_of_stock", lang)
            out.append(f"{p['position']}. {p['name']} — {fmt(p['price'], p['currency'])} ({stock})")
        out.append(t("search_hint", lang))
        return "\n".join(out)
    if tool in ("get_product", "check_inventory"):
        p = r.get("product") or r
        price = f" — {fmt(p['price'], p['currency'])}" if "price" in p else ""
        stock = t("in_stock_cap" if p["in_stock"] else "out_of_stock_cap", lang)
        return f"{p['name']}{price}. {stock} {t('available_count', lang, qty=p['stock_quantity'])}."
    if tool == "add_to_cart":
        a = r["added"]
        return t("added", lang, name=a["name"], qty=a["quantity"]) + "\n\n" + \
            render_cart(r["cart"], with_delivery=False, lang=lang)
    if tool in ("get_cart", "create_cart", "calculate_cart_total", "remove_from_cart"):
        prefix = t("removed", lang) if tool == "remove_from_cart" else ""
        return prefix + render_cart(r["cart"], lang=lang)
    if tool == "clear_cart":
        return t("cart_cleared", lang)
    if tool == "calculate_delivery":
        if not r["available"]:
            if r.get("code"):
                return error_text(r["code"], r.get("params"), r.get("message") or "", lang)
            return r.get("message") or t("no_delivery_there", lang)
        eta = t("eta", lang, time=r["estimated_time"]) if r.get("estimated_time") else ""
        return t("delivery_quote", lang, zone=r["zone"], fee=fmt(r["fee"], r["currency"]), eta=eta)
    if tool == "prepare_checkout":
        return r["summary_text"]
    if tool in ("get_order", "check_order_status"):
        o = r.get("order") or r
        pay = t("payment_part", lang, payment=payment_text(o["payment_status"], lang)) if o.get("payment_status") else ""
        return t("order_status", lang, number=o["order_number"], status=status_text(o["status"], lang),
                 total=fmt(o["total"], o["currency"]), payment=pay)
    if tool == "get_customer_orders":
        if not r["orders"]:
            return t("no_orders", lang)
        return t("recent_orders", lang) + "\n" + "\n".join(
            f"- {o['order_number']}: {status_text(o['status'], lang)} ({fmt(o['total'], o['currency'])})"
            for o in r["orders"])
    if tool == "submit_payment_reference":
        return t("reference_passed", lang, ref=args.get("reference"), number=r["order_number"])
    if tool == "initiate_payment" and r.get("provider") == "manual":
        return t("manual_pay", lang, number=r["order_number"], amount=fmt(r["amount"], r["currency"]),
                 instructions=r["instructions"])
    if tool == "initiate_payment":
        return t("momo_sent", lang, amount=fmt(r["amount"], r["currency"]), phone=r["payer_phone"],
                 number=r["order_number"])
    if tool == "handoff_to_human":
        return t("handoff", lang)
    if tool == "search_knowledge":
        if not r["results"]:
            return t("knowledge_none", lang)
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
            parts.append(t("biz_delivery", lang) + " " + "; ".join(
                f"{z['name']} {fmt(z['fee'], r['currency'])}" for z in r["delivery_zones"]))
        return "\n".join(parts)
    return t("done", lang)
