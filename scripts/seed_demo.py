#!/usr/bin/env python3
"""
Fill a workspace with a believable demo dataset — fictional companies, contacts,
agent-written research, deals, tasks and outreach — so a fresh install has
something to show before the first real research run finishes.

  python scripts/seed_demo.py                      # seed into org "demo" (created if absent)
  python scripts/seed_demo.py --org acme-sales     # seed into an existing org by slug
  python scripts/seed_demo.py --reset              # delete the demo org's data first
  python scripts/seed_demo.py --drop               # remove the demo org entirely and exit

Every company here is invented. Nothing calls an LLM and nothing is embedded, so
vector search stays empty until you run scripts/backfill_embeddings.py --all;
full-text search works immediately.

Safety: --reset and --drop refuse to touch an org whose slug is not the one you
passed, and never touch other orgs.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncpg
import yaml

DEMO_SLUG = "demo"
DEMO_ORG_NAME = "Demo Workspace"
DEMO_USER = "demo"
DEMO_PASSWORD = "demo-password"

NOW = datetime.now(timezone.utc)


def load_db_url() -> str:
    if url := os.environ.get("DATABASE_URL"):
        return url
    cfg_path = Path(__file__).parent.parent / "config.yaml"
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)
    return cfg.get("db_url", "")


def days_ago(n: int) -> datetime:
    return NOW - timedelta(days=n)


# ---------------------------------------------------------------------------
# The dataset. Fictional throughout — any resemblance is coincidence.
# ---------------------------------------------------------------------------

CLIENTS = [
    {
        "name": "Northwind Logistics",
        "metadata": {
            "industry": "Freight forwarding",
            "website": "https://northwind-logistics.example",
            "size": "1,200 employees",
            "location": "Rotterdam, NL",
            "focus": True,
            "notes": "Mid-market forwarder, modernising a 15-year-old TMS. Warm — met at LogiTech Expo.",
        },
        "last_activity": days_ago(3).date().isoformat(),
    },
    {
        "name": "Solaris Energy",
        "metadata": {
            "industry": "Renewable energy",
            "website": "https://solaris-energy.example",
            "size": "480 employees",
            "location": "Freiburg, DE",
            "focus": True,
            "notes": "Expanding into industrial storage. Procurement is slow but budgets are real.",
        },
        "last_activity": days_ago(9).date().isoformat(),
    },
    {
        "name": "Meridian Health",
        "metadata": {
            "industry": "Private clinics",
            "website": "https://meridian-health.example",
            "size": "2,900 employees",
            "location": "Vienna, AT",
            "notes": "Compliance-heavy. Every deal goes through a data-protection review.",
        },
        "last_activity": days_ago(28).date().isoformat(),
    },
    {
        "name": "Volt & Kettle",
        "metadata": {
            "industry": "Kitchen appliances",
            "website": "https://voltkettle.example",
            "size": "310 employees",
            "location": "Manchester, UK",
            "notes": "Went quiet after the pilot. Champion left in spring.",
        },
        "last_activity": days_ago(74).date().isoformat(),
    },
]

CONTACTS = [
    ("Marta Lindqvist", "Northwind Logistics", {"role": "COO", "email": "m.lindqvist@northwind-logistics.example",
     "phone": "+31 10 555 0142", "notes": "Decides. Hates long decks, wants one number: hours saved per week."}),
    ("Tobias Reuter", "Northwind Logistics", {"role": "Head of IT", "email": "t.reuter@northwind-logistics.example",
     "notes": "Technical gatekeeper. Burned by a failed TMS migration in 2023."}),
    ("Dr. Anke Vogel", "Solaris Energy", {"role": "VP Operations", "email": "a.vogel@solaris-energy.example",
     "notes": "Sponsor for the storage rollout. Responds best on Tuesday mornings."}),
    ("Jonas Ferreira", "Solaris Energy", {"role": "Procurement Lead", "email": "j.ferreira@solaris-energy.example",
     "notes": "Wants three quotes on file before anything moves."}),
    ("Elena Marchetti", "Meridian Health", {"role": "CIO", "email": "e.marchetti@meridian-health.example",
     "notes": "Asked for the DPA and a sub-processor list before the second call."}),
    ("Priya Raman", "Volt & Kettle", {"role": "Head of Service", "email": "p.raman@voltkettle.example",
     "notes": "Inherited the pilot. Neutral, not hostile — worth one honest re-open."}),
]

# (doc_id, type, title, client, source, content, metadata, age_days)
DOCUMENTS = [
    ("demo-research-northwind", "research", "Northwind Logistics — research report", "Northwind Logistics", "agent",
     """Northwind Logistics runs a 15-year-old transport management system that its own COO
