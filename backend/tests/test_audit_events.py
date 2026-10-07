"""Audit trail of the changes that hurt most when made by an attacker or by mistake: payment instructions, prices,
WhatsApp numbers, passwords and the AI assistant's settings. Who and when, before and after — never a secret."""
import json
from decimal import Decimal

from sqlalchemy import select

from app import cli
from app.core.config import settings
from app.db.session import SessionLocal
from app.models import AuditEvent, User, WhatsAppAccount


def _events(action: str | None = None) -> list[AuditEvent]:
    with SessionLocal() as s:
        stmt = select(AuditEvent).order_by(AuditEvent.created_at)
        return list(s.scalars(stmt.where(AuditEvent.action == action) if action else stmt))


def _owner(t) -> User:
    with SessionLocal() as s:
        return s.scalar(select(User).where(User.business_id == t.business_id))


def test_payment_instructions_changes_are_audited_with_before_and_after(fashion):
    first, second = "MoMo 0788 123 456 (Kigali Fashion)", "MoMo 0799 999 999 (someone else)"
    assert fashion.patch("/api/business/settings", json={"payment_instructions": first}).status_code == 200
    fashion.patch("/api/business/settings", json={"payment_instructions": first})  # unchanged: nothing to record
    fashion.patch("/api/business/settings", json={"payment_instructions": second})
    events = _events("settings.payment_instructions_changed")
    assert [(e.data["from"], e.data["to"]) for e in events] == [(None, first), (first, second)]
    owner = _owner(fashion)
    assert events[0].actor_type == "user" and events[0].actor_user_id == owner.id
    assert events[0].data["actor_email"] == owner.email and str(events[0].business_id) == fashion.business_id
    assert _events("settings.updated") == []  # only what changed is recorded


def test_ai_pause_and_other_settings_changes_are_audited(fashion):
    fashion.patch("/api/business/settings", json={"ai_enabled": False, "low_stock_threshold": 3})
    fashion.patch("/api/business/settings", json={"ai_enabled": True})
    changes = [e.data["changes"] for e in _events("settings.updated")]
    assert changes == [{"ai_enabled": {"from": True, "to": False}, "low_stock_threshold": {"from": 5, "to": 3}},
                       {"ai_enabled": {"from": False, "to": True}}]


def test_price_changes_are_audited_from_the_dashboard_and_csv_import(fashion):
    p = fashion.get("/api/products").json()[0]
    fashion.patch(f"/api/products/{p['id']}", json={"name": p["name"] + " (new)"})  # no price change
    fashion.patch(f"/api/products/{p['id']}", json={"price": p["price"]})  # same price
    assert fashion.patch(f"/api/products/{p['id']}", json={"price": 12345.5}).status_code == 200
    assert fashion.import_csv(f"name,price,sku\n{p['name']},99000,{p['sku']}\n").json()["updated"] == 1
    events = _events("product.price_changed")
    assert [(Decimal(e.data["from"]), Decimal(e.data["to"]), e.data["source"]) for e in events] == [
        (Decimal(str(p["price"])), Decimal("12345.5"), "api"), (Decimal("12345.5"), Decimal("99000"), "csv_import")]
    assert all(e.entity_id is not None and e.data["sku"] == p["sku"] for e in events)
    assert {e.actor_user_id for e in events} == {_owner(fashion).id}


def test_whatsapp_numbers_are_audited_without_the_access_token(fashion):
    first, second = "EAAG-first-SECRET-token-111", "EAAG-second-SECRET-token-222"
    body = {"phone_number_id": "pnid-cloud", "display_phone_number": "+250 788 000 111", "mode": "cloud"}
    acct = fashion.post("/api/whatsapp/accounts", json={**body, "access_token": first}).json()
    fashion.post("/api/whatsapp/accounts", json={**body, "access_token": second})
    with SessionLocal() as s:
        encrypted = s.scalar(select(WhatsAppAccount.access_token_encrypted).where(WhatsAppAccount.id == acct["id"]))
    assert fashion.delete(f"/api/whatsapp/accounts/{acct['id']}").status_code == 204
    connected = _events("whatsapp.connected")  # the fixture's simulated number first
    assert [(e.data["phone_number_id"], e.data["credential"]) for e in connected] == [
        ("pnid-fashion", "unchanged"), ("pnid-cloud", "set"), ("pnid-cloud", "replaced")]
    assert connected[2].data["reconnected"] is True and connected[2].data["mode"] == "cloud"
    [gone] = _events("whatsapp.disconnected")
    assert gone.data == {"actor_email": _owner(fashion).email, "phone_number_id": "pnid-cloud", "mode": "cloud"}
    trail = json.dumps([e.data for e in _events()])
    assert first not in trail and second not in trail and encrypted not in trail and "EAAG" not in trail


