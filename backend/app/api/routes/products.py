import uuid

from fastapi import APIRouter, Depends, File, Query, UploadFile

from app.api.deps import TenantContext, get_tenant
from app.schemas.api import ProductIn, ProductOut, ProductPatch, StockAdjustIn
from app.services.product_service import MAX_CSV_BYTES, ProductService

router = APIRouter(prefix="/api", tags=["products"])


@router.get("/products", response_model=list[ProductOut])
def list_products(q: str | None = None, active: bool | None = None, limit: int = Query(200, le=1000),
                  offset: int = 0, ctx: TenantContext = Depends(get_tenant)):
    return [ProductOut.of(p) for p in ProductService(ctx.db, ctx.business_id).list(q=q, active=active, limit=limit,
                                                                                     offset=offset)]


@router.get("/products/search")
def search_products(q: str, max_price: float | None = None, limit: int = Query(5, le=20),
                    ctx: TenantContext = Depends(get_tenant)):
    """Same search the agent uses — handy for tuning the catalog."""
    hits = ProductService(ctx.db, ctx.business_id).search(q, max_price=max_price, limit=limit)
    return [{"product": ProductOut.of(h.product), "score": round(h.score, 4), "matched_terms": h.matched_terms}
            for h in hits]


@router.post("/products", response_model=ProductOut, status_code=201)
def create_product(body: ProductIn, ctx: TenantContext = Depends(get_tenant)):
    p = ProductService(ctx.db, ctx.business_id).create(body.model_dump())
    ctx.db.commit()
    return ProductOut.of(p)


@router.post("/products/import")
async def import_products(file: UploadFile = File(...), skip_invalid: bool = False,
                          ctx: TenantContext = Depends(get_tenant)):
    raw = await file.read(MAX_CSV_BYTES + 1)
    result = ProductService(ctx.db, ctx.business_id).import_csv(raw, skip_invalid=skip_invalid)
    if result.imported:
        ctx.db.commit()
    else:
        ctx.db.rollback()
    return {"imported": result.imported, "created": result.created, "updated": result.updated,
            "total_rows": result.total_rows, "errors": result.errors}


@router.get("/products/{product_id}", response_model=ProductOut)
def get_product(product_id: uuid.UUID, ctx: TenantContext = Depends(get_tenant)):
    return ProductOut.of(ProductService(ctx.db, ctx.business_id).get(product_id))


@router.patch("/products/{product_id}", response_model=ProductOut)
def update_product(product_id: uuid.UUID, body: ProductPatch, ctx: TenantContext = Depends(get_tenant)):
    p = ProductService(ctx.db, ctx.business_id).update(product_id, body.model_dump(exclude_unset=True))
    ctx.db.commit()
    return ProductOut.of(p)


@router.delete("/products/{product_id}", status_code=204)
def delete_product(product_id: uuid.UUID, ctx: TenantContext = Depends(get_tenant)):
    ProductService(ctx.db, ctx.business_id).delete(product_id)
    ctx.db.commit()


@router.post("/products/{product_id}/stock", response_model=ProductOut)
def adjust_stock(product_id: uuid.UUID, body: StockAdjustIn, ctx: TenantContext = Depends(get_tenant)):
    p = ProductService(ctx.db, ctx.business_id).adjust_stock(product_id, body.change, reason=body.reason)
    ctx.db.commit()
    return ProductOut.of(p)


@router.get("/products/{product_id}/inventory")
def inventory_history(product_id: uuid.UUID, ctx: TenantContext = Depends(get_tenant)):
    svc = ProductService(ctx.db, ctx.business_id)
    svc.get(product_id)
    return [{"change": m.change, "balance_after": m.balance_after, "reason": m.reason, "reference": m.reference,
             "created_at": m.created_at} for m in svc.inventory_history(product_id)]


@router.get("/categories")
def list_categories(ctx: TenantContext = Depends(get_tenant)):
    return [{"id": str(c.id), "name": c.name} for c in ProductService(ctx.db, ctx.business_id).list_categories()]
