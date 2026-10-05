"""Grounding precision on replies the real model (gpt-4o-mini, live runs through the dev simulator) actually wrote.

In the first live run 12 of 42 model turns were rejected and NONE of the rejected replies contained a wrong fact:
the checker split "2. **Price**: ..." after the list marker, read "IdeaPad 3 is RWF 590,000" as the price 3, did not
know the shop's own "12-month warranty" rule, treated "Here are two laptops available:" as a claim about an unnamed
product, treated "pending until the payment is confirmed" as a payment claim, and knew nothing about products shown
in an earlier turn. Each such reply is kept verbatim below and must pass; each is paired with a fabrication that must
still be rejected.
"""
import uuid

from app.agents.grounding import build_ledger, verify
from app.services.product_service import ProductService
from app.tools.registry import execute_tool
from tests.test_ai_safety import call, model
from tests.test_search import last_reply, tool_ctx

OWNER = ("Phones, laptops and accessories with genuine warranty.\nAll phones and laptops include a 12-month "
         "warranty. Always mention the warranty when recommending phones or laptops.")
A15 = {"position": 1, "name": "Samsung Galaxy A15", "price": 210000, "currency": "RWF", "in_stock": True,
       "stock_quantity": 6, "description": "6.5 inch display 128GB storage 4GB RAM dual SIM smartphone."}
HP = {"position": 1, "name": "HP 250 G9 Laptop", "price": 650000, "currency": "RWF", "in_stock": True,
      "stock_quantity": 4, "description": "15.6 inch Intel Core i5 8GB RAM 512GB SSD laptop."}
LENOVO = {"position": 2, "name": "Lenovo IdeaPad 3", "price": 590000, "currency": "RWF", "in_stock": True,
          "stock_quantity": 5, "description": "15.6 inch AMD Ryzen 5 8GB RAM 256GB SSD laptop."}


def search_result(*products):
    return [("search_products", {"query": "x"}, {"ok": True, "count": len(products), "products": list(products)})]


def kinds(reply, led):
    return {v.kind for v in verify(reply, led)}


def test_numbered_details_and_the_shops_own_warranty_rule():
    led = build_ledger(search_result(A15), {}, "Do you have the Samsung Galaxy A15?", owner_text=OWNER)
    real = ("Yes, we have the Samsung Galaxy A15 available. Here are the details:\n\n1. **Name**: Samsung Galaxy A15\n"
            "2. **Price**: RWF 210,000\n3. **Stock**: 6 units available\n4. **Description**: 6.5 inch display, "
            "128GB storage, 4GB RAM, dual SIM smartphone.\n\nIt includes a 12-month warranty.")
    assert verify(real, led) == []
    assert "money" in kinds(real.replace("210,000", "200,000"), led)
    assert "number" in kinds(real.replace("6 units", "16 units"), led)
    assert "number" in kinds(real.replace("4GB RAM", "8GB RAM"), led)       # invented spec
    assert "number" in kinds(real.replace("12-month", "24-month"), led)     # warranty the shop never offered


def test_listing_header_and_numbers_inside_product_names():
    led = build_ledger(search_result(HP, LENOVO), {}, "Hi, I'm looking for a laptop", owner_text=OWNER)
    real = ("Here are two laptops available:\n\n1. HP 250 G9 Laptop\n   - Price: RWF 650,000\n   - Specs: 15.6 inch "
            "Intel Core i5, 8GB RAM, 512GB SSD\n   - Stock: 4 available\n\n2. Lenovo IdeaPad 3\n   - Price: RWF "
            "590,000\n   - Specs: 15.6 inch AMD Ryzen 5, 8GB RAM, 256GB SSD\n   - Stock: 5 available\n\nBoth "
            "include a 12-month warranty.")
    assert verify(real, led) == []
    assert "money" in kinds(real.replace("590,000", "560,000"), led)
    sold_out = build_ledger(search_result(HP, {**LENOVO, "in_stock": False, "stock_quantity": 0}), {}, "laptop",
                            owner_text=OWNER)
    assert "availability" in kinds(real.replace("Stock: 5 available", "Stock: 0"), sold_out)  # header lies
    led3 = build_ledger(search_result({"position": 3, "name": "Oraimo FreePods 4", "price": 35000, "currency": "RWF",
                                       "in_stock": True, "stock_quantity": 25}), {}, "earbuds")
    assert verify("3. Oraimo FreePods 4 - RWF 35,000", led3) == []        # the 4 is the model name, not a price
    assert "money" in kinds("3. Oraimo FreePods 4 - RWF 4", led3)


def test_follow_up_answered_from_the_conversation_is_checked_against_current_facts():
    state = {"last_products": [{"id": "a", "name": "HP 250 G9 Laptop"}, {"id": "b", "name": "Lenovo IdeaPad 3"}]}
    real = "The Lenovo IdeaPad 3 is RWF 590,000. It includes a 12-month warranty."
    assert "money" in kinds(real, build_ledger([], state, "How much is the Lenovo?", owner_text=OWNER))  # before
    led = build_ledger([], state, "How much is the Lenovo?", context_products=[HP, LENOVO], owner_text=OWNER)
    assert verify(real, led) == []
    assert "money" in kinds(real.replace("590,000", "560,000"), led)       # a stale or invented price still fails
    assert "cart" in kinds("I've added the Lenovo IdeaPad 3 to your cart.", led)  # context is not an action
    assert "availability" in kinds("Yes, it's in stock!", led)            # an unnamed "it": ambiguous


