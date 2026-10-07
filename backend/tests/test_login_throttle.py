"""Sign-in throttling without Redis: per client address (as seen by the trusted proxy, so a forged X-Forwarded-For
gives no fresh allowance) and per account with progressive backoff (whoever is asking). Unknown emails behave
exactly like real ones. Failed sign-ins are logged without the password or the email and audited."""
import json
import logging
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.deps import client_ip
from app.core import security
from app.core.config import settings
from app.core.logging import JsonFormatter
from app.core.ratelimit import FailureBackoff, login_backoff
from app.db.session import SessionLocal
from app.main import app
from app.models import AuditEvent

PASSWORD = "password123"
INVALID = {"detail": "Invalid email or password", "code": "invalid_credentials"}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(login_backoff, "clock", c)
    return c


def _owner(client) -> str:
    email = f"owner-{uuid.uuid4().hex[:6]}@shop.dev"
    r = client.post("/api/auth/register", json={"business_name": "Throttle Shop", "email": email, "password": PASSWORD})
    assert r.status_code == 201
    return email


def _login(client, email, password, **headers):
    return client.post("/api/auth/login", json={"email": email, "password": password}, headers=headers)


def _peer(host: str) -> TestClient:
    """A client connecting from `host` (the socket address the server sees)."""
    return TestClient(app, client=(host, 40000))


def test_repeated_failures_lock_the_account_with_growing_waits(client, clock):
    email = _owner(client)
    for _ in range(5):  # the first five mistakes cost nothing
        r = _login(client, email, "wrong-password")
        assert (r.status_code, r.json()) == (403, INVALID)
    assert _login(client, email, "wrong-password").status_code == 403  # sixth: account locked for 30 s
    locked = _login(client, email, PASSWORD)  # even the right password waits: no guessing while locked
    assert locked.status_code == 429 and locked.headers["Retry-After"] == "30"
    assert locked.json()["detail"] == "Too many failed sign-in attempts. Try again in 1 minute."
    clock.now += 31
    assert _login(client, email, "wrong-password").status_code == 403  # seventh: 60 s
    assert _login(client, email, PASSWORD).headers["Retry-After"] == "60"
    clock.now += 61
    assert _login(client, email, "wrong-password").status_code == 403  # eighth: 120 s
    assert _login(client, email, PASSWORD).headers["Retry-After"] == "120"
    clock.now += 121
    assert _login(client, email, PASSWORD).status_code == 200  # success clears the account's record
    assert _login(client, email, "wrong-password").status_code == 403
    assert _login(client, email, PASSWORD).status_code == 200


def test_backoff_is_capped_and_forgotten_after_a_quiet_hour():
    c = Clock()
    b = FailureBackoff(free=2, base_seconds=10, max_seconds=60, forget_seconds=3600, clock=c)
    waits = [b.failure("acct")[1] for _ in range(8)]
    assert waits == [0, 0, 10, 20, 40, 60, 60, 60]
    assert b.retry_after("acct") == 60
    c.now += 60 + 3601
    assert b.retry_after("acct") == 0 and b.failure("acct") == (1, 0.0)


def test_backoff_memory_is_bounded():
    b = FailureBackoff(max_keys=100)
    for i in range(1000):
        b.failure(f"user{i}@x.test")
    assert len(b._state) == 100 and b.retry_after("user999@x.test") == 0 and "user0@x.test" not in b._state


