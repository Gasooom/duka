"""Catalog: products, categories, inventory, hybrid search, CSV import."""
from __future__ import annotations

import csv
import io
import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, literal, or_, text
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.models import Business, InventoryMovement, Product, ProductCategory
from app.repositories.repos import CategoryRepo, InventoryRepo, ProductRepo, SettingsRepo
from app.services.embeddings import get_embedder

STOPWORDS = {
    "a", "an", "the", "i", "im", "i'm", "me", "my", "we", "you", "your", "do", "does", "have", "has", "any", "some",
    "for", "of", "to", "in", "on", "with", "and", "or", "is", "are", "looking", "want", "need", "show", "find",
    "please", "under", "below", "less", "than", "over", "above", "cheap", "price", "rwf", "frw", "k", "get", "buy",
    "can", "could", "would", "like", "there", "what", "which", "that", "this", "these", "those", "one", "ones",
    "hi", "hello", "hey", "available", "sell", "selling", "got", "something",
}
_TOKEN = re.compile(r"[a-z0-9]+")
VECTOR_THRESHOLD = 0.30
MAX_CSV_BYTES = 2_000_000
MAX_CSV_ROWS = 5000


def _stem(tok: str) -> str:
    for suf in ("ies", "es", "s"):
        if len(tok) > 4 and tok.endswith(suf):
            return tok[: -len(suf)] + ("y" if suf == "ies" else "")
    return tok


def query_terms(q: str) -> list[str]:
    toks = [t for t in _TOKEN.findall(q.lower()) if t not in STOPWORDS and not t.isdigit()]
    seen, out = set(), []
    for t in toks:
        s = _stem(t)
        if s not in seen and len(s) > 1:
            seen.add(s)
            out.append(s)
    return out[:8]


def product_embedding_text(name: str, category: str | None, description: str | None) -> str:
    return f"{name}. {category or ''}. {description or ''}"


@dataclass
class SearchHit:
    product: Product
    score: float
    matched_terms: int


@dataclass
class CsvImportResult:
    created: int = 0
    updated: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    total_rows: int = 0
    imported: bool = False


