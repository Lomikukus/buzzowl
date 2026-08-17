#!/usr/bin/env python3
"""
Backfill metadata.website for clients that have none, then optionally re-run
source discovery so their news/press pages get monitored.

Resolution per client: SearXNG heuristic (domain resembles company name,
aggregators/registries excluded) → LLM pick from the candidates as fallback
(model can only choose from presented domains, never invent one).

Usage (inside the server container):
    python scripts/backfill_websites.py --limit 5        # sample run
    python scripts/backfill_websites.py --rediscover     # full run + source discovery

Idempotent: clients with a website are skipped; discovery merges, never duplicates.
"""

import argparse
import asyncio
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from context import config  # noqa: E402
import db  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="only process the first N clients")
    parser.add_argument("--rediscover", action="store_true",
                        help="run source discovery for clients that gained a website")
    args = parser.parse_args()

    await db.init_db(config.get("db_url", ""), config.get("embed_model", ""), 768,
                     pool_min=1, pool_max=3)
    if db._pool is None:
        print("ERROR: DB unavailable")
        sys.exit(1)

    from routers.pipeline import _resolve_client_website, _discover_client_sources

    org = await db.get_first_org()
    clients = await db.list_clients(org["id"])
    todo = [c for c in clients if not ((c.get("metadata") or {}).get("website") or "").strip()]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(clients)} clients, {len(todo)} without website — resolving…\n")

    resolved = {"heuristic": 0, "llm": 0, "none": 0}
    for i, c in enumerate(todo):
        if i:
            await asyncio.sleep(4)   # pace SearXNG — rapid-fire queries hit engine rate limits
        website = await _resolve_client_website(org["id"], c)
        if website:
            updated = await db.get_client(org["id"], c["name"])
            source = (updated.get("metadata") or {}).get("website_source", "?")
            resolved[source if source in resolved else "heuristic"] += 1
            print(f"  ✓ {c['name']:<40} {website}  ({source})")
            if args.rediscover:
                sources = await _discover_client_sources(org["id"], updated)
                print(f"      sources: {len(sources)}")
        else:
            resolved["none"] += 1
            print(f"  ✗ {c['name']:<40} not found")

    print(f"\nDone. heuristic={resolved['heuristic']}  llm={resolved['llm']}  unresolved={resolved['none']}")
    await db.close_db()


if __name__ == "__main__":
    asyncio.run(main())
