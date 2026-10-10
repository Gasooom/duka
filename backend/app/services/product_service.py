"""Catalog: products, categories, inventory, hybrid search, CSV import."""
from __future__ import annotations

import csv
import io
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Text, cast, func, literal, or_, text
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.models import Business, InventoryMovement, Product, ProductCategory, User
from app.repositories.repos import CategoryRepo, InventoryRepo, ProductRepo, SettingsRepo
from app.services import audit_service
from app.services.embeddings import embed_texts, query_vector

STOPWORDS = {
    "a", "an", "the", "i", "im", "i'm", "me", "my", "we", "you", "your", "do", "does", "have", "has", "any", "some",
    "for", "of", "to", "in", "on", "with", "and", "or", "is", "are", "looking", "want", "need", "show", "find",
    "please", "under", "below", "less", "than", "over", "above", "cheap", "price", "rwf", "frw", "k", "get", "buy",
    "can", "could", "would", "like", "there", "what", "which", "that", "this", "these", "those", "one", "ones",
    "hi", "hello", "hey", "available", "sell", "selling", "got", "something",
    # Words that never name a product: browsing, stock, size and budget talk, chat filler.
    "product", "products", "item", "items", "thing", "things", "stuff", "anything", "everything", "all", "option",
    "options", "catalog", "catalogue", "menu", "list", "kind", "kinds", "type", "types", "stock", "instock", "size",
    "sizes", "color", "colour", "colors", "colours", "cost", "costs", "much", "many", "how", "about", "tell", "know",
    "see", "pay", "it", "its", "at", "by", "from", "be", "will", "just", "also", "only", "other", "else", "still",
    "more", "yes", "no", "ok", "okay", "thanks", "thank", "pls", "plz",
}
# Words that describe a product rather than name one (stemmed). A product matching only these is not a match —
# "black" in "black dress" must not return black tea — but a query made only of them ("something black") searches
# by them.
MODIFIERS = {
    "black", "white", "red", "blue", "green", "yellow", "orange", "purple", "pink", "brown", "grey", "gray",
    "beige", "navy", "gold", "golden", "silver", "maroon", "cream", "khaki", "dark", "light", "small", "medium",
    "large", "big", "mini", "xs", "xl", "xxl", "long", "short", "slim", "tall", "wide", "cotton", "leather", "plastic",
    "wood", "wooden", "metal", "steel", "silk", "wool", "denim", "canvas", "new", "original", "genuine", "best", "good",
    "nice", "premium", "quality", "classic", "organic", "fresh", "pure", "local", "simple", "plain", "smart", "kid",
    "men", "women", "lady", "unisex", "affordable", "expensive", "cheapest",
}
_TOKEN = re.compile(r"[^\W_]+")  # words in any script: an Arabic word is a search word, never "no words"
_AMOUNT = re.compile(r"\d+k")  # "100k": a budget, not a product word
_ACCENTED, _PLAIN = "áàâäãåéèêëíìîïóòôöõúùûüçñ", "aaaaaaeeeeiiiiooooouuuucn"


def fold(text: str | None) -> str:
    """Lower-case without accents or diacritics: "Café" and "cafe" are the same word."""
    t = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))
CANDIDATE_LIMIT = 200
MAX_CSV_BYTES = 2_000_000
MAX_CSV_ROWS = 5000


def stem(tok: str) -> str:
    """Light plural stemming whose result is a prefix of the singular: shoes->shoe, dresses->dress, bags->bag,
    batteries->battery (never shoes->"sho", which would prefix-match "shorts" and "shop")."""
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 4 and tok.endswith(("sses", "shes", "ches", "xes", "zes")):
        return tok[:-2]
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith(("ss", "us", "is")):
        return tok[:-1]
    return tok


