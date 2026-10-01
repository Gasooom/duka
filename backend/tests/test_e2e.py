"""Section 37 'Final product test', automated. Both stores run on the same engine/tools with
zero tenant-specific code: WhatsApp webhook -> AI -> product -> cart -> order -> payment ->
provider callback -> paid -> customer confirmation."""
import json

from app.core.config import settings
from tests.conftest import mock_signature

CUSTOMER = "250788555666"


def test_kigali_fashion_full_journey(fashion, outbox, client, monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_app_secret", "meta-app-secret")
    fashion.post("/api/knowledge", json={"title": "Delivery policy", "content":
                 "We deliver across Kigali the same day. Outside Kigali we deliver to Musanze and Huye in 1-2 days."})

    def say(text):
        assert fashion.send(text, from_number=CUSTOMER, sign_secret="meta-app-secret").status_code == 200
        to, body = outbox.sent[-1]
        assert to == CUSTOMER
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
    assert f"Total: RWF {price + 2000:,.0f}" in r and "Delivery (Kigali City): RWF 2,000" in r

    r = say("Place the order.")
    assert "Order KF-00001 placed" in r
    order = fashion.get("/api/orders").json()[0]
    assert order["total"] == price + 2000 and order["items"][0]["unit_price"] == price
    assert fashion.get(f"/api/products/{products[second]['id']}").json()["stock_quantity"] == \
        products[second]["stock_quantity"] - 1

    r = say("Pay.")
    assert "approve it on your phone" in r
    detail = fashion.get(f"/api/orders/{order['id']}").json()
    assert detail["status"] == "awaiting_payment"

    body = json.dumps({"reference": detail["payments"][0]["provider_reference"], "status": "successful"}).encode()
    assert client.post("/webhooks/payments/mock", content=body,
                       headers={"X-Mock-Signature": mock_signature(body)}).status_code == 200
    assert fashion.get(f"/api/orders/{order['id']}").json()["status"] == "paid"
    assert "Payment received" in outbox.sent[-1][1] and "KF-00001" in outbox.sent[-1][1]

    r = say("What's the status of my order?")
    assert "KF-00001 is paid" in r

    r = say("Do you deliver outside Kigali?")
    assert "Musanze" in r

    # Debugger shows the full trace
    conv = fashion.get("/api/conversations").json()[0]
    dbg = fashion.get(f"/api/conversations/{conv['id']}").json()
    tools_used = [s["tool"] for run in dbg["agent_runs"] for s in run["steps"] if s["type"] == "tool"]
    assert {"search_products", "add_to_cart", "calculate_cart_total", "create_order", "initiate_payment",
            "check_order_status", "search_knowledge"} <= set(tools_used)
    stats = fashion.get("/api/dashboard/stats").json()
    assert stats["orders_by_status"]["paid"] == 1 and stats["revenue"] == price + 2000 and stats["customers"] == 1


def test_second_store_same_engine_completely_different_catalog(fashion, electronics, outbox, client):
    electronics.post("/api/knowledge", json={"title": "Warranty",
                                             "content": "Phones and laptops have a 12-month warranty."})
    for t in ("Do you have a Samsung phone under 300k?", "add it", "place the order", "pay"):
        electronics.send(t, from_number=CUSTOMER)
    order = electronics.get("/api/orders").json()[0]
    assert order["order_number"] == "ME-00001"
    assert order["items"][0]["product_name"] == "Samsung Galaxy A15"
    assert order["total"] == 210000 + 3000
    pay = electronics.get(f"/api/orders/{order['id']}").json()["payments"][0]
    body = json.dumps({"reference": pay["provider_reference"], "status": "successful"}).encode()
    client.post("/webhooks/payments/mock", content=body, headers={"X-Mock-Signature": mock_signature(body)})
    assert electronics.get(f"/api/orders/{order['id']}").json()["status"] == "paid"
    electronics.send("is there a warranty?", from_number=CUSTOMER)
    assert "12-month warranty" in outbox.sent[-1][1]
    # Same customer number in the fashion store sees nothing from electronics
    fashion.send("my orders", from_number=CUSTOMER)
    assert "don't have any orders" in outbox.sent[-1][1]
