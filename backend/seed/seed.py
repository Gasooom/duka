"""Seed development tenants. Idempotent: existing tenants (by owner email) are skipped.

Each tenant below is *pure configuration + data* — the same engine/tools serve all of them.
Run:  python -m seed.seed      (from backend/)   or   make seed
"""
from pathlib import Path

from sqlalchemy import select

from app.db.session import session_scope
from app.models import User
from app.services.business_service import BusinessConfigService, register_business
from app.services.commerce_service import CartService, CheckoutService, OrderService
from app.services.conversation_service import ConversationService, CustomerService
from app.services.knowledge_service import KnowledgeService
from app.services.product_service import ProductService

DATA = Path(__file__).parent / "data"
PASSWORD = "password123"

TENANTS = [
    {
        "business_name": "Demo Store", "email": "demo@duka.dev", "business_type": "grocery",
        "profile": {"description": "Local Rwandan pantry goods and home essentials.", "phone": "+250788000001",
                    "address": "KN 4 Ave, Kigali", "business_hours": {"Mon-Sat": "08:00-20:00", "Sun": "10:00-16:00"},
                    "order_prefix": "DS"},
        "agent": {"tone": "warm and helpful"},
        "csv": "demo_store_products.csv",
        "zones": [{"name": "Kigali", "fee": 1500, "areas": ["Kigali", "Kimihurura", "Remera", "Nyamirambo"],
                   "estimated_time": "same day", "is_default": True}],
        "knowledge": [("Delivery & returns", "We deliver within Kigali only, same day for orders before 3pm. "
                                             "Unopened items can be returned within 7 days.")],
        "whatsapp": {"phone_number_id": "dev-demo-store", "display_phone_number": "+250 700 000 001"},
        "settings": {"payment_instructions": "MTN MoMo to 0788 000 001 (Demo Store)"},
        "sample_orders": True,
    },
    {
        "business_name": "Kigali Fashion", "email": "fashion@duka.dev", "business_type": "clothing",
        "profile": {"description": "Sneakers, streetwear and African print fashion in Kigali.",
                    "phone": "+250788123456", "address": "Kigali Heights, KG 7 Ave, Kigali",
                    "business_hours": {"Mon-Sat": "09:00-20:00", "Sun": "12:00-18:00"}, "order_prefix": "KF"},
        "agent": {"tone": "friendly, stylish and concise",
                  "greeting": "Muraho! 👋 Welcome to Kigali Fashion. Looking for sneakers, streetwear or something "
                              "special today?",
                  "business_rules": "Exchanges for a different size are free within 7 days. "
                                    "Never promise sizes that are not listed in the product description."},
        "csv": "kigali_fashion_products.csv",
        "zones": [
            {"name": "Kigali City", "fee": 2000, "areas": ["Kigali", "Kimihurura", "Kacyiru", "Remera", "Kicukiro",
                                                           "Nyarutarama", "Gisozi", "Nyamirambo"],
             "estimated_time": "same day", "is_default": True},
            {"name": "Outside Kigali", "fee": 5000, "areas": ["Musanze", "Huye", "Rubavu", "Muhanga", "Rwamagana"],
             "estimated_time": "1-2 days"},
        ],
        "knowledge": [
            ("Delivery policy", "We deliver across Kigali the same day for orders placed before 4pm (2,000 RWF). "
                                "We also deliver outside Kigali to Musanze, Huye, Rubavu, Muhanga and Rwamagana "
                                "within 1-2 days for 5,000 RWF via bus courier."),
            ("Returns and exchanges", "Size exchanges are free within 7 days if the item is unworn with tags. "
                                      "Refunds are issued to mobile money within 3 working days."),
        ],
        "whatsapp": {"phone_number_id": "dev-kigali-fashion", "display_phone_number": "+250 700 000 002"},
        "settings": {"payment_instructions": "MTN MoMo to 0788 123 456 (Kigali Fashion Ltd)"},
    },
    {
        "business_name": "Mama's Electronics", "email": "electronics@duka.dev", "business_type": "electronics",
        "profile": {"description": "Phones, laptops and accessories with genuine warranty.",
                    "phone": "+250788654321", "address": "Downtown, KN 2 St, Kigali",
                    "business_hours": {"Mon-Fri": "08:30-19:00", "Sat": "09:00-17:00"}, "order_prefix": "ME"},
        "agent": {"tone": "professional and precise",
                  "greeting": "Hello and welcome to Mama's Electronics! Which device can I help you find?",
                  "business_rules": "All phones and laptops include a 12-month warranty. "
                                    "Always mention the warranty when recommending phones or laptops."},
        "csv": "mamas_electronics_products.csv",
        "zones": [{"name": "Kigali", "fee": 3000, "areas": ["Kigali", "Remera", "Kicukiro", "Nyamirambo", "Gikondo"],
                   "estimated_time": "within 24h", "is_default": True}],
        "knowledge": [("Warranty", "Phones and laptops come with a 12-month warranty covering manufacturing "
                                   "defects. Water and screen damage are not covered. Bring the device with the "
                                   "receipt to our downtown shop."),
                      ("Delivery", "We deliver within Kigali only, within 24 hours, for 3,000 RWF.")],
        "whatsapp": {"phone_number_id": "dev-mamas-electronics", "display_phone_number": "+250 700 000 003"},
        "settings": {"payment_instructions": "MTN MoMo to 0788 654 321 (Mama's Electronics)"},
    },
]


def seed_tenant(db, t: dict) -> str:
    if db.scalar(select(User).where(User.email == t["email"])):
        return f"skip   {t['business_name']} (exists)"
    business, _, _ = register_business(db, business_name=t["business_name"], email=t["email"], password=PASSWORD,
                                       business_type=t["business_type"])
    cfg = BusinessConfigService(db, business.id)
    cfg.update_business(t["profile"])
    cfg.update_agent_config(t["agent"])
    for z in t["zones"]:
        cfg.upsert_delivery_zone(z)
    cfg.connect_whatsapp(mode="dev", access_token=None, waba_id=None, **t["whatsapp"])
    cfg.update_settings(t.get("settings", {}))
    result = ProductService(db, business.id).import_csv((DATA / t["csv"]).read_bytes())
    if result.errors:
        raise RuntimeError(f"Seed CSV invalid for {t['business_name']}: {result.errors}")
    ks = KnowledgeService(db, business.id)
    for title, content in t["knowledge"]:
        ks.add_document(title, content)
    if t.get("sample_orders"):
        _sample_orders(db, business.id)
    return f"seeded {t['business_name']}: {result.created} products, login {t['email']} / {PASSWORD}"


def _sample_orders(db, business_id) -> None:
    customers = CustomerService(db, business_id)
    products = ProductService(db, business_id).list()
    for i, (number, name) in enumerate([("250788100200", "Aline"), ("250788300400", "Eric")]):
        c = customers.upsert_from_whatsapp(number, name)
        conv = ConversationService(db, business_id).get_or_create_active(c)
        carts = CartService(db, business_id)
        cart = carts.get_active(c, conv)
        carts.add_item(cart, products[i], 1)
        CheckoutService(db, business_id).prepare(c, conv, delivery_address="Remera, KG 11 Ave")
        OrderService(db, business_id).create_from_cart(c, conv)


def main() -> None:
    for t in TENANTS:
        with session_scope() as db:
            print(seed_tenant(db, t))


if __name__ == "__main__":
    main()