def test_conditional_payment_and_order_wording_is_not_a_claim():
    pay = [("initiate_payment", {}, {"ok": True, "order_number": "KF-00005", "amount": 80000, "currency": "RWF",
                                     "payment_status": "pending", "provider": "mock"})]
    led = build_ledger(pay, {}, "yes")
    real = ("A payment request for RWF 80,000 has been sent. Please approve it on your phone. \n\nYour order is still "
            "pending until the payment is confirmed. Let me know if you need anything else!")
    assert verify(real, led) == []
    assert "payment_status" in kinds("Your payment has been received, thank you!", led)
    assert "order_placed" in kinds("Great news: your order has been placed.", build_ledger([], {}, "yes"))


def test_budget_header_with_two_phones():
    tecno = {"position": 2, "name": "Tecno Spark 20", "price": 150000, "currency": "RWF", "in_stock": True,
             "stock_quantity": 15, "description": "6.6 inch display 128GB storage 5000mAh battery."}
    led = build_ledger(search_result(A15, tecno), {}, "Hi, do you have a phone under 300,000 RWF?", owner_text=OWNER)
    real = ("We have two phones under 300,000 RWF:\n\n1. Samsung Galaxy A15\n   - Price: RWF 210,000\n   - Features: "
            "6.5 inch display, 128GB storage, 4GB RAM, dual SIM\n   - Stock: 6 available\n\n2. Tecno Spark 20\n   - "
            "Price: RWF 150,000\n   - Features: 6.6 inch display, 128GB storage, 5000mAh battery\n   - Stock: 15 "
            "available")
    assert verify(real, led) == []
    assert "money" in kinds(real.replace("150,000", "140,000"), led)


# ---------------------------------------------------------------- the same, through the pipeline
def test_follow_up_price_reaches_the_customer_unchanged(electronics, outbox, db):
    electronics.patch("/api/business/agent-config", json={"business_rules": OWNER})
    model([call("search_products", query="laptop")], "Here are two laptops: 1. HP 250 G9 Laptop 2. Lenovo IdeaPad 3",
          "The Lenovo IdeaPad 3 is RWF 590,000. It includes a 12-month warranty.")
    electronics.send("Hi, I'm looking for a laptop")
    electronics.send("How much is the Lenovo?")
    assert last_reply(outbox) == "The Lenovo IdeaPad 3 is RWF 590,000. It includes a 12-month warranty."


def test_wrong_follow_up_price_is_replaced_by_the_current_facts_not_a_shrug(electronics, outbox, db):
    model([call("search_products", query="laptop")], "Here are two laptops: 1. HP 250 G9 Laptop 2. Lenovo IdeaPad 3",
          "The Lenovo IdeaPad 3 is RWF 499,000.")
    electronics.send("Hi, I'm looking for a laptop")
    electronics.send("How much is the Lenovo?")
    reply = last_reply(outbox)
    assert "499,000" not in reply
    assert reply.startswith("Lenovo IdeaPad 3 — RWF 590,000")


def test_untranslated_arabic_query_is_a_miss_with_a_hint_not_a_browse(electronics, db):
    svc = ProductService(db, uuid.UUID(electronics.business_id))
    assert svc.search("تلفون") == []           # used to fall through to "browse everything": 5 accessories
    assert svc.search("تلفون سامسونج", max_price=300000) == []
    r, _ = execute_tool(tool_ctx(electronics, db), "search_products", {"query": "تلفون"})
    assert r["count"] == 0 and "translated" in r["note"]


def test_empty_search_says_how_to_retry_and_which_categories_exist(fashion, electronics, db):
    """Live: "inkweto z'umukara" / "baskets noires" / "viatu vyeusi" were searched untranslated and the customer was
    told the shop has no black shoes. An empty result now carries the retry hint and the real categories."""
    ctx = tool_ctx(fashion, db)
    ctx.language = "rw"
    r, _ = execute_tool(ctx, "search_products", {"query": "inkweto z'umukara"})
    assert r["count"] == 0 and "translated" in r["note"]
    assert r["categories"] == ["Accessories", "Boots", "Dresses", "Hoodies", "Jackets", "Jeans", "Sneakers", "T-Shirts"]
    ctx.language = "en"
    r, _ = execute_tool(ctx, "search_products", {"query": "shoes"})
    assert "translated" not in r["note"] and "Sneakers" in r["categories"]  # "shoes" -> the shop says Sneakers/Boots
    assert "Phones" not in r["categories"]  # another tenant's categories never leak


def test_accents_are_folded(fashion, db):
    fashion.post("/api/products", json={"name": "Café Arabica 250g", "price": 7000, "category": "Épicerie",
                                        "sku": "CAF-1", "stock_quantity": 3})
    svc = ProductService(db, uuid.UUID(fashion.business_id))
    assert [h.product.name for h in svc.search("cafe")] == ["Café Arabica 250g"]
    assert [h.product.name for h in svc.search("café arabica")] == ["Café Arabica 250g"]
    assert [h.product.name for h in svc.search("epicerie")] == ["Café Arabica 250g"]
