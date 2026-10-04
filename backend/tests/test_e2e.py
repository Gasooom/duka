"""End-to-end journeys through the real webhook, worker, agent, tools, outbox and dashboard API. Both stores run
on the same engine/tools with zero tenant-specific code:
WhatsApp -> search -> cart -> summary -> explicit YES -> order -> owner notified -> owner accepts ->
payment (manual, owner-confirmed; or provider-confirmed) -> fulfilment -> customer told at each step."""
import json

from app.core.config import settings
from tests.conftest import mock_signature

CUSTOMER = "250788555666"
OWNER = "250788000999"


def test_kigali_fashion_full_journey(fashion, outbox, client, monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_app_secret", "meta-app-secret")
    fashion.post("/api/knowledge", json={"title": "Delivery policy", "content":
                 "We deliver across Kigali the same day. Outside Kigali we deliver to Musanze and Huye in 1-2 days."})
    fashion.patch("/api/business/settings", json={"owner_notification_phone": OWNER,
                                                  "payment_instructions": "MoMo 0788 123 456 (Kigali Fashion)"})

    def say(text):
        assert fashion.send(text, from_number=CUSTOMER, sign_secret="meta-app-secret").status_code == 200
        to, body = [m for m in outbox.sent if m[0] == CUSTOMER][-1]
        return body

    r = say("Hi, I'm looking for black sneakers under 100,000 RWF.")
    lines = [line for line in r.splitlines() if line[:2] in ("1.", "2.", "3.", "4.", "5.")]
    assert len(lines) == 4 and all("RWF" in line for line in lines)
    second = lines[1].split(". ", 1)[1].split(" — ")[0]

    r = say("Add the second one.")
    assert f"Added {second}" in r
    products = {p["name"]: p for p in fashion.get("/api/products").json()}
    price = products[second]["price"]

    r = say("How much including delivery?")
    assert f"Total before delivery: RWF {price:,.0f}" in r and "depends on your area" in r  # no assumed zone

    r = say("Place the order.")
    assert "delivery address" in r and fashion.get("/api/orders").json() == []  # no order, no assumption

    r = say("Deliver to Remera, KG 11 Ave house 5")
    assert "Order summary" in r and f"Total: RWF {price + 2000:,.0f}" in r and "Reply YES" in r
    assert fashion.get("/api/orders").json() == [], "a summary is not an order"

    r = say("yes")
    assert "Order KF-00001 confirmed" in r and "will review it" in r
    order = fashion.get("/api/orders").json()[0]
    assert order["status"] == "pending" and order["payment_status"] == "unpaid"
    assert order["total"] == price + 2000 and order["items"][0]["unit_price"] == price
    assert order["delivery_address"] == "Remera, KG 11 Ave house 5" and order["confirmation_message_id"]
    assert fashion.get(f"/api/products/{products[second]['id']}").json()["stock_quantity"] == \
        products[second]["stock_quantity"] - 1
    owner_msgs = [body for to, body in outbox.sent if to == OWNER]
    assert owner_msgs and "New order KF-00001" in owner_msgs[-1] and "Remera" in owner_msgs[-1]
    notes = fashion.get("/api/dashboard/notifications").json()
    assert notes[0]["kind"] == "new_order" and notes[0]["status"] == "sent"

    # Owner reviews and accepts -> customer is told, with the shop's payment instructions.
    accepted = fashion.patch(f"/api/orders/{order['id']}", json={"status": "accepted"}).json()
    assert accepted["status"] == "accepted"
    assert "accepted your order KF-00001" in outbox.sent[-1][1] and "MoMo 0788 123 456" in outbox.sent[-1][1]

    r = say("I have paid, transaction id MP2410.55.X1")
    assert "MP2410.55.X1" in r and fashion.get(f"/api/orders/{order['id']}").json()["payment_status"] == "pending"
    r = say("What's the status of my order?")
    assert "KF-00001 is accepted" in r and "Payment: pending" in r

    d = fashion.post(f"/api/orders/{order['id']}/payments", json={"method": "momo", "reference": "MP2410.55.X1"}).json()
    assert d["payment_status"] == "paid" and d["payments"][0]["confirmation_source"] == "owner"
    assert "Payment received for order KF-00001" in outbox.sent[-1][1]

    for status, phrase in (("out_for_delivery", "on the way"), ("delivered", "was delivered")):
        fashion.patch(f"/api/orders/{order['id']}", json={"status": status})
        assert phrase in outbox.sent[-1][1]

    r = say("Do you deliver outside Kigali?")
    assert "Musanze" in r

    conv = fashion.get("/api/conversations").json()[0]
    dbg = fashion.get(f"/api/conversations/{conv['id']}").json()
    tools_used = [s["tool"] for run in dbg["agent_runs"] for s in run["steps"] if s["type"] == "tool"]
    assert {"search_products", "add_to_cart", "calculate_cart_total", "prepare_checkout",
            "submit_payment_reference", "check_order_status", "search_knowledge"} <= set(tools_used)
    assert "create_order" not in tools_used
    assert any(run["status"] == "order_confirmed" for run in dbg["agent_runs"])
    stats = fashion.get("/api/dashboard/stats").json()
    assert stats["orders_by_status"]["delivered"] == 1 and stats["revenue"] == price + 2000 and stats["customers"] == 1


def test_second_store_same_engine_completely_different_catalog(fashion, electronics, outbox, client):
    electronics.post("/api/knowledge", json={"title": "Warranty",
                                             "content": "Phones and laptops have a 12-month warranty."})
    assert electronics.patch("/api/business/settings", json={"payment_provider": "mock"}).status_code == 200
    for t in ("Do you have a Samsung phone under 300k?", "add it", "deliver to Remera, KK 15 Rd", "yes", "pay"):
        electronics.send(t, from_number=CUSTOMER)
    order = electronics.get("/api/orders").json()[0]
    assert order["order_number"] == "ME-00001"
    assert order["items"][0]["product_name"] == "Samsung Galaxy A15"
    assert order["total"] == 210000 + 3000
    pay = electronics.get(f"/api/orders/{order['id']}").json()["payments"][0]
    body = json.dumps({"reference": pay["provider_reference"], "status": "successful"}).encode()
    client.post("/webhooks/payments/mock", content=body, headers={"X-Mock-Signature": mock_signature(body)})
    assert electronics.get(f"/api/orders/{order['id']}").json()["payment_status"] == "paid"
    electronics.send("is there a warranty?", from_number=CUSTOMER)
    assert "12-month warranty" in outbox.sent[-1][1]
    fashion.send("my orders", from_number=CUSTOMER)
    assert "don't have any orders" in outbox.sent[-1][1]
