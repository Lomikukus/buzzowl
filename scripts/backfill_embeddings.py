#!/usr/bin/env python3
"""
Backfill missing (or all) embeddings for documents, clients, and contacts.

Usage:
    python scripts/backfill_embeddings.py            # only rows with embedding IS NULL
    python scripts/backfill_embeddings.py --all      # re-embed everything (REQUIRED after
                                                     # switching embed_backend or embed_model —
                                                     # vectors from different models don't mix)

Reads config.yaml / .env for db_url and the embedding backend, same as the server.
Safe to re-run; processes rows one at a time and commits as it goes.
"""

import argparse
import asyncio
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from context import config  # noqa: E402  (loads .env + config.yaml)
import db  # noqa: E402


async def backfill_table(table: str, build_text, force_all: bool) -> tuple[int, int]:
    """Re-embed rows of one table. Returns (updated, failed)."""
    where = "" if force_all else "WHERE embedding IS NULL"
    async with db._pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT * FROM {table} {where} ORDER BY id")

    updated = failed = 0
    for row in rows:
        text = build_text(dict(row))
        if not text.strip():
            continue
        embedding = await db.embed_text(text)
        if not embedding:
            failed += 1
            print(f"  {table} id={row['id']}: embedding FAILED ({db.embed_stats['last_error']})")
            continue
        async with db._pool.acquire() as conn:
            await conn.execute(
                f"UPDATE {table} SET embedding = $1 WHERE id = $2", embedding, row["id"]
            )
        updated += 1
        if updated % 25 == 0:
            print(f"  {table}: {updated}/{len(rows)} done")
    return updated, failed


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="re-embed every row, not just NULLs")
    args = parser.parse_args()

    await db.init_db(
        config.get("db_url", ""),
        config.get("embed_model", "text-embedding-3-small"),
        int(config.get("embed_dim", 768)),
        embed_backend=config.get("embed_backend", ""),
        embed_url=config.get("embed_url", ""),
        embed_api_key=config.get("embed_api_key", ""),
        pool_min=1,
        pool_max=4,
    )
    if db._pool is None:
        print("ERROR: could not connect to DB — check db_url / DATABASE_URL")
        sys.exit(1)

    # Probe the embedding backend before touching any rows
    probe = await db.embed_text("backfill probe")
    if not probe:
        print(f"ERROR: embedding backend unreachable ({db.embed_stats['last_error']})")
        print("Check embed_backend / embed_url / EMBED_API_KEY before re-running.")
        sys.exit(1)
    print(f"Embedding backend OK ({len(probe)} dims). Mode: {'--all' if args.all else 'NULLs only'}\n")

    # Same texts the live writers embed, so backfilled vectors match new ones
    targets = [
        ("documents", lambda r: f"{r.get('title', '')}\n{(r.get('content') or '')[:2000]}"),
        ("clients",   lambda r: f"{r['name']} {(r.get('metadata') or {}).get('industry', '')}"),
        ("contacts",  lambda r: f"{r['name']} {(r.get('metadata') or {}).get('role', '')} "
                                f"{(r.get('metadata') or {}).get('company', '')}"),
    ]
    total_updated = total_failed = 0
    for table, build_text in targets:
        print(f"Backfilling {table}…")
        updated, failed = await backfill_table(table, build_text, args.all)
        print(f"  {table}: {updated} updated, {failed} failed\n")
        total_updated += updated
        total_failed += failed

    await db.close_db()
    print(f"Done. {total_updated} rows embedded, {total_failed} failures.")
    if total_failed:
        sys.exit(2)


if __name__ == "__main__":
    asyncio.run(main())
