"""Tenant-scoped data access.

Every repository is constructed with a business_id and *all* reads/writes go through
`self.query()` / `self.get()` which add `business_id = :bid` automatically. Services never
build raw unscoped queries against tenant tables, so forgetting a filter is not possible
through this layer.
"""
from __future__ import annotations

import uuid
from typing import Any, Generic, Sequence, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.db.base import Base

ModelT = TypeVar("ModelT", bound=Base)


class TenantRepository(Generic[ModelT]):
    model: type[ModelT]

    def __init__(self, db: Session, business_id: uuid.UUID):
        if business_id is None:
            raise ValueError("TenantRepository requires a business_id")
        if not hasattr(self.model, "business_id"):
            raise TypeError(f"{self.model.__name__} is not a tenant-owned model")
        self.db = db
        self.business_id = business_id

    # -- query helpers -------------------------------------------------
    def select(self, *entities: Any) -> Select:
        stmt = select(*(entities or (self.model,)))
        return stmt.where(self.model.business_id == self.business_id)

    def query(self) -> Select:
        return self.select()

    def get(self, id_: uuid.UUID | str, *, for_update: bool = False) -> ModelT | None:
        try:
            pk = id_ if isinstance(id_, uuid.UUID) else uuid.UUID(str(id_))
        except ValueError:
            return None
        stmt = self.query().where(self.model.id == pk)
        if for_update:
            stmt = stmt.with_for_update(of=self.model)
        return self.db.scalars(stmt).unique().first()

    def get_or_404(self, id_: uuid.UUID | str, *, for_update: bool = False) -> ModelT:
        obj = self.get(id_, for_update=for_update)
        if obj is None:
            raise NotFoundError(f"{self.model.__name__} not found")
        return obj

    def list(self, *, where: Sequence[Any] = (), order_by: Sequence[Any] = (), limit: int | None = None,
             offset: int = 0) -> list[ModelT]:
        stmt = self.query().where(*where).order_by(*order_by).offset(offset)
        if limit:
            stmt = stmt.limit(limit)
        return list(self.db.scalars(stmt).unique().all())

    def count(self, *where: Any) -> int:
        stmt = select(func.count()).select_from(self.model).where(self.model.business_id == self.business_id, *where)
        return int(self.db.scalar(stmt) or 0)

    def first(self, *where: Any) -> ModelT | None:
        return self.db.scalars(self.query().where(*where).limit(1)).unique().first()

    # -- writes ----------------------------------------------------------
    def add(self, **fields: Any) -> ModelT:
        fields.pop("business_id", None)  # never trust caller-provided tenant
        obj = self.model(business_id=self.business_id, **fields)
        self.db.add(obj)
        self.db.flush()
        return obj

    def update(self, obj: ModelT, **fields: Any) -> ModelT:
        self._assert_owned(obj)
        fields.pop("business_id", None)
        for k, v in fields.items():
            setattr(obj, k, v)
        self.db.flush()
        return obj

    def delete(self, obj: ModelT) -> None:
        self._assert_owned(obj)
        self.db.delete(obj)
        self.db.flush()

    def _assert_owned(self, obj: ModelT) -> None:
        if getattr(obj, "business_id", None) != self.business_id:
            raise NotFoundError(f"{self.model.__name__} not found")