def test_password_change_and_operator_reset_are_audited_without_secrets(fashion):
    owner = _owner(fashion)
    r = fashion.post("/api/auth/change-password", json={"current_password": "password123",
                                                         "new_password": "Fresh-Passw0rd-2026"})
    assert r.status_code == 200
    assert cli.main(["reset-password", "--email", owner.email, "--password", "Cli-Reset-Passw0rd-9"]) == 0
    [changed] = _events("auth.password_changed")
    assert (changed.actor_user_id, changed.entity_id) == (owner.id, owner.id)
    assert changed.data == {"actor_email": owner.email, "method": "self_service", "other_sessions_signed_out": True}
    [reset] = _events("auth.password_reset")
    assert reset.actor_type == "system" and reset.actor_user_id is None and reset.entity_id == owner.id
    assert reset.data == {"method": "cli", "generated": False, "other_sessions_signed_out": True}


def test_ai_assistant_changes_are_audited(fashion):
    patch = {"tone": "warm and brief", "temperature": 0.5, "business_rules": "No discounts above 10%.",
             "model": settings.llm_model}
    assert fashion.patch("/api/business/agent-config", json=patch).status_code == 200
    fashion.patch("/api/business/agent-config", json=patch)  # unchanged: nothing to record
    [e] = _events("agent_config.updated")
    ch = e.data["changes"]
    assert set(ch) == {"tone", "temperature", "business_rules", "model"}
    assert ch["tone"] == {"from": "friendly and concise", "to": "warm and brief"}
    assert ch["business_rules"] == {"from": None, "to": "No discounts above 10%."}
    assert ch["model"] == {"from": None, "to": settings.llm_model}
    assert (Decimal(ch["temperature"]["from"]), Decimal(ch["temperature"]["to"])) == (Decimal("0.2"), Decimal("0.5"))
    assert e.actor_user_id == _owner(fashion).id


def test_no_password_token_or_key_ever_reaches_the_audit_trail(fashion, client):
    """Every audited path at once, then the whole table is searched for every secret involved."""
    owner = _owner(fashion)
    guess, new_password, whatsapp_token = "Guessed-Passw0rd!", "Brand-New-Passw0rd-1", "EAAG-SECRET-TOKEN-XYZ"
    for _ in range(3):
        client.post("/api/auth/login", json={"email": owner.email, "password": guess})
    login_token = client.post("/api/auth/login", json={"email": owner.email, "password": "password123"}).json()
    fashion.patch("/api/business/settings", json={"payment_instructions": "MoMo 0788 1", "ai_enabled": False})
    fashion.patch("/api/business/agent-config", json={"system_prompt": "Be brief.", "temperature": 0.3})
    fashion.post("/api/whatsapp/accounts", json={"phone_number_id": "pnid-x", "mode": "cloud",
                                                 "access_token": whatsapp_token})
    p = fashion.get("/api/products").json()[0]
    fashion.patch(f"/api/products/{p['id']}", json={"price": 1})
    new_token = fashion.post("/api/auth/change-password", json={"current_password": "password123",
                                                                "new_password": new_password}).json()["access_token"]
    cli.main(["reset-password", "--email", owner.email])  # generated password, printed once to the operator
    with SessionLocal() as s:
        password_hash = s.get(User, owner.id).password_hash
        encrypted = s.scalar(select(WhatsAppAccount.access_token_encrypted)
                             .where(WhatsAppAccount.phone_number_id == "pnid-x"))
    events = _events()
    assert {"auth.login_failed", "settings.payment_instructions_changed", "settings.updated", "agent_config.updated",
            "whatsapp.connected", "product.price_changed", "auth.password_changed",
            "auth.password_reset"} <= {e.action for e in events}
    trail = json.dumps([[e.action, e.entity_type, e.actor_type, e.data] for e in events])
    secrets = {"guessed password": guess, "original password": "password123", "new password": new_password,
               "sign-in token": login_token["access_token"], "session token": fashion.token,
               "new session token": new_token, "whatsapp token": whatsapp_token, "encrypted token": encrypted,
               "password hash": password_hash, "bcrypt prefix": "$2b$", "jwt secret": settings.jwt_secret,
               "webhook secret": settings.payment_webhook_secret}
    leaked = [name for name, value in secrets.items() if value and value in trail]
    assert leaked == []
