"""Catalog retrieval returns a product only when the query gives real lexical evidence for it.

Regression for a failure found with the real model: "Hi, do you have a phone under 300,000 RWF?" made it call
search_products(query="phone", max_price=300000) in a store that sells coffee, tea, honey, a bottle and a tote bag,
and the search answered "Rwandan Tea 250g". The hash embedding is lexical feature hashing: "phone" and the tea share
no word, yet their vectors collide (cosine 0.46 > the 0.30 vector threshold), and the re-rank kept vector-only hits
whenever nothing matched a query word. Grounding cannot repair that: its fallback renders the same tool result.
"""
import re
import uuid

import pytest
from sqlalchemy import select

from app.agents.grounding import build_ledger, verify
from app.agents.render import render_tool_result
from app.i18n import t
from app.models import AgentRun, Business, Customer
from app.services.conversation_service import ConversationService
from app.services.embeddings import HashingEmbedder
from app.services.knowledge_service import KnowledgeService
from app.services.product_service import ProductService, product_embedding_text, query_terms
from app.tools.registry import ToolContext, execute_tool
from tests.conftest import BACKEND, Tenant
from tests.test_ai_safety import call, model

NUMBER = "250788111222"
GROCERY_CSV = (BACKEND / "seed/data/demo_store_products.csv").read_text()
PHONE_QUESTION = "Hi, do you have a phone under 300,000 RWF?"


@pytest.fixture
def grocery(client):
    """The demo store from the seed: Coffee, Tea, Honey (Groceries), Water Bottle, Tote Bag (Home). No phones."""
    t_ = Tenant(client, "Duka Demo Store", "pnid-grocery")
    assert t_.import_csv(GROCERY_CSV).json()["created"] == 5
    t_.zone("Kigali", 1500, ["Kigali", "Remera"], True)
    return t_


def search(tenant, db, query, **kw):
    return [h.product.name for h in ProductService(db, uuid.UUID(tenant.business_id)).search(query, **kw)]


def last_reply(outbox):
    return [b for to, b in outbox.sent if to == NUMBER][-1]


def tool_ctx(tenant, db):
    tenant.send("hello there", from_number="250788000777")
    biz = db.get(Business, uuid.UUID(tenant.business_id))
    customer = db.scalars(select(Customer).where(Customer.business_id == biz.id)).one()
    return ToolContext(db=db, business=biz, customer=customer,
                       conversation=ConversationService(db, biz.id).get_or_create_active(customer))


# ---------------------------------------------------------------- the root cause
def test_hash_vector_similarity_is_not_evidence_of_relevance():
    """Why vector-only hits can never be accepted with the hash embedder: an unrelated product can score higher
    than a real match. (If this stops holding, the embedder changed; the search rule below still applies.)"""
    e = HashingEmbedder(384)

    def cos(a, b):
        return sum(x * y for x, y in zip(e.embed_one(a), e.embed_one(b)))

    tea = product_embedding_text("Rwandan Tea 250g", "Groceries", "Premium black tea from Rubaya.")
    jacket = product_embedding_text("Denim Jacket", "Jackets", "Classic blue denim trucker jacket.")
    assert cos("phone", tea) > 0.3  # no shared word: a pure hash collision
    assert cos("jakcet", jacket) < 0.3  # a genuine (misspelt) match scores lower than the collision


# ---------------------------------------------------------------- 1-3: unrelated products are never returned
@pytest.mark.parametrize("query,kw", [
    ("phone", {"max_price": 300000}),          # the exact failing tool call
    ("phone", {}),
    (PHONE_QUESTION, {}),
    ("Samsung phone", {}),
    ("smartphone", {"max_price": 300000}),
    ("shoes", {}),
    ("black dress", {}),                       # the tea is "black", but it is not a dress
    ("laptop", {"max_price": 1000000}),
    ("iphone 13", {}),
    ("submarine periscope", {}),
])
def test_queries_for_things_the_store_does_not_sell_return_nothing(grocery, db, query, kw):
    assert search(grocery, db, query, **kw) == []


def test_coffee_returns_only_coffee(grocery, db):
    assert search(grocery, db, "coffee") == ["Organic Coffee Beans 500g"]
    assert search(grocery, db, "coffee", limit=10) == ["Organic Coffee Beans 500g"]  # never the water bottle


