"""M7: what a merchant needs to operate without developer access."""
import uuid

from app.core.config import settings


def test_setup_checklist_reflects_real_state(fashion):
    checks = {c["key"]: c for c in fashion.get("/api/dashboard/setup").json()["checks"]}
    assert checks["products"]["ok"] and checks["delivery"]["ok"]
    assert not checks["whatsapp"]["ok"]  # only a dev number is connected
    assert not checks["owner_alerts"]["ok"] and not checks["payment_instructions"]["ok"]
    assert not checks["platform_ai"]["ok"]  # tests run the offline rules engine
    fashion.patch("/api/business/settings", json={"owner_notification_phone": "250788000999",
                                                  "payment_instructions": "MoMo 0788 000 000"})
    fashion.post("/api/whatsapp/accounts", json={"phone_number_id": "pnid-live", "mode": "cloud",
                                                 "access_token": "EAAG-token"})
    fashion.patch("/api/business", json={"business_hours": {"Daily": "08:00-20:00"}})
    checks = {c["key"]: c for c in fashion.get("/api/dashboard/setup").json()["checks"]}
    assert all(checks[k]["ok"] for k in ("whatsapp", "owner_alerts", "payment_instructions", "hours"))
    fashion.patch("/api/business/settings", json={"ai_enabled": False})
    assert fashion.get("/api/dashboard/stats").json()["ai_enabled"] is False


def test_owner_can_change_password(fashion, client):
    email = client.get("/api/auth/me", headers=fashion.h).json()["user"]["email"]
    r = fashion.post("/api/auth/change-password", json={"current_password": "wrong", "new_password": "newpass123"})
    assert r.status_code == 403
    assert fashion.post("/api/auth/change-password",
                        json={"current_password": "password123", "new_password": "short"}).status_code == 422
    r = fashion.post("/api/auth/change-password", json={"current_password": "password123", "new_password": "newpass123"})
    assert r.status_code == 200
    assert fashion.get("/api/orders").status_code == 401, "old sessions are signed out"
    assert client.get("/api/orders", headers={"Authorization": f"Bearer {r.json()['access_token']}"}).status_code == 200
    assert client.post("/api/auth/login", json={"email": email, "password": "password123"}).status_code == 403
    assert client.post("/api/auth/login", json={"email": email, "password": "newpass123"}).status_code == 200


def test_operator_can_reset_a_forgotten_password(fashion, client, capsys):
    from app.cli import main
    email = client.get("/api/auth/me", headers=fashion.h).json()["user"]["email"]
    assert main(["reset-password", "--email", email]) == 0
    assert fashion.get("/api/orders").status_code == 401, "a reset signs out every session"
    password = capsys.readouterr().out.split("shown once): ")[1].strip()
    assert client.post("/api/auth/login", json={"email": email, "password": password}).status_code == 200
    assert main(["reset-password", "--email", f"nobody-{uuid.uuid4().hex[:4]}@x.dev"]) == 1


def test_dev_tools_flag_is_exposed_and_off_in_production(fashion, monkeypatch):
    assert fashion.get("/api/auth/me").json()["features"]["dev_tools"] is True
    monkeypatch.setattr(settings, "app_env", "production")
    assert fashion.get("/api/auth/me").json()["features"] == {"dev_tools": False, "registration_open": False}