def query_words(q: str) -> list[tuple[str, str]]:
    """(stem, word as typed) for each meaningful query word, in order, without duplicates. Both forms are matched:
    the stem finds plurals ("bags" -> "bag"), the typed word keeps prefixes intact ("sams" must not become "sam")."""
    seen, out = set(), []
    for tok in _TOKEN.findall(fold(q)):
        if tok in STOPWORDS or tok.isdigit() or _AMOUNT.fullmatch(tok):
            continue
        s = stem(tok)
        if s not in seen and len(s) > 1 and s not in STOPWORDS:
            seen.add(s)
            out.append((s, tok))
    return out[:8]


def query_terms(q: str) -> list[str]:
    return [s for s, _ in query_words(q)]


def field_tokens(text: str | None) -> set[str]:
    """Stemmed words of a product field, plus joined short compounds ("T-Shirt" -> "tshirt", "USB-C" -> "usbc")."""
    raw = _TOKEN.findall(fold(text))
    toks = {stem(t) for t in raw}
    toks |= {stem(a + b) for a, b in zip(raw, raw[1:]) if min(len(a), len(b)) <= 2}
    return toks


def term_matches(term: str, tokens: set[str]) -> bool:
    """A query word matches a field when it is one of its words or (4+ letters) the start of one: "sams" finds
    "samsung", "phone" finds "phones" — but "phone" never finds "smartphone" or "headphones", and "tea" never "teal"."""
    return term in tokens or (len(term) >= 4 and any(tok.startswith(term) for tok in tokens))


def product_embedding_text(name: str, category: str | None, description: str | None) -> str:
    return f"{name}. {category or ''}. {description or ''}"


@dataclass
class SearchHit:
    product: Product
    score: float
    matched_terms: int
    strong_terms: int = 0  # query words found in the name, category, SKU or attributes (not only the description)
    missing: list[str] = field(default_factory=list)  # query words this product does not match


@dataclass
class CsvImportResult:
    created: int = 0
    updated: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    total_rows: int = 0
    imported: bool = False