def test_shoes_and_phones_do_not_return_groceries_in_other_stores(fashion, electronics, db):
    assert search(fashion, db, "phone") == []
    assert search(fashion, db, "coffee") == []
    assert search(electronics, db, "black dress") == []
    assert search(electronics, db, "sneakers") == []
    # A description that mentions another product does not make it that product.
    assert search(fashion, db, "laptop") == []  # "Canvas Backpack ... with laptop sleeve"
    assert search(electronics, db, "phone", max_price=100000) == []  # "Fast charger for phones and tablets"


# ---------------------------------------------------------------- 4-5: hard filters
def test_price_filter_is_strict(electronics, db):
    names = search(electronics, db, "phone", max_price=300000, limit=10)
    assert set(names) == {"Tecno Spark 20", "Samsung Galaxy A15"}
    assert set(search(electronics, db, "phone", min_price=300000, limit=10)) == {"Samsung Galaxy A35 5G",
                                                                                 "iPhone 13 128GB"}
    assert search(electronics, db, "phone", max_price=100000) == []  # no fallback to cheaper unrelated items


def test_phone_matches_phones_not_things_that_mention_phones(electronics, db):
    # The charger's description says "Fast charger for phones": a description-only mention never outranks or
    # joins products that ARE phones (name/category evidence).
    names = search(electronics, db, "phone", limit=10)
    assert set(names) == {"Samsung Galaxy A15", "Samsung Galaxy A35 5G", "Tecno Spark 20", "iPhone 13 128GB"}
    assert "Anker 20W USB-C Charger" not in names


def test_category_filter_is_strict(fashion, db):
    assert set(search(fashion, db, "black", category="Sneakers", limit=10)) == {
        "Nike Air Max 90 Black", "Adidas Samba OG Black", "Puma Suede Classic Black",
        "Converse Chuck Taylor High Black", "Vans Old Skool Black/White"}
    assert set(search(fashion, db, "", category="Dresses", limit=10)) == {"Kitenge Wrap Dress", "Black Evening Dress"}
    assert search(fashion, db, "black", category="Groceries") == []


# ---------------------------------------------------------------- 6-8: exact, partial, no match
def test_exact_name_and_sku(fashion, db):
    assert search(fashion, db, "Adidas Samba OG Black") == ["Adidas Samba OG Black"]
    assert search(fashion, db, "KF-SAMBA-BLK") == ["Adidas Samba OG Black"]


def test_partial_words_plurals_and_compounds(fashion, electronics, grocery, db):
    assert search(fashion, db, "samba") == ["Adidas Samba OG Black"]
    assert set(search(electronics, db, "galaxy")) == {"Samsung Galaxy A15", "Samsung Galaxy A35 5G"}
    assert set(search(electronics, db, "sams", limit=10)) == {"Samsung Galaxy A15", "Samsung Galaxy A35 5G",
                                                             "Samsung 43 inch Smart TV"}  # prefix of "samsung"
    assert set(search(fashion, db, "tshirt")) == {"Classic Black T-Shirt", "White Oversized T-Shirt"}
    assert set(search(fashion, db, "dresses")) == {"Kitenge Wrap Dress", "Black Evening Dress"}
    assert search(grocery, db, "bags") == ["Cotton Tote Bag"]


def test_colour_words_never_make_a_match_on_their_own(fashion, db):
    # Black hoodies, caps and t-shirts are black but are not dresses.
    assert search(fashion, db, "black dress") == ["Black Evening Dress"]
    # No red dress: the dresses are returned as partial matches (the tool marks "red" as missing), never black
    # things or other red things.
    assert set(search(fashion, db, "red dress")) == {"Kitenge Wrap Dress", "Black Evening Dress"}
    # A query that is only a colour searches by the colour.
    assert "Black Hoodie" in search(fashion, db, "black", limit=10)


def test_no_match_is_empty(fashion, db):
    assert search(fashion, db, "submarine periscope") == []
    assert search(fashion, db, "wedding cake") == []


# ---------------------------------------------------------------- 9: browsing without words still works
def test_filter_only_browsing(fashion, db):
    hits = ProductService(db, uuid.UUID(fashion.business_id)).search("", max_price=15000, limit=10)
    prices = [float(h.product.price) for h in hits]
    assert prices == sorted(prices) and prices and max(prices) <= 15000
    # Only budget/filler words: a browse, not a search for the word "50k" or "products"
    assert search(fashion, db, "what do you have under 50k?", max_price=50000) == \
        search(fashion, db, "", max_price=50000)
    assert search(fashion, db, "products", max_price=50000) == search(fashion, db, "", max_price=50000)


