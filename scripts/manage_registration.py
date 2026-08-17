#!/usr/bin/env python3
"""
Operator CLI for managing registration keys and listing orgs.

Usage:
  python scripts/manage_registration.py              # list orgs + all keys
  python scripts/manage_registration.py new          # generate a new registration key
  python scripts/manage_registration.py new --label "Alpha Tester 1"
  python scripts/manage_registration.py new --label "Acme Corp" --days 30
  python scripts/manage_registration.py revoke <key_id>
"""

import argparse
import asyncio
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow running from repo root or from scripts/
sys.path.insert(0, str(Path(__file__).parent.parent))

import os

import yaml
import asyncpg


def load_db_url() -> str:
    if url := os.environ.get("DATABASE_URL"):
        return url
    cfg_path = Path(__file__).parent.parent / "config.yaml"
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)
    return cfg.get("db_url", "")


def fmt_dt(dt) -> str:
    if dt is None:
        return "—"
    if hasattr(dt, "strftime"):
        return dt.strftime("%Y-%m-%d %H:%M")
    return str(dt)


async def cmd_list(conn):
    # ── Orgs ──────────────────────────────────────────────────────────────
    orgs = await conn.fetch(
        """
        SELECT o.id, o.name, o.slug, o.created_at, COUNT(u.id) AS user_count
        FROM orgs o LEFT JOIN users u ON u.org_id = o.id
        GROUP BY o.id ORDER BY o.created_at
        """
    )
    print("\n── Organisations ─────────────────────────────────────────────────")
    if not orgs:
        print("  (none)")
    else:
        print(f"  {'ID':<4}  {'Name':<25}  {'Slug':<20}  {'Users':<6}  Created")
        print(f"  {'─'*4}  {'─'*25}  {'─'*20}  {'─'*6}  {'─'*16}")
        for o in orgs:
            print(f"  {o['id']:<4}  {o['name'][:25]:<25}  {o['slug'][:20]:<20}  {o['user_count']:<6}  {fmt_dt(o['created_at'])}")

    # ── Registration keys ──────────────────────────────────────────────────
    keys = await conn.fetch(
        """
        SELECT rk.*, o.name AS org_name
        FROM registration_keys rk
        LEFT JOIN orgs o ON o.id = rk.used_by_org
        ORDER BY rk.created_at DESC
        """
    )
    print("\n── Registration Keys ─────────────────────────────────────────────")
    if not keys:
        print("  (none — run 'new' to generate one)")
    else:
        now = datetime.now(timezone.utc)
        print(f"  {'ID':<4}  {'Key (preview)':<14}  {'Label':<22}  {'Status':<10}  {'Expires':<16}  Used by org")
        print(f"  {'─'*4}  {'─'*14}  {'─'*22}  {'─'*10}  {'─'*16}  {'─'*20}")
        for k in keys:
            preview = (k["reg_key"] or "")[:10] + "…"
            if k["used_at"]:
                status = "USED"
            elif k["expires_at"] and k["expires_at"].replace(tzinfo=timezone.utc) <= now:
                status = "EXPIRED"
            else:
                status = "pending"
            org_name = k["org_name"] or "—"
            expires  = fmt_dt(k["expires_at"]) if k["expires_at"] else "never"
            label    = (k["label"] or "")[:22]
            print(f"  {k['id']:<4}  {preview:<14}  {label:<22}  {status:<10}  {expires:<16}  {org_name}")
    print()


async def cmd_new(conn, label: str | None, days: int | None):
    key = secrets.token_urlsafe(32)
    expires_at = None
    if days:
        expires_at = datetime.now(timezone.utc) + timedelta(days=days)

    row = await conn.fetchrow(
        "INSERT INTO registration_keys (reg_key, label, expires_at) VALUES ($1, $2, $3) RETURNING *",
        key, label, expires_at,
    )
    print("\n── New Registration Key ──────────────────────────────────────────")
    print(f"  ID      : {row['id']}")
    print(f"  Key     : {key}")
    print(f"  Label   : {label or '—'}")
    print(f"  Expires : {fmt_dt(expires_at) if expires_at else 'never'}")
    print()
    print("  Share this key with the org admin. They enter it in the")
    print("  'Registration key' field on the Register tab.")
    print()


async def cmd_revoke(conn, key_id: int):
    row = await conn.fetchrow(
        "DELETE FROM registration_keys WHERE id = $1 AND used_at IS NULL RETURNING id, label",
        key_id,
    )
    if row:
        print(f"\n  Revoked key {key_id} (label: {row['label'] or '—'})\n")
    else:
        print(f"\n  Key {key_id} not found or already used — cannot revoke.\n")


async def main():
    parser = argparse.ArgumentParser(
        description="Manage Buzzowl registration keys",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd")

    new_p = sub.add_parser("new", help="Generate a new registration key")
    new_p.add_argument("--label", help="Human label for this key (e.g. 'Alpha Tester 1')")
    new_p.add_argument("--days", type=int, help="Expiry in days (default: never)")

    rev_p = sub.add_parser("revoke", help="Revoke an unused key by ID")
    rev_p.add_argument("key_id", type=int, help="ID from the list output")

    args = parser.parse_args()

    db_url = load_db_url()
    if not db_url:
        print("ERROR: db_url not set in config.yaml")
        sys.exit(1)

    conn = await asyncpg.connect(db_url)
    try:
        if args.cmd == "new":
            await cmd_new(conn, label=args.label, days=args.days)
        elif args.cmd == "revoke":
            await cmd_revoke(conn, key_id=args.key_id)
        else:
            await cmd_list(conn)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
