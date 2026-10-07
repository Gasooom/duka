from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings


def connect_args() -> dict:
    """Server-side limits on every app connection, so a stuck query, a lock queue or a transaction left open can
    neither hang a worker nor pin a connection forever. Migrations (alembic/env.py) use their own engine."""
    limits = {"statement_timeout": settings.db_statement_timeout_ms, "lock_timeout": settings.db_lock_timeout_ms,
              "idle_in_transaction_session_timeout": settings.db_idle_in_transaction_timeout_ms}
    return {"options": " ".join(f"-c {name}={int(ms)}" for name, ms in limits.items())}


engine = create_engine(settings.database_url, pool_pre_ping=True, pool_size=10, max_overflow=10, future=True,
                       connect_args=connect_args())
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for background jobs: commit on success, rollback on error."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