def test_query_terms():
    assert query_terms("Hi, I'm looking for black sneakers under 100k RWF.") == ["black", "sneaker"]
    assert query_terms("shoes") == ["shoe"]  # not "sho" (which would match "shorts", "shop")
    assert query_terms("dresses glasses batteries bags") == ["dress", "glass", "battery", "bag"]
    assert query_terms("do you have any products in stock?") == []


# ---------------------------------------------------------------- 10: tenant isolation
def test_search_is_tenant_scoped(fashion, electronics, db):
    assert search(fashion, db, "Samsung Galaxy") == []
    assert search(electronics, db, "samba") == []
    assert search(electronics, db, "", max_price=20000, limit=10) == ["Anker 20W USB-C Charger"]


# ---------------------------------------------------------------- 11-12: positions and cart references
def test_tool_positions_and_partial_matches_are_explicit(fashion, db):
    ctx = tool_ctx(fashion, db)
    r, _ = execute_tool(ctx, "search_products", {"query": "black sneakers", "max_price": 100000})
    assert [p["position"] for p in r["products"]] == [1, 2, 3, 4]
    assert [p["name"] for p in r["products"]] == [s["name"] for s in ctx.conversation.state["last_products"]]
    assert all("missing" not in p for p in r["products"])
    r, _ = execute_tool(ctx, "search_products", {"query": "red dress"})
    assert r["count"] == 2 and all(p["missing"] == ["red"] for p in r["products"])  # not claimed to be red
    assert "missing" in r["note"]
    rendered = render_tool_result("search_products", {}, r | {"ok": True}, "en")
    assert rendered.startswith(t("search_partial", "en", count=2))  # never "Here's what I found" for a partial


def test_partial_matches_never_borrow_the_missing_word(electronics, db):
    hits = ProductService(db, uuid.UUID(electronics.business_id)).search("laptop bag", limit=10)
    assert {h.product.name for h in hits} == {"HP 250 G9 Laptop", "Lenovo IdeaPad 3"}
    assert all(h.missing == ["bag"] for h in hits)


def test_positions_and_cart_references_survive_an_empty_search(fashion, outbox, db):
    fashion.send("black sneakers under 100k")
    listed = re.findall(r"^(\d+)\. (.+?) — ", last_reply(outbox), re.M)
    assert [n for n, _ in listed] == ["1", "2", "3", "4"]
    fashion.send("do you have a phone?")
    assert last_reply(outbox) == t("search_none", "en")
    fashion.send("add 2")  # the customer means the 2nd item they SAW; the empty search showed nothing
    assert listed[1][1] in last_reply(outbox)
    assert "Added" in last_reply(outbox) or "added" in last_reply(outbox).lower()


# ---------------------------------------------------------------- the same rule for policy knowledge
def test_knowledge_search_needs_a_shared_word(fashion, db):
    svc = KnowledgeService(db, uuid.UUID(fashion.business_id))
    svc.add_document("Returns", "You can return unworn items within 7 days with the receipt.")
    svc.add_document("Delivery", "We deliver in Kigali the same day. Outside Kigali takes 2 days.")
    assert [h.document_title for h in svc.search("can I return my shoes?")] == ["Returns"]
    assert svc.search("delivery to Kigali")[0].document_title == "Delivery"
    assert svc.search("phone warranty") == []  # no shared word: no policy text for the model to misapply


# ---------------------------------------------------------------- the exact failure, end to end
def test_phone_question_gets_a_truthful_no_match_offline(grocery, outbox):
    grocery.send(PHONE_QUESTION)
    assert last_reply(outbox) == t("search_none", "en")


def test_phone_question_gets_a_truthful_no_match_even_if_the_model_recommends_tea(grocery, outbox, db):
    model([call("search_products", query="phone", max_price=300000)],
          "We don't have phones, but we have Rwandan Tea 250g for RWF 4,000!")
    grocery.send(PHONE_QUESTION)
    reply = last_reply(outbox)
    assert "Tea" not in reply and "4,000" not in reply
    assert reply == t("search_none", "en")
    run = db.scalars(select(AgentRun).order_by(AgentRun.created_at.desc())).first()
    tool = next(s for s in run.steps if s["type"] == "tool")
    assert tool["result"]["count"] == 0  # the tool told the model the truth
    assert any(s["type"] == "grounding" for s in run.steps)  # and the invented offer was blocked


