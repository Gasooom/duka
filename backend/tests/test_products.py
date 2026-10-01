import uuid

from app.services.product_service import ProductService, query_terms


def test_product_crud_and_inventory_ledger(fashion):
    r = fashion.post("/api/products", json={"name": "Test Hat", "price": 5000, "category": "Hats",
                                            "sku": "HAT-1", "stock_quantity": 3})
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["currency"] == "RWF" and p["category"] == "Hats"
    assert fashion.post("/api/products", json={"name": "Dup", "price": 1, "sku": "HAT-1"}).status_code == 409
    assert fashion.post("/api/products", json={"name": "Neg", "price": -1}).status_code == 422

    p = fashion.patch(f"/api/products/{p['id']}", json={"price": 6000, "active": False}).json()
    assert p["price"] == 6000 and p["active"] is False
    assert fashion.post(f"/api/products/{p['id']}/stock", json={"change": 4}).json()["stock_quantity"] == 7
    assert fashion.post(f"/api/products/{p['id']}/stock", json={"change": -100}).status_code == 422
    ledger = fashion.get(f"/api/products/{p['id']}/inventory").json()
    assert [m["change"] for m in ledger] == [4, 3]
    assert ledger[0]["balance_after"] == 7
    assert fashion.delete(f"/api/products/{p['id']}").status_code == 204
    assert fashion.get(f"/api/products/{p['id']}").status_code == 404


def test_csv_import_reports_errors_and_is_atomic(fashion):
    bad = ("name,description,price,category,sku,stock_quantity\n"
           "Good Shoe,ok,1000,Shoes,GS1,2\n"
           ",missing name,1000,Shoes,GS2,2\n"
           "Bad Price,x,abc,Shoes,GS3,1\n"
           "Bad Stock,x,10,Shoes,GS4,-3\n"
           "Dup,x,10,Shoes,GS1,1\n")
    before = len(fashion.get("/api/products").json())
    r = fashion.import_csv(bad).json()
    assert r["imported"] is False
    fields = {(e["row"], e["field"]) for e in r["errors"]}
    assert fields == {(3, "name"), (4, "price"), (5, "stock_quantity"), (6, "sku")}
    assert len(fashion.get("/api/products").json()) == before  # nothing written

    r = fashion.import_csv(bad, skip_invalid=True).json()
    assert r["imported"] is True and r["created"] == 1


def test_csv_import_missing_columns_and_upsert(fashion):
    r = fashion.import_csv("title,cost\nX,1\n")
    assert r.status_code == 422 and "missing required column" in r.json()["detail"]
    r = fashion.import_csv("name,price,sku,stock_quantity\nAdidas Samba OG Black,90000,KF-SAMBA-BLK,9\n").json()
    assert r["updated"] == 1 and r["created"] == 0
    samba = [p for p in fashion.get("/api/products", params={"q": "Samba"}).json()][0]
    assert samba["price"] == 90000 and samba["stock_quantity"] == 9


def test_query_terms_strip_noise():
    assert query_terms("Hi, I'm looking for black sneakers under 100,000 RWF.") == ["black", "sneaker"]


def test_search_precision_and_price_filter(fashion, db):
    svc = ProductService(db, uuid.UUID(fashion.business_id))
    hits = svc.search("black sneakers", max_price=100000)
    names = [h.product.name for h in hits]
    assert set(names) == {"Adidas Samba OG Black", "Puma Suede Classic Black", "Converse Chuck Taylor High Black",
                          "Vans Old Skool Black/White"}
    assert all(float(h.product.price) <= 100000 for h in hits)
    # Black T-shirt (black but not a sneaker) and white sneakers are excluded
    assert "Classic Black T-Shirt" not in names and "Nike Air Force 1 White" not in names
    # Without price filter the 120k Air Max appears
    assert "Nike Air Max 90 Black" in [h.product.name for h in svc.search("black sneakers", limit=10)]
    assert svc.search("submarine periscope") == []


def test_search_excludes_inactive(fashion, db):
    p = [p for p in fashion.get("/api/products", params={"q": "Samba"}).json()][0]
    fashion.patch(f"/api/products/{p['id']}", json={"active": False})
    svc = ProductService(db, uuid.UUID(fashion.business_id))
    assert "Adidas Samba OG Black" not in [h.product.name for h in svc.search("black sneakers")]
