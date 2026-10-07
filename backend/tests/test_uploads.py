"""Upload paths after the pypdf 6 / python-multipart 0.0.32 upgrades. Real PDFs are read; broken or hostile ones are
refused with a clear 422 — quickly, never as a 500. Multipart parsing (which runs before authentication) refuses
malformed bodies with a 4xx."""
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.errors import ValidationError
from app.db.session import SessionLocal
from app.main import app
from app.models import KnowledgeDocument
from app.services.knowledge_service import KnowledgeService, extract_pdf_text
from app.services.product_service import ProductService


def pdf(text: str) -> bytes:
    """A minimal valid one-page PDF showing `text` (built by hand: no PDF writer needed)."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


GOOD = pdf("Returns accepted within 7 days with the receipt")
MALFORMED = {
    "not a pdf": b"this is not a pdf at all",
    "header only": b"%PDF-1.7\n",
    "truncated": GOOD[: len(GOOD) // 2],
    # A huge /Size and no /Root: pypdf < 6.6 searched every object number (GHSA-4xc4-762w-m6cg).
    "huge size, no root": b"%PDF-1.4\ntrailer\n<< /Size 99999999 >>\nstartxref\n0\n%%EOF\n",
}


def _docs() -> int:
    with SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(KnowledgeDocument))


def test_text_is_extracted_from_a_real_pdf():
    assert extract_pdf_text(GOOD) == "Returns accepted within 7 days with the receipt"


@pytest.mark.parametrize("name", MALFORMED)
def test_malformed_pdf_is_refused_quickly(name):
    start = time.perf_counter()
    with pytest.raises(ValidationError, match="Could not read PDF"):
        extract_pdf_text(MALFORMED[name])
    assert time.perf_counter() - start < 15


def test_pdf_upload_end_to_end(fashion):
    r = fashion.post("/api/knowledge/upload", files={"file": ("returns.pdf", GOOD, "application/pdf")})
    assert r.status_code == 201 and r.json()["source_type"] == "pdf"
    hits = fashion.get("/api/knowledge/search", params={"q": "returns receipt"}).json()
    assert hits and "7 days" in hits[0]["content"]


def test_malformed_pdf_upload_is_a_clean_422(fashion):
    for name, raw in MALFORMED.items():
        r = fashion.post("/api/knowledge/upload", files={"file": ("broken.pdf", raw, "application/pdf")})
        assert r.status_code == 422, (name, r.status_code, r.text)
        assert r.json()["detail"].startswith("Could not read PDF")
    assert _docs() == 0


def test_multipart_is_refused_without_a_session_and_parsed_safely_when_malformed(client, fashion):
    files = {"file": ("returns.pdf", GOOD, "application/pdf")}
    assert client.post("/api/knowledge/upload", files=files).status_code == 401
    assert client.post("/api/products/import", files={"file": ("p.csv", b"name,price\nX,1\n")}).status_code == 401
    malformed = {
        "missing boundary": ("multipart/form-data", b"--x\r\n\r\n"),
        "boundary over 256 bytes": ("multipart/form-data; boundary=" + "b" * 300,
                                    b"--" + b"b" * 300 + b"\r\n\r\nx\r\n--" + b"b" * 300 + b"--\r\n"),
        "oversized part header": ("multipart/form-data; boundary=xyz",
                                  b"--xyz\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.txt\"\r\n"
                                  b"X-Padding: " + b"p" * 200_000 + b"\r\n\r\nhello\r\n--xyz--\r\n"),
        "no closing boundary": ("multipart/form-data; boundary=xyz",
                                b"--xyz\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.txt\"\r\n\r\nhi"),
    }
    for who in (client, fashion.client):
        headers = {} if who is client else fashion.h
        for name, (ctype, body) in malformed.items():
            start = time.perf_counter()
            r = who.post("/api/knowledge/upload", content=body, headers={**headers, "Content-Type": ctype})
            assert 400 <= r.status_code < 500, (name, r.status_code, r.text)
            assert time.perf_counter() - start < 5, name
    assert _docs() == 0


def test_csv_import_still_parses_multipart(fashion):
    r = fashion.import_csv("name,price,sku\nCanvas tote,15000,TOTE-1\n")
    assert r.status_code == 200 and r.json()["created"] == 1


@pytest.mark.parametrize("kind", ["csv", "pdf"])
def test_health_checks_are_answered_while_a_slow_upload_runs(fashion, monkeypatch, kind):
    """Uploads do their heavy work (parsing, storing, embedding) in a worker thread: the event loop stays free for
    webhooks and health checks however long an import takes."""
    service, method = (ProductService, "import_csv") if kind == "csv" else (KnowledgeService, "add_document")
    real = getattr(service, method)

    def slow(*args, **kwargs):
        time.sleep(2.0)
        return real(*args, **kwargs)

    monkeypatch.setattr(service, method, slow)
    path, files = (("/api/products/import", {"file": ("p.csv", b"name,price,sku\nTote,1500,TOTE-9\n", "text/csv")})
                   if kind == "csv" else ("/api/knowledge/upload", {"file": ("returns.pdf", GOOD, "application/pdf")}))
    with TestClient(app) as shared:  # one event loop serves every request, as in a real server
        done = {}
        upload = threading.Thread(target=lambda: done.update(r=shared.post(path, headers=fashion.h, files=files)))
        upload.start()
        time.sleep(0.5)  # the upload is now inside its slow part
        start = time.perf_counter()
        health = shared.get("/healthz")
        waited = time.perf_counter() - start
        upload.join(15)
    assert health.status_code == 200 and waited < 1.0, f"/healthz waited {waited:.2f}s behind the upload"
    assert done["r"].status_code in (200, 201), done["r"].text