class ProductService:
    def __init__(self, db: Session, business_id: uuid.UUID, actor: User | None = None):
        self.db = db
        self.business_id = business_id
        self.actor = actor  # the signed-in user making changes (audit trail of price changes)
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
        text_ = product_embedding_text(p.name, p.category.name if p.category else None, p.description)
        p.embedding = embed_texts(self.db.get_bind(), self.business_id, [text_], source_type="product",
                                  source_id=p.id)[0]

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

    def update(self, product_id: uuid.UUID, data: dict[str, Any], *, source: str = "api") -> Product:
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
            if price != p.price:  # what customers are charged: every change is audited
                audit_service.record(self.db, self.business_id, "product.price_changed", "product", p.id,
                                     user=self.actor, sku=p.sku, currency=p.currency, source=source,
                                     **{"from": str(p.price), "to": str(price)})
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
        """Catalog search. A product is returned only with lexical evidence for what the customer asked for.

        1. Hard filters, always in SQL: tenant, active, price range, category, stock.
        2. No meaningful words (e.g. "what do you have under 50k?"): filter-only browse, cheapest first.
        3. Candidates: full-text prefix match on name/description, or the word inside the name, category, SKU or
           attributes. Vector similarity is a ranking signal only, never a reason to return a product: the
           default hash embedding is lexical feature hashing whose collisions score unrelated products higher than
           real (misspelt) matches ("phone" vs "Rwandan Tea 250g": 0.46).
        4. Evidence per query word: a word (or a 4+ letter prefix of one) of the name, category, SKU or attributes
           (what the product IS) or of the description (detail only). A product is returned only when what it is
           matches a product word — not just a colour/size/material ("black" does not make black tea a "black
           dress") and not a description that mentions something else ("charger for phones" is not a phone).
        5. Keep the products matching the most query words, so partial matches only appear when nothing matches
           every word; each hit reports the words it is missing.
        6. Order: name/category evidence, text/vector score, price, name. Nothing is ever added to fill an empty
           result.
        """
        words = query_words(query or "")
        terms = [s for s, _ in words]
        forms = {s: {s, w} for s, w in words}
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

        if not terms:
            stmt = (self.products.select(Product, literal(0.0).label("score"))
                    .outerjoin(cat_join, Product.category_id == cat_join.id)
                    .where(*where).order_by(Product.price, Product.name, Product.id).limit(limit))
            return [SearchHit(product=p, score=0.0, matched_terms=0) for p, _ in self.db.execute(stmt).unique().all()]

        doc = func.to_tsvector(
            text("'simple'::regconfig"),
            func.coalesce(Product.name, "") + " " + func.coalesce(Product.description, ""),
        )
        all_forms = sorted({f for fs in forms.values() for f in fs})
        tsq = func.to_tsquery(text("'simple'::regconfig"), " | ".join(f"{f}:*" for f in all_forms))
        vec = query_vector(query, bind=self.db.get_bind(), business_id=self.business_id,
                           source_type="product_search")  # None: embeddings unavailable, rank by words alone
        vscore = 1 - Product.embedding.cosine_distance(vec) if vec is not None else literal(0.0)
        score = (func.ts_rank(doc, tsq) * 2 + func.coalesce(vscore, 0)).label("score")

        def contains_term(col: Any) -> Any:  # "T-Shirt" -> "tshirt", so compounds and SKUs are candidates too
            folded = func.translate(func.lower(func.coalesce(col, "")), _ACCENTED, _PLAIN)
            compact = func.regexp_replace(folded, "[^[:alnum:]]+", "", "g")
            return or_(*[compact.like(f"%{f}%") for f in all_forms])

        lexical = or_(doc.op("@@")(tsq), contains_term(Product.name), contains_term(ProductCategory.name),
                      contains_term(Product.sku), contains_term(cast(Product.attributes, Text)))
        stmt = (self.products.select(Product, score)
                .outerjoin(cat_join, Product.category_id == cat_join.id)
                .where(*where).where(lexical)
                .order_by(text("score DESC")).limit(CANDIDATE_LIMIT))

        anchors = [t for t in terms if t not in MODIFIERS] or terms
        hits: list[SearchHit] = []
        for product, s in self.db.execute(stmt).unique().all():
            strong = field_tokens(" ".join([product.name, product.category.name if product.category else "",
                                            product.sku or "",
                                            *(str(v) for v in (product.attributes or {}).values())]))
            weak = field_tokens(product.description)
            level = {t: 2 if any(term_matches(f, strong) for f in forms[t])
                     else 1 if any(term_matches(f, weak) for f in forms[t]) else 0 for t in terms}
            if not any(level[t] == 2 for t in anchors):
                continue  # what this product is does not match anything the customer is shopping for
            hits.append(SearchHit(product=product, score=float(s or 0),
                                  matched_terms=sum(1 for v in level.values() if v),
                                  strong_terms=sum(1 for v in level.values() if v == 2),
                                  missing=[w for s_, w in words if not level[s_]]))
        if hits:
            best = max(h.matched_terms for h in hits)
            hits = [h for h in hits if h.matched_terms == best]
        hits.sort(key=lambda h: (-h.strong_terms, -h.score, float(h.product.price), h.product.name.lower(),
                                 str(h.product.id)))
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
                self.update(existing.id, {k: v for k, v in r.items() if k != "sku"} | {"category": r["category"]},
                            source="csv_import")
                result.updated += 1
            else:
                self.create(r, embed=False)
                result.created += 1
        # Batch-embed newly created products (one embedding call instead of N).
        new_products = self.products.list(where=[Product.embedding.is_(None)])
        if new_products:
            vecs = embed_texts(self.db.get_bind(), self.business_id,
                               [product_embedding_text(p.name, p.category.name if p.category else None, p.description)
                                for p in new_products], source_type="product_import")
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