called "the reason we cannot bid on same-day freight" at LogiTech Expo. The modernisation
budget was approved in Q1 and sits with IT, not operations — which is why the technical
gatekeeper matters more than usual here.

## What changed recently
- New COO (Marta Lindqvist, since January) with an explicit mandate to cut manual dispatch work.
- Two of four regional hubs moved to a shared planning team in March; the third is scheduled for autumn.
- A failed TMS migration in 2023 left the IT team cautious about big-bang rollouts.

## Where it hurts
Dispatchers re-key shipment data between the TMS and three customer portals. Their own
estimate in the trade-press interview: "about a day a week per dispatcher".

## How to approach it
Lead with the re-keying, not the platform. A two-hub pilot with a hard measurement of hours
saved answers both the COO's number and IT's fear of a big migration.

## Sources
- [northwind-logistics.example/press/coo-appointment](https://northwind-logistics.example/press/coo-appointment)
- [logitech-expo.example/2026/sessions/keynote-panel](https://logitech-expo.example/2026/sessions/keynote-panel)
- [freightweekly.example/interviews/lindqvist](https://freightweekly.example/interviews/lindqvist)
""",
     {"service": "pi", "sources_count": 3}, 3),

    ("demo-finding-northwind-tms", "finding", "Northwind: TMS modernisation budget approved Q1", "Northwind Logistics", "agent",
     """The 2026 capital plan lists "transport platform renewal" with a budget owner in IT.
Timing note: the plan runs to the end of the fiscal year in September — unspent budget
does not roll over, which is a real reason for urgency in July and August.

## Sources
- [northwind-logistics.example/investors/capital-plan-2026](https://northwind-logistics.example/investors/capital-plan-2026)
""",
     {"service": "pi", "source_url": "https://northwind-logistics.example/investors/capital-plan-2026"}, 3),

    ("demo-signal-northwind-hub", "signal", "Northwind opens third shared planning hub in autumn", "Northwind Logistics", "agent",
     """Announced in a regional trade outlet: the Antwerp hub moves to shared planning in
October. Each hub move has preceded a tooling decision by roughly six weeks.

## Sources
- [freightweekly.example/news/northwind-antwerp-hub](https://freightweekly.example/news/northwind-antwerp-hub)
""",
     {"service": "pi", "signal_type": "opportunity", "relevance_score": 4,
      "source_url": "https://freightweekly.example/news/northwind-antwerp-hub"}, 5),

    ("demo-osint-solaris", "osint", "Solaris Energy — OSINT sweep", "Solaris Energy", "agent",
     """Solaris Energy is moving from solar installation into industrial battery storage, a
shift visible in hiring, partnerships and their own conference talks.

## Signals
- 14 open roles in storage engineering, 9 of them new this quarter.
- A supply agreement with a cell manufacturer announced in June.
- The VP Operations spoke about "grid services we could not sell two years ago".

## Risks
Procurement requires three comparable quotes for anything above €50k, and the review board
meets monthly. Deals here are not lost, they are delayed.

## Sources
- [solaris-energy.example/careers](https://solaris-energy.example/careers)
- [solaris-energy.example/press/cell-supply-agreement](https://solaris-energy.example/press/cell-supply-agreement)
- [energy-review.example/talks/storage-panel](https://energy-review.example/talks/storage-panel)
""",
     {"service": "pi", "sources_count": 3}, 9),

    ("demo-finding-solaris-procurement", "finding", "Solaris: procurement needs three quotes above €50k", "Solaris Energy", "agent",
     """Stated in their supplier portal terms. Practical consequence: send the quote early
enough that the monthly review board can take it, or lose four weeks.

## Sources
- [solaris-energy.example/suppliers/terms](https://solaris-energy.example/suppliers/terms)
""",
     {"service": "pi", "source_url": "https://solaris-energy.example/suppliers/terms"}, 9),

    ("demo-meeting-northwind", "meeting", "Discovery call — Northwind Logistics", "Northwind Logistics", "human",
     """**Attendees:** Marta Lindqvist (COO), Tobias Reuter (Head of IT), us.

Marta opened with the dispatch problem in her own words: four people spend "most of a day
a week" moving data between systems. Tobias pushed back on scope twice — he is not against
the project, he is against a migration weekend.

**Agreed**
- Two-hub pilot, Rotterdam and Duisburg, measured on dispatcher hours.
- Security review before any data leaves their network.

**Open**
- Who signs off the pilot budget: Marta thinks IT, Tobias thinks operations.

**Next step:** send the pilot outline by Friday.
""",
     {"attendees": ["Marta Lindqvist", "Tobias Reuter"], "duration_min": 42}, 3),

    ("demo-research-meridian", "research", "Meridian Health — research report", "Meridian Health", "agent",
     """Meridian Health operates eleven private clinics and is consolidating patient
administration onto one platform. Every vendor decision passes a data-protection review,
which is the real sales cycle here — the clinical case is usually the easy part.

## What matters
- A data-processing agreement and a sub-processor list are asked for before the second call.
- The CIO has publicly said they will not accept US-only hosting for patient-adjacent data.
- Consolidation runs until the end of next year, so there is a window, not a rush.

## Sources
- [meridian-health.example/about/technology](https://meridian-health.example/about/technology)
- [clinicdigital.example/interviews/marchetti](https://clinicdigital.example/interviews/marchetti)
""",
     {"service": "pi", "sources_count": 2}, 28),

    ("demo-outreach-solaris", "outreach", "Follow-up: storage rollout timing — Dr. Anke Vogel", "Solaris Energy", "agent",
     """**To:** a.vogel@solaris-energy.example
**Subject:** Three quotes before the September board?

Dr. Vogel,

you mentioned the review board meets monthly and needs three comparable quotes. If the
storage rollout is to start this year, ours should be with you before the September
meeting — I can have it over by Friday if that helps you make the deadline.

One question so the quote is not generic: is the first site Freiburg or the Karlsruhe
plant? The commissioning effort differs enough to matter for the number.

Best regards
""",
     {"state": "pending_approval", "contact": "Dr. Anke Vogel",
      "contact_email": "a.vogel@solaris-energy.example", "drafted_by": "agent"}, 1),
]

DEALS = [
    ("Northwind Logistics", "TMS pilot — two hubs", "proposal", 48000, 60, 21, "open"),
    ("Solaris Energy", "Industrial storage rollout phase 1", "qualified", 120000, None, 45, "open"),
    ("Meridian Health", "Clinic platform consolidation", "lead", 90000, None, 120, "open"),
    ("Volt & Kettle", "Service desk pilot", "negotiation", 26000, 40, -12, "open"),
]

TASKS = [
    ("Northwind Logistics", "Send pilot outline to Marta", "Two hubs, dispatcher hours as the metric.", 1, 2, None),
    ("Solaris Energy", "Quote in before the September board", "Ask Freiburg vs Karlsruhe first.", 3, 1, None),
    ("Volt & Kettle", "Re-open with Priya after the champion left", "Honest reset, no pitch.", -2, 4, None),
    ("Meridian Health", "Send DPA and sub-processor list", None, 5, 3, "monthly"),
]

CONTACT_LOG = [
    ("Northwind Logistics", "Marta Lindqvist", "m.lindqvist@northwind-logistics.example",
     "Pilot outline as discussed", 3, True),
    ("Solaris Energy", "Dr. Anke Vogel", "a.vogel@solaris-energy.example",
     "Storage rollout — next steps", 11, False),
    ("Volt & Kettle", "Priya Raman", "p.raman@voltkettle.example",
     "Checking in after the pilot", 40, False),
]

AGENT_RUNS = [
    ("research", "done", "Research Northwind Logistics in depth for a B2B sales context", 3,
     {"tool_calls_made": 41, "searches_made": 18, "documents_written": 3}),
    ("osint", "done", "OSINT sweep: Solaris Energy", 9,
     {"tool_calls_made": 63, "searches_made": 31, "documents_written": 2}),
    ("research", "done", "Research Meridian Health in depth for a B2B sales context", 28,
     {"tool_calls_made": 37, "searches_made": 15, "documents_written": 1}),
    ("autonomy_review", "done", "Autonomy: Volt & Kettle — skip (no change since last run)", 1,
     {"decision": "skip", "reason": "no news fingerprint change in 30 days"}),
]


# ---------------------------------------------------------------------------

async def ensure_org_and_user(conn) -> tuple[int, int, str]:
    """Return (org_id, user_id, note). Creates the demo org + user when missing."""
    org = await conn.fetchrow("SELECT id, name FROM orgs WHERE slug = $1", ARGS.org)
    if not org:
        if ARGS.org != DEMO_SLUG:
            raise SystemExit(f"No org with slug '{ARGS.org}'. Create it first, or run without --org.")
        org = await conn.fetchrow(
            "INSERT INTO orgs (name, slug) VALUES ($1, $2) RETURNING id, name",
            DEMO_ORG_NAME, DEMO_SLUG,
        )
    org_id = org["id"]

    user = await conn.fetchrow("SELECT id FROM users WHERE org_id = $1 ORDER BY id LIMIT 1", org_id)
    note = ""
    if not user:
        from passlib.context import CryptContext
        pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
        user = await conn.fetchrow(
            """INSERT INTO users (org_id, username, display_name, email, role, password_hash)
               VALUES ($1, $2, $3, $4, 'admin', $5) RETURNING id""",
            org_id, DEMO_USER, "Demo Rep", "demo@example.com", pwd.hash(DEMO_PASSWORD),
        )
        note = f"created user '{DEMO_USER}' with password '{DEMO_PASSWORD}'"
    return org_id, user["id"], note


async def wipe(conn, org_id: int) -> None:
    """Remove seeded rows from this org. Documents cascade to document_links."""
    await conn.execute("DELETE FROM documents WHERE org_id = $1 AND doc_id LIKE 'demo-%'", org_id)
    await conn.execute("DELETE FROM deal_events WHERE org_id = $1", org_id)
    await conn.execute("DELETE FROM deals WHERE org_id = $1", org_id)
    await conn.execute("DELETE FROM user_tasks WHERE org_id = $1", org_id)
    await conn.execute("DELETE FROM contact_log WHERE org_id = $1", org_id)
    await conn.execute("DELETE FROM agent_runs WHERE org_id = $1", org_id)
    await conn.execute("DELETE FROM contacts WHERE org_id = $1", org_id)
    await conn.execute("DELETE FROM clients WHERE org_id = $1", org_id)


async def seed(conn, org_id: int, user_id: int) -> dict:
    counts = {"clients": 0, "contacts": 0, "documents": 0, "deals": 0, "tasks": 0, "mails": 0, "runs": 0}
    client_ids: dict[str, int] = {}
    contact_ids: dict[str, int] = {}

    for c in CLIENTS:
        row = await conn.fetchrow(
            """INSERT INTO clients (org_id, name, metadata, session_count, last_activity, created_by)
               VALUES ($1, $2, $3::jsonb, $4, $5, $6)
               ON CONFLICT (org_id, name) DO UPDATE SET metadata = EXCLUDED.metadata
               RETURNING id""",
            org_id, c["name"], json.dumps(c["metadata"]), 1, c["last_activity"], user_id,
        )
        client_ids[c["name"]] = row["id"]
        counts["clients"] += 1

    for name, client, meta in CONTACTS:
        meta = {**meta, "company": client}
        row = await conn.fetchrow(
            """INSERT INTO contacts (org_id, client_id, name, metadata, created_by)
               VALUES ($1, $2, $3, $4::jsonb, $5)
               ON CONFLICT (org_id, name) DO UPDATE SET metadata = EXCLUDED.metadata
               RETURNING id""",
            org_id, client_ids[client], name, json.dumps(meta), user_id,
        )
        contact_ids[name] = row["id"]
        counts["contacts"] += 1

    run_ids: list[int] = []
    for agent_type, status, task, age, output in AGENT_RUNS:
        row = await conn.fetchrow(
            """INSERT INTO agent_runs (org_id, triggered_by, trigger_type, agent_type, status, task,
                                       output, created_at, completed_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9) RETURNING id""",
            org_id, user_id, "autonomous" if agent_type == "autonomy_review" else "user",
            agent_type, status, task, json.dumps(output),
            days_ago(age), days_ago(age) + timedelta(minutes=7),
        )
        run_ids.append(row["id"])
        counts["runs"] += 1

    for doc_id, dtype, title, client, source, content, meta, age in DOCUMENTS:
        run_id = run_ids[0] if source == "agent" and run_ids else None
        row = await conn.fetchrow(
            """INSERT INTO documents (org_id, doc_id, type, title, content, metadata, source,
                                      agent_run_id, created_by, created_at, updated_at)
               VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $10)
               ON CONFLICT (org_id, doc_id) DO UPDATE
                 SET content = EXCLUDED.content, metadata = EXCLUDED.metadata
               RETURNING id""",
            org_id, doc_id, dtype, title, content, json.dumps(meta), source,
            run_id, user_id, days_ago(age),
        )
        await conn.execute(
            "INSERT INTO document_links (document_id, entity_type, entity_id) VALUES ($1, 'client', $2) "
            "ON CONFLICT DO NOTHING",
            row["id"], client_ids[client],
        )
        counts["documents"] += 1

    for client, name, stage, value, prob, close_in, status in DEALS:
        row = await conn.fetchrow(
            """INSERT INTO deals (org_id, client_id, name, stage, value, currency, probability,
                                  expected_close, owner_user_id, status, created_by)
               VALUES ($1, $2, $3, $4, $5, 'EUR', $6, $7, $8, $9, $8) RETURNING id""",
            org_id, client_ids[client], name, stage, value, prob,
            date.today() + timedelta(days=close_in), user_id, status,
        )
        await conn.execute(
            """INSERT INTO deal_events (org_id, deal_id, kind, to_value, actor_user_id, created_at)
               VALUES ($1, $2, 'created', $3, $4, $5)""",
            org_id, row["id"], stage, user_id, days_ago(30),
        )
        counts["deals"] += 1

    for client, title, notes, due_in, prio, recurrence in TASKS:
        await conn.execute(
            """INSERT INTO user_tasks (org_id, user_id, client_name, title, notes, due_date,
                                       priority, status, source, recurrence)
               VALUES ($1, $2, $3, $4, $5, $6, $7, 'open', 'manual', $8)""",
            org_id, user_id, client, title, notes,
            date.today() + timedelta(days=due_in), prio, recurrence,
        )
        counts["tasks"] += 1

    for client, contact, email, subject, age, replied in CONTACT_LOG:
        await conn.execute(
            """INSERT INTO contact_log (org_id, user_id, client_name, contact_name, contact_email,
                                        subject, body, sent_at, replied)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
            org_id, user_id, client, contact, email, subject,
            "(demo message body)", days_ago(age), replied,
        )
        counts["mails"] += 1

    return counts


async def main() -> None:
    db_url = load_db_url()
    if not db_url:
        raise SystemExit("No database URL — set DATABASE_URL or db_url in config.yaml.")
    conn = await asyncpg.connect(db_url)
    try:
        if ARGS.drop:
            if ARGS.org != DEMO_SLUG:
                raise SystemExit("--drop only removes the demo org, not '%s'." % ARGS.org)
            deleted = await conn.execute("DELETE FROM orgs WHERE slug = $1", DEMO_SLUG)
            print(f"Demo org removed ({deleted}).")
            return

        org_id, user_id, note = await ensure_org_and_user(conn)
        if ARGS.reset:
            await wipe(conn, org_id)
            print("Existing demo data removed.")
        counts = await seed(conn, org_id, user_id)

        print(f"Seeded org '{ARGS.org}' (id {org_id}): " +
              ", ".join(f"{v} {k}" for k, v in counts.items()))
        if note:
            print("  " + note)
        print("  Log in and look at Today, a client page, the pipeline board and search.")
        print("  Vector search stays empty until: python scripts/backfill_embeddings.py --all")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed a demo dataset (fictional companies).")
    parser.add_argument("--org", default=DEMO_SLUG, help="org slug to seed into (default: demo)")
    parser.add_argument("--reset", action="store_true", help="delete this org's seeded data first")
    parser.add_argument("--drop", action="store_true", help="delete the demo org entirely and exit")
    ARGS = parser.parse_args()
    asyncio.run(main())