class ProductService:
    def __init__(self, db: Session, business_id: uuid.UUID):
        self.db = db
        self.business_id = business_id
        self.products = ProductRepo(db, business_id)
        self.categories = CategoryRepo(db, business_id)
        self.inventory = InventoryRepo(db, business_id)

    @property
    def business(self) -> Business:
        return self.db.get(Business, self.business_id)

    # Categories -----------------------------------------------------------
    def get_or_create_category(self, name: str | None) -> ProductCategory | None:
        if not name or not name.strip():
            return None
        name = name.strip()
        cat = self.categories.first(func.lower(ProductCategory.name) == name.lower())
        return cat or self.categories.add(name=name)

    def list_categories(self) -> list[ProductCategory]:
        return self.categories.list(order_by=[ProductCategory.name])

    # CRUD -------------------------------------------------------------------
    def _embed(self, p: Product) -> None:
        p.embedding = get_embedder().embed_one(
            product_embedding_text(p.name, p.category.name if p.category else None, p.description))

    def create(self, data: dict[str, Any], *, embed: bool = True) -> Product:
        price = Decimal(str(data["price"]))
        if price < 0:
            raise ValidationError("Price cannot be negative")
        stock = int(data.get("stock_quantity") or 0)
        if stock < 0:
            raise ValidationError("Stock cannot be negative")
        sku = (data.get("sku") or "").strip() or f"SKU-{uuid.uuid4().hex[:8].upper()}"
        if self.products.first(Product.sku == sku):
            raise ConflictError(f"SKU '{sku}' already exists")
        category = self.get_or_create_category(data.get("category"))
        p = self.products.add(
            name=data["name"].strip(), description=data.get("description"), price=price,
            currency=(data.get("currency") or self.business.currency).upper(), sku=sku,
            category_id=category.id if category else None, stock_quantity=stock, image_url=data.get("image_url"),
            active=data.get("active", True), attributes=data.get("metadata") or {},
        )
        p.category = category
        if stock:
            self.inventory.add(product_id=p.id, change=stock, balance_after=stock, reason="initial")
        if embed:
            self._embed(p)
        self.db.flush()
        return p

    def update(self, product_id: uuid.UUID, data: dict[str, Any]) -> Product:
        p = self.products.get_or_404(product_id)
        reembed = False
        if "sku" in data and data["sku"] and data["sku"] != p.sku:
            if self.products.first(Product.sku == data["sku"], Product.id != p.id):
                raise ConflictError(f"SKU '{data['sku']}' already exists")
            p.sku = data["sku"]
        for k in ("name", "description", "image_url", "active"):
            if k in data and data[k] is not None:
                setattr(p, k, data[k])
                reembed = reembed or k in ("name", "description")
        if data.get("price") is not None:
            price = Decimal(str(data["price"]))
            if price < 0:
                raise ValidationError("Price cannot be negative")
            p.price = price
        if "metadata" in data and data["metadata"] is not None:
            p.attributes = data["metadata"]
        if "category" in data:
            cat = self.get_or_create_category(data["category"])
            p.category_id = cat.id if cat else None
            p.category = cat
            reembed = True
        if data.get("stock_quantity") is not None and int(data["stock_quantity"]) != p.stock_quantity:
            self.set_stock(p, int(data["stock_quantity"]), reason="adjustment")
        if reembed:
            self._embed(p)
        self.db.flush()
        return p

    def delete(self, product_id: uuid.UUID) -> None:
        self.products.delete(self.products.get_or_404(product_id))

    def get(self, product_id: uuid.UUID | str) -> Product:
        return self.products.get_or_404(product_id)

    def list(self, *, q: str | None = None, active: bool | None = None, limit: int = 200, offset: int = 0) -> list[Product]:
        where = []
        if q:
            where.append(or_(Product.name.ilike(f"%{q}%"), Product.sku.ilike(f"%{q}%")))
        if active is not None:
            where.append(Product.active.is_(active))
        return self.products.list(where=where, order_by=[Product.name], limit=limit, offset=offset)

    def low_stock(self) -> list[Product]:
        threshold = (SettingsRepo(self.db, self.business_id).first() or None)
        t = threshold.low_stock_threshold if threshold else 5
        return self.products.list(where=[Product.active.is_(True), Product.stock_quantity <= t],
                                  order_by=[Product.stock_quantity])

    # Inventory ----------------------------------------------------------------
    def set_stock(self, p: Product, new_qty: int, *, reason: str, reference: str | None = None) -> None:
        if new_qty < 0:
            raise ValidationError("Stock cannot be negative")
        change = new_qty - p.stock_quantity
        p.stock_quantity = new_qty
        self.inventory.add(product_id=p.id, change=change, balance_after=new_qty, reason=reason, reference=reference)

    def adjust_stock(self, product_id: uuid.UUID, change: int, *, reason: str = "adjustment",
                     reference: str | None = None) -> Product:
        p = self.products.get_or_404(product_id, for_update=True)
        self.set_stock(p, p.stock_quantity + change, reason=reason, reference=reference)
        self.db.flush()
        return p

    def inventory_history(self, product_id: uuid.UUID) -> list[InventoryMovement]:
        return self.inventory.list(where=[InventoryMovement.product_id == product_id],
                                   order_by=[InventoryMovement.created_at.desc()], limit=100)

    # Search -------------------------------------------------------------------
    def search(self, query: str, *, max_price: float | None = None, min_price: float | None = None,
               category: str | None = None, in_stock_only: bool = False, limit: int = 5) -> list[SearchHit]:
        """Hybrid search: Postgres full-text (prefix OR query) + pgvector cosine similarity,
        then a deterministic re-rank on how many query terms each product covers."""
        terms = query_terms(query or "")
        where: list[Any] = [Product.active.is_(True)]
        if max_price is not None:
            where.append(Product.price <= Decimal(str(max_price)))
        if min_price is not None:
            where.append(Product.price >= Decimal(str(min_price)))
        if in_stock_only:
            where.append(Product.stock_quantity > 0)
        cat_join = ProductCategory
        if category:
            where.append(func.lower(ProductCategory.name).like(f"%{category.lower()}%"))

        doc = func.to_tsvector(
            text("'simple'::regconfig"),
            func.coalesce(Product.name, "") + " " + func.coalesce(Product.description, ""),
        )
        if terms:
            tsq = func.to_tsquery(text("'simple'::regconfig"), " | ".join(f"{t}:*" for t in terms))
            lex = func.ts_rank(doc, tsq)
            qvec = get_embedder().embed_one(query)
            vscore = 1 - Product.embedding.cosine_distance(qvec)
            cat_match = or_(*[func.lower(func.coalesce(ProductCategory.name, "")).like(f"%{t}%") for t in terms])
            score = (lex * 2 + func.coalesce(vscore, 0)).label("score")
            stmt = (self.products.select(Product, score)
                    .outerjoin(cat_join, Product.category_id == cat_join.id)
                    .where(*where)
                    .where(or_(doc.op("@@")(tsq), vscore > VECTOR_THRESHOLD, cat_match))
                    .order_by(text("score DESC")).limit(40))
        else:
            # No meaningful terms (e.g. "what do you have under 50k?"): filter-only browse.
            stmt = (self.products.select(Product, literal(0.0).label("score"))
                    .outerjoin(cat_join, Product.category_id == cat_join.id)
                    .where(*where).order_by(Product.price).limit(40))
        rows = self.db.execute(stmt).unique().all()

        hits = []
        for product, score in rows:
            hay = " ".join(_stem(t) for t in _TOKEN.findall(
                f"{product.name} {product.description or ''} {product.category.name if product.category else ''} "
                f"{' '.join(str(v) for v in (product.attributes or {}).values())}".lower()))
            matched = sum(1 for t in terms if t in hay)
            hits.append(SearchHit(product=product, score=float(score or 0), matched_terms=matched))
        if terms and hits:
            best = max(h.matched_terms for h in hits)
            if best > 0:
                hits = [h for h in hits if h.matched_terms == best]
        hits.sort(key=lambda h: (-h.matched_terms, -h.score, float(h.product.price)))
        return hits[:limit]

    # CSV import -----------------------------------------------------------------
    def import_csv(self, raw: bytes, *, skip_invalid: bool = False) -> CsvImportResult:
        """Validate every row first. By default the import is all-or-nothing; with
        skip_invalid=True, valid rows are imported and invalid ones reported."""
        result = CsvImportResult()
        if len(raw) > MAX_CSV_BYTES:
            raise ValidationError(f"CSV too large (max {MAX_CSV_BYTES // 1_000_000}MB)")
        try:
            text_data = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValidationError("CSV must be UTF-8 encoded")
        reader = csv.DictReader(io.StringIO(text_data))
        headers = [h.strip().lower() for h in (reader.fieldnames or [])]
        missing = {"name", "price"} - set(headers)
        if missing:
            raise ValidationError(f"CSV is missing required column(s): {', '.join(sorted(missing))}. "
                                  "Expected: name,description,price,category,sku,stock_quantity[,image_url,active]")
        valid_rows: list[dict[str, Any]] = []
        seen_skus: dict[str, int] = {}
        for i, raw_row in enumerate(reader, start=2):  # row 1 = header
            if i - 1 > MAX_CSV_ROWS:
                result.errors.append({"row": i, "field": None, "message": f"Too many rows (max {MAX_CSV_ROWS})"})
                break
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw_row.items()}
            if not any(row.values()):
                continue
            result.total_rows += 1
            errs = []
            if not row.get("name"):
                errs.append(("name", "Name is required"))
            try:
                price = Decimal(row.get("price", "").replace(",", ""))
                if price < 0:
                    errs.append(("price", "Price cannot be negative"))
            except InvalidOperation:
                errs.append(("price", f"Invalid price '{row.get('price')}'"))
                price = None
            stock = 0
            if row.get("stock_quantity"):
                try:
                    stock = int(row["stock_quantity"])
                    if stock < 0:
                        errs.append(("stock_quantity", "Stock cannot be negative"))
                except ValueError:
                    errs.append(("stock_quantity", f"Invalid integer '{row['stock_quantity']}'"))
            sku = row.get("sku") or ""
            if sku:
                if sku in seen_skus:
                    errs.append(("sku", f"Duplicate SKU '{sku}' (also on row {seen_skus[sku]})"))
                seen_skus.setdefault(sku, i)
            active = row.get("active", "true").lower() not in ("false", "0", "no", "inactive")
            for f_, m in errs:
                result.errors.append({"row": i, "field": f_, "message": m})
            if not errs:
                valid_rows.append(dict(name=row["name"], description=row.get("description") or None, price=price,
                                       category=row.get("category") or None, sku=sku or None, stock_quantity=stock,
                                       image_url=row.get("image_url") or None, active=active))
        if result.errors and not skip_invalid:
            return result
        for r in valid_rows:
            existing = self.products.first(Product.sku == r["sku"]) if r["sku"] else None
            if existing:
                self.update(existing.id, {k: v for k, v in r.items() if k != "sku"} | {"category": r["category"]})
                result.updated += 1
            else:
                self.create(r, embed=False)
                result.created += 1
        # Batch-embed newly created products (one embedding call instead of N).
        new_products = self.products.list(where=[Product.embedding.is_(None)])
        if new_products:
            vecs = get_embedder().embed([product_embedding_text(p.name, p.category.name if p.category else None,
                                                                p.description) for p in new_products])
            for p, v in zip(new_products, vecs):
                p.embedding = v
        self.db.flush()
        result.imported = bool(valid_rows)
        return result


def get_product_or_none(db: Session, business_id: uuid.UUID, product_id: str) -> Product | None:
    try:
        return ProductRepo(db, business_id).get(product_id)
    except NotFoundError:
        return None
