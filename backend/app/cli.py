"""Operator commands. Run on the server (requires database access), e.g.

    python -m app.cli create-business --name "Kigali Shoes" --email owner@example.rw

This is the protected onboarding path while public registration is closed: only someone with shell
access to the deployment can create a tenant. If --password is omitted a strong one is generated
and printed once.
"""
import argparse
import secrets
import sys

from app.core.errors import DomainError
from app.db.session import session_scope
from app.services.business_service import register_business


def create_business(args: argparse.Namespace) -> int:
    password = args.password or secrets.token_urlsafe(18)
    try:
        with session_scope() as db:
            business, user, _ = register_business(
                db, business_name=args.name, email=args.email, password=password, full_name=args.full_name,
                business_type=args.business_type, currency=args.currency)
            business_id, email = business.id, user.email
    except DomainError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    print(f"Created business {business_id} with owner {email}")
    if not args.password:
        print(f"Generated password (shown once): {password}")
    return 0


def reset_password(args: argparse.Namespace) -> int:
    from sqlalchemy import func, select

    from app.models import User
    from app.services import audit_service
    from app.services.business_service import set_password
    password = args.password or secrets.token_urlsafe(18)
    with session_scope() as db:
        user = db.scalar(select(User).where(func.lower(User.email) == args.email.strip().lower()))
        if user is None:
            print("error: no user with that email", file=sys.stderr)
            return 1
        try:
            set_password(user, password)
        except DomainError as exc:
            print(f"error: {exc.message}", file=sys.stderr)
            return 1
        audit_service.record(db, user.business_id, "auth.password_reset", "user", user.id, actor_type="system",
                             method="cli", generated=not args.password, other_sessions_signed_out=True)
    print(f"Password reset for {args.email}")
    if not args.password:
        print(f"Generated password (shown once): {password}")
    return 0


