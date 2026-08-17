"""
Phase 12.5 — 3-way baseline benchmark.

Compares all three research agent candidates on the same task and model:
  1. Python (embedded)  — server.py research loop   POST /api/research/trigger → DB poll
  2. Pi (TypeScript)    — Docker agent service        POST :8001/runs → poll /runs/{id}
  3. Hermes (Python)    — Docker agent service        POST :8002/runs → poll /runs/{id}

All three use openrouter/deepseek/deepseek-v4-flash for a fair, model-controlled comparison.
Subjects are tagged ([python], [pi], [hermes]) to prevent KB cross-contamination.

Metrics (uniform across all three):
  time_s      — wall clock to completion
  tool_calls  — total tool calls (Pi/Hermes live stream; Python = total research tasks)
  fetches     — fetch_page/fetch_url calls (Pi/Hermes); fetch tasks (Python)
  findings    — type=finding documents saved in DB (all three, queried from DB)
  avg_rel     — mean relevance_score across findings
  citations   — [N] inline citation count in final vault report
  sources     — unique domains in ## Sources section
  sections    — H2 headings in report body (excl. Sources)
  words       — final report word count

Usage:
  python scripts/three_way_benchmark.py
  python scripts/three_way_benchmark.py --subject "Dräger" --timeout 700
  python scripts/three_way_benchmark.py --subject "Siemens Healthineers" --pi-only
  python scripts/three_way_benchmark.py --skip-python   # skip Python baseline
"""
import argparse
import asyncio
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import asyncpg
import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

PYTHON_SERVER = "http://localhost:8000"
BRAIN = "openrouter"
MODEL = "deepseek/deepseek-v4-flash"
DEFAULT_SUBJECT = "Dräger"

TASK_TEMPLATE = (
    "Research {subject} in depth. Cover: "
    "(1) exact 2024/2025 financials — revenue, operating profit, R&D spend, headcount; "
    "(2) full leadership team with LinkedIn profiles for each executive; "
    "(3) strategic priorities, recent product launches, M&A activity; "
    "(4) sales intelligence signals — pain points, technology investments, vendor relationships, "
    "org changes; "
    "(5) recent news from the last 12 months. "
    "Fetch full pages, not just snippets. "
    "Write findings as you go, then produce a comprehensive final report."
)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _post(url: str, payload: dict, timeout: int = 15) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _get(url: str, timeout: int = 10) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------------------
# Vault report analysis (shared)
# ---------------------------------------------------------------------------

def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _analyse_report(vault_path: Path, subject: str) -> dict:
    """Extract citation count, unique source domains, section count, word count."""
    slug = _slugify(subject)
    candidates = [
        vault_path / "research" / slug / "overview.md",
        vault_path / "research" / f"{slug}-overview.md",
    ]
    for path in candidates:
        if path.exists():
            content = path.read_text(encoding="utf-8")
            body = re.sub(r"^---[\s\S]*?---\n", "", content).strip()
            citations = len(re.findall(r"\[\d+\]", body))
            sources_block = re.search(r"## Sources\s*([\s\S]*?)(?=##|$)", body)
            urls = re.findall(r"https?://[^\s\]]+", sources_block.group(1) if sources_block else "")
            domains = {re.sub(r"^https?://([^/]+).*", r"\1", u) for u in urls}
            headings = [h for h in re.findall(r"^## (.+)", body, re.MULTILINE)
                        if "source" not in h.lower()]
            words = len(body.split())
            return {
                "citations": citations,
                "sources": len(domains),
                "sections": len(headings),
                "words": words,
                "report_path": str(path),
            }
    return {"citations": 0, "sources": 0, "sections": 0, "words": 0, "report_path": None}


# ---------------------------------------------------------------------------
# DB helpers (for Python agent polling + findings query)
# ---------------------------------------------------------------------------

async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def _get_org_id(pool: asyncpg.Pool) -> Optional[int]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM orgs ORDER BY id ASC LIMIT 1")
    return row["id"] if row else None