def test_account_lock_holds_across_client_addresses_and_forged_headers(client, clock, monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    email = _owner(client)
    for i in range(6):  # a botnet: every attempt from another address, each with a forged header
        r = _login(_peer(f"10.0.0.{i}"), email, "wrong-password", **{"X-Forwarded-For": f"1.1.1.{i}, 172.16.0.{i}"})
        assert r.status_code == 403
    r = _login(_peer("10.9.9.9"), email, PASSWORD, **{"X-Forwarded-For": "8.8.8.8"})
    assert r.status_code == 429


def test_unknown_emails_are_throttled_exactly_like_real_ones(client, clock):
    real, ghost = _owner(client), "nobody-here@shop.dev"
    for _ in range(6):
        a, b = _login(client, real, "wrong-password"), _login(client, ghost, "wrong-password")
        assert (a.status_code, a.json()) == (b.status_code, b.json()) == (403, INVALID)
    a, b = _login(client, real, PASSWORD), _login(client, ghost, PASSWORD)
    assert (a.status_code, a.json(), a.headers["Retry-After"]) == (b.status_code, b.json(), b.headers["Retry-After"])
    assert a.status_code == 429


def test_unknown_email_costs_the_same_password_check(client, monkeypatch):
    calls = []
    real = security.verify_password
    monkeypatch.setattr(security, "verify_password", lambda p, h: calls.append(h) or real(p, h))
    assert _login(client, "nobody-here@shop.dev", "whatever-password").status_code == 403
    assert len(calls) == 1 and calls[0].startswith("$2b$")  # a real bcrypt comparison, not a shortcut


def test_ip_limit_counts_the_real_client_behind_the_proxy(client, monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    proxy = _peer("10.1.0.1")  # the proxy appends the address it saw; the attacker controls everything left of it
    for i in range(20):
        r = _login(proxy, f"guess{i}@shop.dev", "x", **{"X-Forwarded-For": f"203.0.113.{i}, 198.51.100.7"})
        assert r.status_code == 403
    r = _login(proxy, "guess-more@shop.dev", "x", **{"X-Forwarded-For": "203.0.113.99, 198.51.100.7"})
    assert r.status_code == 429 and r.json()["detail"] == "Too many attempts, try again in a minute"
    other = _login(proxy, "someone@shop.dev", "x", **{"X-Forwarded-For": "192.0.2.44"})
    assert other.status_code == 403  # a different real client has its own allowance


def test_without_trusted_proxies_the_forwarded_header_is_ignored(client, monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_hops", 0)
    direct = _peer("192.0.2.10")
    for i in range(20):
        assert _login(direct, f"guess{i}@shop.dev", "x", **{"X-Forwarded-For": f"203.0.113.{i}"}).status_code == 403
    assert _login(direct, "guess-more@shop.dev", "x", **{"X-Forwarded-For": "203.0.113.200"}).status_code == 429


@pytest.mark.parametrize("hops, header, expected", [
    (0, "203.0.113.5", "192.0.2.10"),
    (1, "203.0.113.5", "203.0.113.5"),
    (1, "6.6.6.6, 203.0.113.5", "203.0.113.5"),
    (2, "6.6.6.6, 203.0.113.5, 10.0.0.2", "203.0.113.5"),
    (2, "203.0.113.5", "203.0.113.5"),
    (1, None, "192.0.2.10"),
])
def test_client_ip_resolution(monkeypatch, hops, header, expected):
    from starlette.requests import Request
    monkeypatch.setattr(settings, "trusted_proxy_hops", hops)
    headers = [(b"x-forwarded-for", header.encode())] if header else []
    request = Request({"type": "http", "headers": headers, "client": ("192.0.2.10", 5000)})
    assert client_ip(request) == expected


def test_failed_sign_in_is_logged_safely_and_audited(client, clock, caplog):
    email = _owner(client)
    guess = "Kigali-Secret-Guess-42"
    with caplog.at_level(logging.WARNING, logger="app"):
        for _ in range(6):
            _login(client, email, guess)
        _login(client, email, guess)  # locked: logged as throttled, not audited
        _login(client, "nobody-here@shop.dev", guess)
    lines = [JsonFormatter().format(r) for r in caplog.records]
    failed = [json.loads(line) for line in lines if '"auth.login_failed"' in line]
    assert len(failed) == 7 and any('"auth.login_throttled"' in line for line in lines)
    assert failed[0]["account"] == security.pseudonym(email) and failed[0]["known_account"] is True
    assert failed[-1]["known_account"] is False
    assert failed[5]["failures"] == 6 and failed[5]["lockout_seconds"] == 30
    text = "\n".join(lines)
    assert guess not in text and email not in text and "nobody-here" not in text
    with SessionLocal() as s:
        events = list(s.scalars(select(AuditEvent).where(AuditEvent.action == "auth.login_failed")
                                .order_by(AuditEvent.created_at)))
    assert len(events) == 6  # the real account's six checked attempts; the unknown email has no tenant
    assert events[0].actor_type == "anonymous" and events[0].entity_type == "user"
    assert events[-1].data == {"client_ip": "testclient", "failures": 6, "lockout_seconds": 30}
    assert guess not in json.dumps([e.data for e in events])


def test_wrong_current_password_counts_against_the_account(client, clock):
    email = _owner(client)
    token = _login(client, email, PASSWORD).json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    for _ in range(6):
        r = client.post("/api/auth/change-password", headers=auth,
                        json={"current_password": "not-my-password", "new_password": "a-new-password"})
        assert r.status_code == 403
    r = client.post("/api/auth/change-password", headers=auth,
                    json={"current_password": PASSWORD, "new_password": "a-new-password"})
    assert r.status_code == 429
    assert _login(client, email, PASSWORD).status_code == 429  # the same account: sign-in is locked too
