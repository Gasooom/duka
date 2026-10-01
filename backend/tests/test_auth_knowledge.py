import io

from app.services.knowledge_service import chunk_text


def test_register_login_and_auth_errors(client):
    r = client.post("/api/auth/register", json={"business_name": "Shop", "email": "a@b.co", "password": "password123"})
    assert r.status_code == 201
    assert client.post("/api/auth/register", json={"business_name": "Shop2", "email": "A@b.co",
                                                   "password": "password123"}).status_code == 409
    assert client.post("/api/auth/register", json={"business_name": "S", "email": "x@b.co",
                                                   "password": "short"}).status_code == 422
    assert client.post("/api/auth/login", json={"email": "a@b.co", "password": "wrong-pass"}).status_code == 403
    tok = client.post("/api/auth/login", json={"email": "a@b.co", "password": "password123"}).json()["access_token"]
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"}).json()["business"]["name"] == "Shop"
    assert client.get("/api/products").status_code == 401
    assert client.get("/api/products", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_password_is_hashed(client, db):
    from app.models import User
    client.post("/api/auth/register", json={"business_name": "Shop", "email": "h@b.co", "password": "password123"})
    u = db.query(User).one()
    assert u.password_hash.startswith("$2") and "password123" not in u.password_hash


def test_login_rate_limited(client):
    codes = [client.post("/api/auth/login", json={"email": "n@b.co", "password": "x" * 8}).status_code
             for _ in range(25)]
    assert 429 in codes


def test_business_and_agent_config_update(fashion):
    b = fashion.patch("/api/business", json={"phone": "+250 788", "currency": "kes", "business_hours": {"Mon": "9-5"}})
    assert b.json()["currency"] == "KES" and b.json()["business_hours"] == {"Mon": "9-5"}
    cfg = fashion.patch("/api/business/agent-config", json={"greeting": "Yo!", "temperature": 0.5}).json()
    assert cfg["greeting"] == "Yo!" and cfg["temperature"] == 0.5
    assert fashion.patch("/api/business/settings", json={"payment_provider": "paypal"}).status_code == 422


def test_chunking():
    chunks = chunk_text("Para one.\n\nPara two.\n\n" + "x" * 1600)
    assert chunks[0] == "Para one.\n\nPara two." and all(len(c) <= 700 for c in chunks) and len(chunks) == 4


def test_knowledge_crud_search_and_upload(fashion):
    d = fashion.post("/api/knowledge", json={"title": "Returns", "content": "Returns accepted within 7 days."})
    assert d.status_code == 201 and d.json()["chunk_count"] == 1
    hits = fashion.get("/api/knowledge/search", params={"q": "can I return an item?"}).json()
    assert hits and "7 days" in hits[0]["content"]
    up = fashion.post("/api/knowledge/upload", files={"file": ("hours.txt", io.BytesIO(b"Open daily 9am to 8pm."))},
                      data={"title": "Hours"})
    assert up.status_code == 201 and up.json()["source_type"] == "file"
    assert fashion.post("/api/knowledge/upload", files={"file": ("x.exe", io.BytesIO(b"MZ"))}).status_code == 415
    assert fashion.delete(f"/api/knowledge/{d.json()['id']}").status_code == 204
    assert len(fashion.get("/api/knowledge").json()) == 1


def test_validation_errors_are_structured(fashion):
    r = fashion.post("/api/products", json={"name": "", "price": "abc"})
    assert r.status_code == 422 and {e["field"] for e in r.json()["errors"]} == {"name", "price"}
