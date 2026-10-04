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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
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
