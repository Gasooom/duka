"""database-level tenant integrity

Defence in depth under the repository layer:
  1. a row may only reference rows of the *same* business (e.g. a cart item in business A can
     never point at a product of business B), enforced by a trigger per tenant->tenant foreign key;
  2. business_id is immutable once written.

A trigger is used instead of composite foreign keys because several references are
ON DELETE SET NULL, and SQLAlchemy cannot express PostgreSQL's `SET NULL (column)` form.
`tests/test_tenant_isolation.py` asserts every tenant->tenant FK in the models is covered here.

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# child table -> [(fk column, parent table)]
TENANT_REFERENCES: dict[str, list[tuple[str, str]]] = {
    "products": [("category_id", "product_categories")],
    "inventory": [("product_id", "products")],
    "carts": [("customer_id", "customers"), ("conversation_id", "conversations"),
              ("delivery_zone_id", "delivery_zones")],
    "cart_items": [("cart_id", "carts"), ("product_id", "products")],
    "orders": [("customer_id", "customers"), ("conversation_id", "conversations")],
    "order_items": [("order_id", "orders"), ("product_id", "products")],
    "payments": [("order_id", "orders")],
    "conversations": [("customer_id", "customers")],
    "messages": [("conversation_id", "conversations")],
    "agent_runs": [("conversation_id", "conversations"), ("customer_id", "customers")],
    "knowledge_chunks": [("document_id", "knowledge_documents")],
}

TENANT_TABLES = [
    "users", "business_settings", "agent_configs", "whatsapp_accounts", "delivery_zones", "product_categories",
    "products", "inventory", "customers", "conversations", "messages", "agent_runs", "carts", "cart_items",
    "orders", "order_items", "payments", "knowledge_documents", "knowledge_chunks",
]


def upgrade() -> None:
    # Triggers only check new writes, so refuse to install them over data that already violates the rule.
    bind = op.get_bind()
    for table, refs in TENANT_REFERENCES.items():
        for col, parent in refs:
            bad = bind.execute(sa.text(f"SELECT count(*) FROM {table} c JOIN {parent} p ON p.id = c.{col} "
                                       "WHERE p.business_id <> c.business_id")).scalar()
            if bad:
                raise RuntimeError(f"{bad} existing cross-tenant rows in {table}.{col} -> {parent}; fix before migrating")
    op.execute("""
    CREATE OR REPLACE FUNCTION duka_enforce_same_tenant() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE
        ref_id uuid;
        parent_bid uuid;
        i int;
    BEGIN
        -- TG_ARGV = [column, parent_table, column, parent_table, ...]
        FOR i IN 0 .. (TG_NARGS / 2) - 1 LOOP
            ref_id := (to_jsonb(NEW) ->> TG_ARGV[2 * i])::uuid;
            IF ref_id IS NOT NULL THEN
                EXECUTE format('SELECT business_id FROM %I WHERE id = $1', TG_ARGV[2 * i + 1])
                    INTO parent_bid USING ref_id;
                IF parent_bid IS DISTINCT FROM NEW.business_id THEN
                    RAISE EXCEPTION 'cross-tenant reference: %.% -> %', TG_TABLE_NAME, TG_ARGV[2 * i],
                        TG_ARGV[2 * i + 1] USING ERRCODE = 'foreign_key_violation';
                END IF;
            END IF;
        END LOOP;
        RETURN NEW;
    END $$;
    """)
    op.execute("""
    CREATE OR REPLACE FUNCTION duka_business_id_immutable() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.business_id IS DISTINCT FROM OLD.business_id THEN
            RAISE EXCEPTION '%.business_id is immutable', TG_TABLE_NAME USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END $$;
    """)
    for table, refs in TENANT_REFERENCES.items():
        cols = ", ".join(c for c, _ in refs)
        args = ", ".join(f"'{c}', '{p}'" for c, p in refs)
        op.execute(f"CREATE TRIGGER tenant_fk_{table} BEFORE INSERT OR UPDATE OF business_id, {cols} ON {table} "
                   f"FOR EACH ROW EXECUTE FUNCTION duka_enforce_same_tenant({args})")
    for table in TENANT_TABLES:
        op.execute(f"CREATE TRIGGER tenant_immutable_{table} BEFORE UPDATE OF business_id ON {table} "
                   f"FOR EACH ROW EXECUTE FUNCTION duka_business_id_immutable()")


def downgrade() -> None:
    for table in TENANT_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS tenant_immutable_{table} ON {table}")
    for table in TENANT_REFERENCES:
        op.execute(f"DROP TRIGGER IF EXISTS tenant_fk_{table} ON {table}")
    op.execute("DROP FUNCTION IF EXISTS duka_business_id_immutable()")
    op.execute("DROP FUNCTION IF EXISTS duka_enforce_same_tenant()")
