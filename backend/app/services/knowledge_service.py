"""Business knowledge (FAQ, policies) -> chunks -> embeddings in pgvector.

RAG is for unstructured knowledge only. Products, stock, prices, orders and payments are
always answered by transactional tools, never by retrieval."""
from __future__ import annotations

import io
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import func, literal, or_, text
from sqlalchemy.orm import Session

from app.core.errors import ValidationError
from app.models import KnowledgeChunk, KnowledgeDocument
from app.repositories.repos import KnowledgeChunkRepo, KnowledgeDocumentRepo
from app.services.embeddings import get_embedder, query_vector
from app.services.product_service import field_tokens, query_terms, term_matches

MAX_DOC_CHARS = 200_000
CHUNK_CHARS = 700
CHUNK_OVERLAP = 120


def chunk_text(content: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Paragraph-aware chunking: pack paragraphs up to `size`, split long ones with overlap."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", content) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if len(p) > size:
            if buf:
                chunks.append(buf)
                buf = ""
            start = 0
            while start < len(p):
                chunks.append(p[start:start + size])
                start += size - overlap
            continue
        if len(buf) + len(p) + 2 <= size:
            buf = f"{buf}\n\n{p}" if buf else p
        else:
            chunks.append(buf)
            buf = p
    if buf:
        chunks.append(buf)
    return chunks


def extract_pdf_text(raw: bytes) -> str:
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(raw))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:  # malformed PDFs
        raise ValidationError(f"Could not read PDF: {exc}")


@dataclass
class KnowledgeHit:
    document_title: str
    content: str
    score: float


class KnowledgeService:
    def __init__(self, db: Session, business_id: uuid.UUID):
        self.db = db
        self.business_id = business_id
        self.docs = KnowledgeDocumentRepo(db, business_id)
        self.chunks = KnowledgeChunkRepo(db, business_id)

    def add_document(self, title: str, content: str, source_type: str = "text") -> KnowledgeDocument:
        content = (content or "").strip()
        if not title.strip():
            raise ValidationError("Title is required")
        if not content:
            raise ValidationError("Document has no text content")
        if len(content) > MAX_DOC_CHARS:
            raise ValidationError(f"Document too long (max {MAX_DOC_CHARS} characters)")
        doc = self.docs.add(title=title.strip(), content=content, source_type=source_type)
        parts = chunk_text(content)
        vectors = get_embedder().embed([f"{doc.title}\n{p}" for p in parts])
        for i, (part, vec) in enumerate(zip(parts, vectors)):
            self.chunks.add(document_id=doc.id, chunk_index=i, content=part, embedding=vec)
        doc.chunk_count = len(parts)
        self.db.flush()
        return doc

    def list(self) -> list[KnowledgeDocument]:
        return self.docs.list(order_by=[KnowledgeDocument.created_at.desc()])

    def delete(self, doc_id: uuid.UUID) -> None:
        self.docs.delete(self.docs.get_or_404(doc_id))

    def search(self, query: str, limit: int = 3) -> list[KnowledgeHit]:
        """A chunk is returned only when it shares a meaningful word with the question (the product-search rule).
        Vector similarity ranks but never admits: with the default hash embedding it is collision-prone, and an
        unrelated policy is worse than none — the model would apply it to the customer's question."""
        terms = query_terms(query)
        if not terms:
            return []
        vec = query_vector(query)  # None: embeddings unavailable, rank by words alone
        vscore = 1 - KnowledgeChunk.embedding.cosine_distance(vec) if vec is not None else literal(0.0)
        doc_tsv = func.to_tsvector(text("'simple'::regconfig"), KnowledgeChunk.content)
        tsq = func.to_tsquery(text("'simple'::regconfig"), " | ".join(f"{t}:*" for t in terms))
        score = (func.coalesce(vscore, 0) + func.ts_rank(doc_tsv, tsq)).label("score")
        in_title = or_(*[func.lower(KnowledgeDocument.title).like(f"%{t}%") for t in terms])
        stmt = (self.chunks.select(KnowledgeChunk, KnowledgeDocument.title, score)
                .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
                .where(KnowledgeDocument.business_id == self.business_id)
                .where(or_(doc_tsv.op("@@")(tsq), in_title))
                .order_by(text("score DESC")).limit(limit * 10))
        ranked = []
        for chunk, title, s in self.db.execute(stmt).all():
            words = field_tokens(f"{title} {chunk.content}")
            matched = sum(1 for t in terms if term_matches(t, words))
            if matched:
                ranked.append((-matched, -float(s or 0),
                               KnowledgeHit(document_title=title, content=chunk.content, score=round(float(s or 0), 3))))
        ranked.sort(key=lambda r: r[:2])
        return [hit for _, _, hit in ranked[:limit]]

