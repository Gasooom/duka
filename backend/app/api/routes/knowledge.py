import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from app.api.deps import TenantContext, get_tenant
from app.schemas.api import KnowledgeIn, KnowledgeOut
from app.services.knowledge_service import KnowledgeService, extract_pdf_text

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])
MAX_UPLOAD = 5_000_000


@router.get("", response_model=list[KnowledgeOut])
def list_docs(ctx: TenantContext = Depends(get_tenant)):
    return KnowledgeService(ctx.db, ctx.business_id).list()


@router.post("", response_model=KnowledgeOut, status_code=201)
def add_text(body: KnowledgeIn, ctx: TenantContext = Depends(get_tenant)):
    doc = KnowledgeService(ctx.db, ctx.business_id).add_document(body.title, body.content, "text")
    ctx.db.commit()
    return doc


@router.post("/upload", response_model=KnowledgeOut, status_code=201)
async def upload(file: UploadFile = File(...), title: str | None = Form(None), ctx: TenantContext = Depends(get_tenant)):
    raw = await file.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, "File too large (max 5MB)")
    name = (file.filename or "document").lower()
    if name.endswith(".pdf"):
        # CPU-bound and attacker-shaped (a hostile PDF can take seconds to refuse): never on the event loop.
        content, kind = await run_in_threadpool(extract_pdf_text, raw), "pdf"
    elif name.endswith((".txt", ".md")):
        content, kind = raw.decode("utf-8", errors="replace"), "file"
    else:
        raise HTTPException(415, "Only .pdf, .txt and .md files are supported")
    doc = KnowledgeService(ctx.db, ctx.business_id).add_document(title or file.filename or "Document", content, kind)
    ctx.db.commit()
    return doc


@router.get("/search")
def search(q: str, ctx: TenantContext = Depends(get_tenant)):
    return [h.__dict__ for h in KnowledgeService(ctx.db, ctx.business_id).search(q, limit=5)]


@router.delete("/{doc_id}", status_code=204)
def delete(doc_id: uuid.UUID, ctx: TenantContext = Depends(get_tenant)):
    KnowledgeService(ctx.db, ctx.business_id).delete(doc_id)
    ctx.db.commit()