async def _cancel_stale(pool: asyncpg.Pool, org_id: int, subject: str) -> int:
    async with pool.acquire() as conn:
        result = await conn.execute(
            """UPDATE research_tasks SET status='skipped', completed_at=NOW()
               WHERE org_id=$1 AND subject=$2 AND status IN ('pending','running')""",
            org_id, subject,
        )
    return int(result.split()[-1]) if result else 0


async def _poll_db_until_done(
    pool: asyncpg.Pool, org_id: int, subject: str, timeout: int
) -> dict:
    """Poll DB until no pending/running research tasks remain for subject."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with pool.acquire() as conn:
            pending = await conn.fetchval(
                "SELECT COUNT(*) FROM research_tasks WHERE org_id=$1 AND subject=$2 "
                "AND status IN ('pending','running')",
                org_id, subject,
            )
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM research_tasks WHERE org_id=$1 AND subject=$2",
                org_id, subject,
            )
        if total and pending == 0:
            break
        remaining = int(deadline - time.monotonic())
        print(f"    {pending} tasks pending  ({remaining}s remaining)  ", end="\r")
        await asyncio.sleep(5)
    else:
        print(f"\n    ⚠ Timeout after {timeout}s")

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT status, task_type, COUNT(*) AS cnt FROM research_tasks "
            "WHERE org_id=$1 AND subject=$2 GROUP BY status, task_type",
            org_id, subject,
        )
    stats: dict = {}
    for r in rows:
        stats.setdefault(r["status"], {})
        stats[r["status"]][r["task_type"]] = r["cnt"]
    return stats


async def _db_findings(pool: asyncpg.Pool, org_id: int, subject: str) -> list[dict]:
    async with pool.acquire() as conn:
        client_rows = await conn.fetch(
            "SELECT id FROM clients WHERE org_id=$1 "
            "AND (lower(name)=lower($2) OR lower(name) LIKE '%'||lower($2)||'%')",
            org_id, subject,
        )
        if not client_rows:
            return []
        cids = [r["id"] for r in client_rows]
        rows = await conn.fetch(
            "SELECT d.metadata FROM documents d "
            "JOIN document_links dl ON dl.document_id=d.id "
            "WHERE d.org_id=$1 AND d.type='finding' "
            "AND dl.entity_type='client' AND dl.entity_id=ANY($2)",
            org_id, cids,
        )
    return [dict(r) for r in rows]


async def _db_task_counts(pool: asyncpg.Pool, org_id: int, subject: str) -> dict:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT task_type, COUNT(*) AS cnt FROM research_tasks "
            "WHERE org_id=$1 AND subject=$2 AND status='done' GROUP BY task_type",
            org_id, subject,
        )
    return {r["task_type"]: r["cnt"] for r in rows}


# ---------------------------------------------------------------------------
# Run: Python agent (POST /api/research/trigger + DB poll)
# ---------------------------------------------------------------------------

async def run_python(
    pool: asyncpg.Pool,
    org_id: int,
    subject: str,
    vault_path: Path,
    timeout: int,
) -> dict:
    label = "Python (embedded)"
    print(f"\n{'─' * 60}")
    print(f"  {label}")
    print(f"  Subject : {subject}")
    print(f"  Brain   : {BRAIN} / {MODEL}")
    print(f"{'─' * 60}")

    stale = await _cancel_stale(pool, org_id, subject)
    if stale:
        print(f"  Cleared {stale} stale tasks")

    t0 = time.monotonic()
    try:
        resp = _post(
            f"{PYTHON_SERVER}/api/research/trigger",
            {"subject": subject, "subject_type": "company",
             "brain": BRAIN, "model": MODEL},
        )
        print(f"  Triggered → {resp}")
    except Exception as exc:
        print(f"  FAILED to trigger: {exc}")
        return {"label": label, "subject": subject, "error": str(exc)}

    await _poll_db_until_done(pool, org_id, subject, timeout)
    elapsed = round(time.monotonic() - t0, 1)
    print()

    task_counts = await _db_task_counts(pool, org_id, subject)
    findings = await _db_findings(pool, org_id, subject)

    scores = [
        int(f["metadata"].get("relevance_score", 0))
        for f in findings
        if isinstance(f.get("metadata", {}).get("relevance_score"), (int, float))
    ]
    avg_rel = round(sum(scores) / len(scores), 2) if scores else 0.0

    total_tasks = sum(task_counts.values())
    fetch_tasks = task_counts.get("fetch_url", 0)
    report = _analyse_report(vault_path, subject)

    result = {
        "label": label,
        "subject": subject,
        "time_s": elapsed,
        "tool_calls": total_tasks,       # research_tasks.done count
        "fetches": fetch_tasks,
        "findings": len(findings),
        "avg_rel": avg_rel,
        **report,
        "manual_quality": "(fill in 1–5)",
    }

    print(f"  ✓ Done in {elapsed}s")
    print(f"    Tasks done : {total_tasks}  (fetch_url: {fetch_tasks})")
    print(f"    Findings   : {len(findings)}  avg_relevance: {avg_rel}")
    print(f"    Citations  : {report['citations']}  Sources: {report['sources']}  "
          f"Sections: {report['sections']}  Words: {report['words']}")
    if report["report_path"]:
        print(f"    Report     : {report['report_path']}")
    return result


# ---------------------------------------------------------------------------
# Run: Pi or Hermes (POST /runs + poll /runs/{id})
# ---------------------------------------------------------------------------

async def run_agent_service(
    label: str,
    base_url: str,
    subject: str,
    vault_path: Path,
    pool: asyncpg.Pool,
    org_id: int,
    timeout: int,
    poll_interval: float = 3.0,
) -> dict:
    print(f"\n{'─' * 60}")
    print(f"  {label}")
    print(f"  Subject : {subject}")
    print(f"  Brain   : {BRAIN} / {MODEL}")
    print(f"{'─' * 60}")

    stale = await _cancel_stale(pool, org_id, subject)
    if stale:
        print(f"  Cleared {stale} stale tasks from Python DB")

    task = TASK_TEMPLATE.format(subject=subject)
    payload = {
        "task": task,
        "subject": subject,
        "org_id": org_id,
        "brain": BRAIN,
        "model": MODEL,
        "use_browser_fetch": False,
    }

    try:
        resp = _post(f"{base_url}/runs", payload)
    except Exception as exc:
        print(f"  FAILED to trigger: {exc}")
        return {"label": label, "subject": subject, "error": str(exc)}

    run_id = resp["run_id"]
    print(f"  Run #{run_id} started. Polling every {poll_interval}s (max {timeout}s)...")

    t0 = time.monotonic()
    deadline = t0 + timeout
    last_seen = 0

    while time.monotonic() < deadline:
        try:
            state = _get(f"{base_url}/runs/{run_id}")
        except Exception:
            await asyncio.sleep(poll_interval)
            continue

        status = state.get("status", "?")
        tcs = state.get("tool_calls", [])
        if len(tcs) > last_seen:
            for tc in tcs[last_seen:]:
                tool = tc.get("tool", "?")
                args = tc.get("args", {})
                detail = ""
                if isinstance(args, dict):
                    detail = args.get("query", args.get("url", args.get("title", "")))
                print(f"    {tool:<18} {str(detail)[:65]}")
            last_seen = len(tcs)

        if status in ("done", "failed", "timeout", "cancelled"):
            elapsed = round(time.monotonic() - t0, 1)
            print(f"\n  Status : {status}  ({elapsed}s)")
            if state.get("error"):
                print(f"  Error  : {state['error']}")

            fetches = sum(1 for tc in tcs if tc.get("tool") in ("fetch_page", "fetch_page_browser"))
            report = _analyse_report(vault_path, subject)

            findings = await _db_findings(pool, org_id, subject)
            scores = [
                int(f["metadata"].get("relevance_score", 0))
                for f in findings
                if isinstance(f.get("metadata", {}).get("relevance_score"), (int, float))
            ]
            avg_rel = round(sum(scores) / len(scores), 2) if scores else 0.0

            result = {
                "label": label,
                "subject": subject,
                "time_s": elapsed,
                "tool_calls": len(tcs),
                "fetches": fetches,
                "findings": len(findings),
                "avg_rel": avg_rel,
                **report,
                "manual_quality": "(fill in 1–5)",
            }
            print(f"  Tool calls: {len(tcs)}  Fetches: {fetches}  Findings: {len(findings)}  avg_rel: {avg_rel}")
            print(f"  Citations : {report['citations']}  Sources: {report['sources']}  "
                  f"Sections: {report['sections']}  Words: {report['words']}")
            if report["report_path"]:
                print(f"  Report    : {report['report_path']}")
            return result

        await asyncio.sleep(poll_interval)

    elapsed = round(time.monotonic() - t0, 1)
    print(f"\n  ⚠ Timed out after {elapsed}s")
    return {"label": label, "subject": subject, "status": "timeout",
            "time_s": elapsed, "error": "client timeout"}


# ---------------------------------------------------------------------------
# Render results table
# ---------------------------------------------------------------------------

def _render_table(results: list[dict], base_subject: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# 3-Way Agent Benchmark: {base_subject}",
        f"*Generated {ts}*",
        "",
        "All three candidates used the same model: "
        f"`{BRAIN}/{MODEL}`. "
        "Subjects are tagged ([python], [pi], [hermes]) for KB isolation.",
        "",
        "| Candidate | Time | Tool calls | Fetches | Findings | Avg rel | "
        "Citations | Sources | Sections | Words | Manual quality |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        if "error" in r and "time_s" not in r:
            lines.append(f"| {r['label']} | ERROR | — | — | — | — | — | — | — | — | — |")
            continue
        lines.append(
            f"| {r['label']} "
            f"| {r.get('time_s', '?')}s "
            f"| {r.get('tool_calls', '?')} "
            f"| {r.get('fetches', '?')} "
            f"| {r.get('findings', '?')} "
            f"| {r.get('avg_rel', '?')} "
            f"| {r.get('citations', '?')} "
            f"| {r.get('sources', '?')} "
            f"| {r.get('sections', '?')} "
            f"| {r.get('words', '?')} "
            f"| {r.get('manual_quality', '—')} |"
        )
    lines += [
        "",
        "**Notes:**",
        "- **Tool calls** — for Python: total `research_tasks` completed; for Pi/Hermes: live tool_calls list",
        "- **Fetches** — for Python: `fetch_url` tasks; for Pi/Hermes: `fetch_page` calls",
        "- **Findings** — `type=finding` documents saved in DB, linked to the subject client",
        "- **Avg rel** — mean `relevance_score` (1–5) across findings",
        "- **Citations** — inline `[N]` citation count in the final vault overview.md",
        "- **Sources** — unique domains in the `## Sources` section",
        "- **Manual quality** — fill in 1–5 after reading each report (1=poor 5=excellent)",
        "",
        "## Report paths",
    ]
    for r in results:
        path = r.get("report_path") or "(not written)"
        lines.append(f"- **{r['label']}**: `{path}`")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _main(
    base_subject: str,
    timeout: int,
    pi_port: int,
    hermes_port: int,
    skip_python: bool,
    skip_pi: bool,
    skip_hermes: bool,
) -> None:
    cfg_path = Path(__file__).parent.parent / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
    db_url = cfg.get("db_url", "postgresql://whisper:whisper@localhost:5432/whisper")
    vault_path = Path(cfg.get("vault_path", "")) if cfg.get("vault_path") else \
        Path(__file__).parent.parent / "north-info"

    pi_url = f"http://localhost:{pi_port}"
    hermes_url = f"http://localhost:{hermes_port}"

    # Verify services that will be used
    if not skip_python:
        try:
            _get(f"{PYTHON_SERVER}/api/clients", timeout=5)
        except Exception as exc:
            print(f"✗ Python server at {PYTHON_SERVER} — {exc}")
            print("  Run: source .venv/bin/activate && python server.py")
            sys.exit(1)
        print(f"✓ Python server at {PYTHON_SERVER}")

    for name, url, skip in [("Pi", pi_url, skip_pi), ("Hermes", hermes_url, skip_hermes)]:
        if skip:
            continue
        try:
            _get(f"{url}/health")
            print(f"✓ {name} at {url}")
        except Exception as exc:
            print(f"✗ {name} at {url} — {exc}", file=sys.stderr)
            print(f"  Run: docker compose --profile bench up {name.lower()}-service -d")
            sys.exit(1)

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=3, init=_init_conn)
    org_id = await _get_org_id(pool)
    if not org_id:
        print("No org in DB — run `python server.py` once to seed.")
        await pool.close()
        sys.exit(1)

    print(f"\nBenchmark subject : {base_subject}")
    print(f"Model             : {BRAIN}/{MODEL}")
    print(f"Vault             : {vault_path}")
    print(f"Timeout per run   : {timeout}s")
    print(f"Org id            : {org_id}")

    subjects = {
        "python": f"{base_subject} [python]",
        "pi": f"{base_subject} [pi]",
        "hermes": f"{base_subject} [hermes]",
    }
    print("\nIsolated subject names:")
    for k, v in subjects.items():
        print(f"  {k:<8} → '{v}'")

    results = []

    if not skip_python:
        r = await run_python(pool, org_id, subjects["python"], vault_path, timeout)
        results.append(r)

    if not skip_pi:
        r = await run_agent_service(
            label="Pi (TypeScript)",
            base_url=pi_url,
            subject=subjects["pi"],
            vault_path=vault_path,
            pool=pool,
            org_id=org_id,
            timeout=timeout,
        )
        results.append(r)

    if not skip_hermes:
        r = await run_agent_service(
            label="Hermes (Python async)",
            base_url=hermes_url,
            subject=subjects["hermes"],
            vault_path=vault_path,
            pool=pool,
            org_id=org_id,
            timeout=timeout,
        )
        results.append(r)

    await pool.close()

    table = _render_table(results, base_subject)

    print(f"\n{'=' * 60}")
    print("RESULTS TABLE")
    print("=" * 60)
    print(table)

    out_dir = Path(__file__).parent.parent / "data" / "benchmarks"
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = _slugify(base_subject)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    md_path = out_dir / f"{today}-3way-{slug}.md"
    json_path = out_dir / f"{today}-3way-{slug}.json"

    md_path.write_text(table, encoding="utf-8")
    json_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    print(f"\nSaved : {md_path}")
    print(f"JSON  : {json_path}")
    print("\nNext: read each overview.md, fill in Manual Quality column in the saved markdown.")


def main() -> None:
    parser = argparse.ArgumentParser(description="3-way agent benchmark: Python vs Pi vs Hermes")
    parser.add_argument("--subject", default=DEFAULT_SUBJECT,
                        help=f"Research subject (default: {DEFAULT_SUBJECT})")
    parser.add_argument("--timeout", type=int, default=700,
                        help="Max seconds per run (default: 700)")
    parser.add_argument("--pi-port", type=int, default=8001)
    parser.add_argument("--hermes-port", type=int, default=8002)
    parser.add_argument("--skip-python", action="store_true",
                        help="Skip the embedded Python agent run")
    parser.add_argument("--skip-pi", action="store_true")
    parser.add_argument("--skip-hermes", action="store_true")
    args = parser.parse_args()

    asyncio.run(_main(
        base_subject=args.subject,
        timeout=args.timeout,
        pi_port=args.pi_port,
        hermes_port=args.hermes_port,
        skip_python=args.skip_python,
        skip_pi=args.skip_pi,
        skip_hermes=args.skip_hermes,
    ))


if __name__ == "__main__":
    main()
