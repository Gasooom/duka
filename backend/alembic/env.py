from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

import app.models  # noqa: F401  (register models)
from alembic import context
from app.core.config import settings
from app.db.base import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)
# Only configure logging when alembic owns the process (CLI). When migrations run inside the app/tests, reusing
# alembic.ini's config would reset the root level to WARN and disable every existing logger (all app logs gone).
import logging  # noqa: E402

if config.config_file_name is not None and not logging.getLogger().handlers:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# Indexes created with raw SQL in migrations (HNSW vector, FTS, composite). Autogenerate must never drop them.
MANUAL_INDEXES = {"ix_products_embedding_hnsw", "ix_knowledge_chunks_embedding_hnsw", "ix_products_fts",
                  "ix_messages_conversation_created", "ix_orders_business_status"}


def include_object(obj, name, type_, reflected, compare_to):
    return not (type_ == "index" and name in MANUAL_INDEXES)


def run_migrations_offline() -> None:
    context.configure(url=settings.database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True,
                          include_object=include_object)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