def test_phone_question_honest_model_reply_is_sent_unchanged(grocery, outbox):
    honest = "Sorry, we don't have any phones under RWF 300,000."
    model([call("search_products", query="phone", max_price=300000)], honest)
    grocery.send(PHONE_QUESTION)
    assert last_reply(outbox) == honest


def test_partial_result_cannot_be_presented_as_what_was_asked():
    result = {"ok": True, "count": 1, "products": [{"position": 1, "name": "Kitenge Wrap Dress", "price": 45000,
                                                     "currency": "RWF", "in_stock": True, "stock_quantity": 10,
                                                     "missing": ["red"]}]}
    led = build_ledger([("search_products", {"query": "red dress"}, result)], {}, "do you have a red dress?")
    bad = "Yes! We have a red dress: the Kitenge Wrap Dress for RWF 45,000."
    assert {v.kind for v in verify(bad, led)} == {"attribute"}
    assert verify("We don't have a red dress, but the Kitenge Wrap Dress is RWF 45,000.", led) == []
    assert verify("The closest is the Kitenge Wrap Dress (RWF 45,000); it is not red.", led) == []
    assert verify("The Kitenge Wrap Dress has a reduced price of RWF 45,000.", led) == []  # "reduced" is not "red"


def test_model_presenting_a_partial_match_as_the_request_is_replaced(fashion, outbox):
    model([call("search_products", query="red dress")], "Yes! We have a red dress: the Kitenge Wrap Dress, RWF 45,000.")
    fashion.send("do you have a red dress?")
    assert last_reply(outbox).startswith(t("search_partial", "en", count=2))


def test_contracted_negations_are_negations():
    """"isn't available" / "hasn't been paid" are truthful; the regexes used to miss "n't" (no word boundary
    before it), so honest replies were rejected as availability or payment claims."""
    samba = {"position": 1, "name": "Adidas Samba OG Black", "price": 95000, "currency": "RWF", "in_stock": False,
             "stock_quantity": 0}
    led = build_ledger([("search_products", {"query": "samba"}, {"ok": True, "count": 1, "products": [samba]})],
                       {}, "is the samba available?")
    assert verify("Sorry, the Adidas Samba OG Black isn't available right now.", led) == []
    assert {v.kind for v in verify("The Adidas Samba OG Black is available!", led)} == {"availability"}
    order = {"order_number": "KF-00012", "status": "pending", "payment_status": "unpaid", "total": 97000}
    led = build_ledger([("check_order_status", {}, {"ok": True, **order})], {}, "is my order paid?")
    assert verify("Your order KF-00012 hasn't been paid yet.", led) == []
    assert {v.kind for v in verify("Your order KF-00012 has been paid.", led)} == {"payment_status"}


def test_specifications_with_units_are_checked_as_numbers_not_prices():
    phone = {"position": 1, "name": "Samsung Galaxy A15", "price": 210000, "currency": "RWF", "in_stock": True,
             "stock_quantity": 10, "description": "6.5 inch display 128GB storage 4GB RAM dual SIM smartphone."}
    led = build_ledger([("search_products", {"query": "samsung"}, {"ok": True, "count": 1, "products": [phone]})],
                       {}, "samsung phone?")
    assert verify("The Samsung Galaxy A15 (128GB, 6.5 inch) costs RWF 210,000.", led) == []
    assert {v.kind for v in verify("The Samsung Galaxy A15 (256GB) costs RWF 210,000.", led)} == {"number"}
    assert "money" in {v.kind for v in verify("The Samsung Galaxy A15 costs RWF 128,000.", led)}


def test_irrelevant_product_with_a_correct_price_is_still_ungrounded_when_search_was_empty():
    led = build_ledger([("search_products", {"query": "phone"}, {"ok": True, "count": 0, "products": []})],
                       {}, PHONE_QUESTION)
    kinds = {v.kind for v in verify("Yes, we have Rwandan Tea 250g for RWF 4,000.", led)}
    assert {"money", "availability"} <= kinds