def requeue_dead(args: argparse.Namespace) -> int:
    """Retry dead-lettered inbound messages (e.g. after fixing the bug that killed them). Per-customer order is
    kept because events are claimed by sequence; each gets a fresh set of attempts, and a fresh AI budget (its
    Runaway Conversation Guard message counter is cleared: retrying on purpose, after a fix, is the operator's call)."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import delete, update

    from app.models import AiUsageCounter, WebhookEvent
    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    with session_scope() as db:
        stmt = update(WebhookEvent).where(WebhookEvent.status == "dead", WebhookEvent.updated_at >= since)
        if args.id:
            stmt = stmt.where(WebhookEvent.id == args.id)
        ids = list(db.scalars(stmt.values(status="retry", attempts=0, next_attempt_at=datetime.now(timezone.utc),
                                          last_error=None).returning(WebhookEvent.id)))
        if ids:
            db.execute(delete(AiUsageCounter).where(AiUsageCounter.scope == "message",
                                                    AiUsageCounter.subject_id.in_(ids)))
    print(f"requeued {len(ids)} dead event(s) with a fresh AI budget; the workers will process them now")
    return 0


def llm_check(args: argparse.Namespace) -> int:
    """One real call to the configured LLM with a tool schema: proves the key, model and tool calling work.
    Touches no database. Exit 0 = the model called the tool, 1 = it answered without the tool, 2 = failure."""
    import time
    from urllib.parse import urlparse

    from app.agents.providers import LLMError
    from app.agents.providers.openai_compat import OpenAICompatProvider
    from app.core.config import settings
    from app.tools.commerce_tools import search_products  # noqa: F401  (registers tools)
    from app.tools.registry import TOOLS

    print(f"provider=openai_compat host={urlparse(settings.llm_base_url).netloc} model={settings.llm_model} "
          f"key={'set' if settings.llm_api_key else 'MISSING'}")
    try:
        provider = OpenAICompatProvider()
        start = time.perf_counter()
        resp = provider.complete(
            [{"role": "system", "content": "You are a shop assistant. Always use tools for product facts."},
             {"role": "user", "content": args.message}],
            [TOOLS["search_products"].schema()], timeout=settings.agent_turn_timeout_seconds)
    except LLMError as exc:
        print(f"FAILED: {exc}")
        return 2
    ms = (time.perf_counter() - start) * 1000
    print(f"ok latency_ms={ms:.0f} model={resp.model} prompt_tokens={resp.prompt_tokens} "
          f"completion_tokens={resp.completion_tokens}")
    if resp.tool_calls:
        print(f"tool_call: {resp.tool_calls[0].name}({resp.tool_calls[0].arguments})")
        return 0
    print(f"answered without calling the tool: {(resp.content or '')[:200]!r}")
    return 1


def inventory_check(args: argparse.Namespace) -> int:
    """Read-only: every product whose stock differs from the sum of its inventory movements. Every stock change writes
    a movement in the same transaction, so a difference means a change bypassed the ledger. Exit 1 when any differs."""
    from sqlalchemy import func, select

    from app.db.session import SessionLocal
    from app.models import Business, InventoryMovement, Product
    ledger = (select(InventoryMovement.product_id, func.sum(InventoryMovement.change).label("total"))
              .group_by(InventoryMovement.product_id).subquery())
    total = func.coalesce(ledger.c.total, 0)
    with SessionLocal() as db:
        rows = db.execute(select(Business.name, Product.id, Product.sku, Product.name, Product.stock_quantity, total)
                          .join(Business, Business.id == Product.business_id)
                          .outerjoin(ledger, ledger.c.product_id == Product.id)
                          .where(Product.stock_quantity != total).order_by(Business.name, Product.name)).all()
        db.rollback()
    for shop, product_id, sku, name, stock, movements in rows:
        print(f"{shop}: {name} ({sku or product_id}) stock {stock}, inventory movements add up to {movements}")
    print(f"{len(rows)} product(s) whose stock differs from the inventory ledger" if rows
          else "every product's stock matches its inventory ledger")
    return 1 if rows else 0


def usage_report(args: argparse.Namespace) -> int:
    """Monthly usage from the usage ledger (docs/OPERATIONS.md, "Monthly usage reports"). Read-only: one snapshot in a
    READ ONLY transaction; no provider is called and nothing is sent. One shop (--business): the calendar month of
    its own time zone, as the shop's report shows it. All shops: UTC months."""
    import json
    import uuid

    from sqlalchemy import select

    from app.db.session import engine
    from app.models import Business
    from app.services import usage_report as reports
    try:
        if args.business:
            try:
                business_id = uuid.UUID(args.business)
            except ValueError:
                print("error: --business must be a business id", file=sys.stderr)
                return 1
            with reports.snapshot(engine) as db:
                shop = db.execute(select(Business.name, Business.timezone).where(Business.id == business_id)).first()
            if shop is None:
                print("error: no business with that id", file=sys.stderr)
                return 1
            report = {**reports.tenant_month(engine, business_id, shop.timezone, args.month), "name": shop.name}
        else:
            report = reports.operator_month(engine, args.month)
    except DomainError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2) if args.json else reports.format_text(report))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("reset-password", help="Set a new password for a user (generated if omitted)")
    r.add_argument("--email", required=True)
    r.add_argument("--password")
    r.set_defaults(func=reset_password)
    q = sub.add_parser("requeue-dead", help="Retry dead-lettered inbound WhatsApp messages")
    q.add_argument("--hours", type=int, default=24, help="only events that died in the last N hours")
    q.add_argument("--id", help="a single webhook_events id")
    q.set_defaults(func=requeue_dead)
    i = sub.add_parser("inventory-check", help="List products whose stock differs from the inventory ledger")
    i.set_defaults(func=inventory_check)
    u = sub.add_parser("usage-report", help="Monthly usage and estimated costs from the usage ledger (read-only)")
    u.add_argument("--month", help="YYYY-MM (default: this month)")
    u.add_argument("--business", help="one shop's id: its own time zone; omitted: every shop, in UTC")
    u.add_argument("--json", action="store_true", help="the full report as JSON")
    u.set_defaults(func=usage_report)
    c = sub.add_parser("llm-check", help="Make one real call to the configured LLM (needs LLM_API_KEY)")
    c.add_argument("--message", default="Muraho! Ndashaka inkweto z'umukara ziri munsi ya 100,000 RWF.")
    c.set_defaults(func=llm_check)
    p = sub.add_parser("create-business", help="Onboard a business and its owner account")
    p.add_argument("--name", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--password", help="At least 8 characters. Omit to generate one.")
    p.add_argument("--full-name")
    p.add_argument("--business-type", default="retail")
    p.add_argument("--currency", default="RWF")
    p.set_defaults(func=create_business)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
