"""
Phase 7.75 model evaluation — Deep Research Engine benchmark.

Each model researches the same company in full isolation: the subject is labeled
"{base_subject} [{model_tag}]" so findings, tasks, and the vault directory are
completely separate per model.  The LLM only sees "{base_subject}" in its prompts
(the tag is stripped by _display_subject in workers/delegator/orchestrator).

Metrics collected per model:
  - time_total (s)            — wall clock from trigger to queue empty
  - tasks_done / tasks_failed — from research_tasks table
  - findings_saved            — type=finding docs linked to the labeled client
  - avg_relevance             — mean relevance_score across findings (1–5)
  - mean_query_score          — mean of query_score stored on web_search tasks
  - sections_complete         — sections present in aggregated overview

Requires:
  - Server running at http://localhost:8000
  - PostgreSQL accessible via db_url in config.yaml
  - Ollama running (for local models)

Usage:
  python scripts/research_benchmark.py
  python scripts/research_benchmark.py --subject "Bayer" --subject-type company
  python scripts/research_benchmark.py --subject "Siemens" --timeout 900
"""
import argparse
import asyncio
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import asyncpg
import requests
import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

SERVER = "http://localhost:8000"

COMPANY_SECTIONS = [
    "## overview", "## leadership", "## financial",
    "## products", "## market", "## sales", "## sources",
]
PERSON_SECTIONS = [
    "## role", "## what they talk", "## recent public",
    "## areas of expertise", "## sales engagement", "## sources",
]


def _load_config() -> dict:
    cfg_path = Path(__file__).parent.parent / "config.yaml"
    try:
        with open(cfg_path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _model_short_tag(label: str) -> str:
    """'ollama/qwen3.5' → 'qwen3.5',  'openrouter/deepseek-v4-pro' → 'deepseek-v4-pro'."""
    return label.split("/", 1)[-1] if "/" in label else label


def _slugify(s: str) -> str:
    slug = s.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


async def _cancel_pending(conn: asyncpg.Connection, org_id: int, subject: str) -> int:
    """Mark any pending/running tasks for subject as skipped to start clean."""
    result = await conn.execute(
        """
        UPDATE research_tasks
        SET status = 'skipped', completed_at = NOW()
        WHERE org_id = $1 AND subject = $2 AND status IN ('pending', 'running')
        """,
        org_id, subject,
    )
    return int(result.split()[-1]) if result else 0


async def _poll_until_done(
    conn: asyncpg.Connection,
    org_id: int,
    subject: str,
    timeout: int,
) -> dict:
    """Poll every 5s until all non-aggregate tasks for subject are terminal or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = await conn.fetchval(
            """
            SELECT COUNT(*) FROM research_tasks
            WHERE org_id = $1 AND subject = $2
              AND status IN ('pending', 'running')
            """,
            org_id, subject,
        )
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM research_tasks WHERE org_id = $1 AND subject = $2",
            org_id, subject,
        )
        if total and pending == 0:
            break
        remaining = int(deadline - time.monotonic())
        print(f"    {pending} tasks pending  ({remaining}s remaining)  ", end="\r")
        await asyncio.sleep(5)
    else:
        print(f"\n    ⚠ Timeout after {timeout}s")

    rows = await conn.fetch(
        """
        SELECT status, COUNT(*) AS cnt FROM research_tasks
        WHERE org_id = $1 AND subject = $2 AND task_type != 'aggregate'
        GROUP BY status
        """,
        org_id, subject,
    )
    return {r["status"]: r["cnt"] for r in rows}


async def _get_findings(
    conn: asyncpg.Connection,
    org_id: int,
    subject: str,
) -> list[dict]:
    """Return type=finding documents linked to the labeled subject client.

    Labeled clients are always brand-new (e.g. 'Bayer [qwen3.5]' never existed before
    this benchmark run), so we query all linked findings without a time filter.
    The same underlying finding document can be linked to multiple model clients via
    document_links — each model's count reflects what it would have surfaced.
    """
    client_rows = await conn.fetch(
        """
        SELECT id FROM clients
        WHERE org_id = $1
          AND (lower(name) = lower($2) OR lower(name) LIKE '%' || lower($2) || '%')
        """,
        org_id, subject,
    )
    if not client_rows:
        return []
    client_ids = [r["id"] for r in client_rows]
    rows = await conn.fetch(
        """
        SELECT d.metadata
        FROM documents d
        JOIN document_links dl ON dl.document_id = d.id
        WHERE d.org_id = $1 AND d.type = 'finding'
          AND dl.entity_type = 'client' AND dl.entity_id = ANY($2)
        """,
        org_id, client_ids,
    )
    return [dict(r) for r in rows]


async def _get_query_scores(
    conn: asyncpg.Connection,
    org_id: int,
    subject: str,
) -> Optional[float]:
    """Return mean query_score across all web_search tasks for this labeled subject."""
    rows = await conn.fetch(
        """
        SELECT (result->>'query_score')::float AS qs
        FROM research_tasks
        WHERE org_id = $1 AND subject = $2
          AND task_type = 'web_search'
          AND result ? 'query_score'
          AND status = 'done'
        """,
        org_id, subject,
    )
    scores = [r["qs"] for r in rows if r["qs"] is not None]
    return round(sum(scores) / len(scores), 2) if scores else None


def _count_sections(vault_path: Path, subject: str, subject_type: str) -> int:
    """Count recognised sections in the aggregated overview.md / profile.md for this subject."""
    slug = _slugify(subject)
    fname = "overview.md" if subject_type == "company" else "profile.md"
    path = vault_path / "research" / slug / fname
    if not path.exists():
        return 0
    content = path.read_text(encoding="utf-8").lower()
    expected = COMPANY_SECTIONS if subject_type == "company" else PERSON_SECTIONS
    return sum(1 for s in expected if s in content)


async def _trigger_aggregate(labeled_subject: str, subject_type: str, pool: asyncpg.Pool, org_id: int) -> bool:
    """POST /api/research/aggregate for labeled_subject, then wait up to 120s for it to complete."""
    print(f"  Triggering manual aggregation for '{labeled_subject}'...")
    try:
        resp = requests.post(
            f"{SERVER}/api/research/aggregate",
            json={"subject": labeled_subject, "subject_type": subject_type},
            timeout=10,
        )
        resp.raise_for_status()
        task_id = resp.json().get("task_id")
        print(f"  Aggregate task enqueued (id={task_id}). Waiting up to 120s...")
    except Exception as exc:
        print(f"  Failed to trigger aggregate: {exc}")
        return False

    # Poll until the aggregate task is terminal
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        async with pool.acquire() as conn:
            pending = await conn.fetchval(
                """
                SELECT COUNT(*) FROM research_tasks
                WHERE org_id = $1 AND subject = $2
                  AND task_type = 'aggregate'
                  AND status IN ('pending', 'running')
                """,
                org_id, labeled_subject,
            )
        if pending == 0:
            return True
        await asyncio.sleep(5)
    print("  ⚠ Aggregate did not complete within 120s")
    return False


async def _run_one(
    pool: asyncpg.Pool,
    org_id: int,
    labeled_subject: str,
    base_subject: str,
    subject_type: str,
    brain: str,
    model: str,
    label: str,
    vault_path: Optional[Path],
    timeout: int,
) -> dict:
    print(f"\n{'─' * 60}")
    print(f"  Model   : {label}")
    print(f"  Subject : {labeled_subject}  (researches: {base_subject})")
    print(f"{'─' * 60}")

    async with pool.acquire() as conn:
        skipped = await _cancel_pending(conn, org_id, labeled_subject)
        if skipped:
            print(f"  Cleared {skipped} stale tasks from previous run")

    t0 = time.monotonic()
    trigger_wall = time.time()
    try:
        payload: dict = {
            "subject": labeled_subject,
            "subject_type": subject_type,
        }
        if brain and brain != "default":
            payload["brain"] = brain
        if model:
            payload["model"] = model
        resp = requests.post(f"{SERVER}/api/research/trigger", json=payload, timeout=10)
        resp.raise_for_status()
        print(f"  Triggered. Waiting up to {timeout}s...")
    except Exception as exc:
        print(f"  FAILED to trigger: {exc}")
        return {"label": label, "labeled_subject": labeled_subject, "error": str(exc)}

    async with pool.acquire() as conn:
        task_stats = await _poll_until_done(conn, org_id, labeled_subject, timeout)

    # Explicitly trigger aggregation in case the auto-trigger race-conditioned out.
    # Only trigger if we have findings but no overview yet.
    needs_agg = vault_path and not (vault_path / "research" / _slugify(labeled_subject) / "overview.md").exists()
    if needs_agg:
        agg_ok = await _trigger_aggregate(labeled_subject, subject_type, pool, org_id)
        if agg_ok:
            # Re-poll to include aggregate task in final stats
            async with pool.acquire() as conn:
                task_stats = await _poll_until_done(conn, org_id, labeled_subject, timeout=180)

    elapsed = round(time.monotonic() - t0, 1)
    print()

    async with pool.acquire() as conn:
        findings = await _get_findings(conn, org_id, labeled_subject)
        mean_qs = await _get_query_scores(conn, org_id, labeled_subject)

    scores = [
        int(f["metadata"].get("relevance_score", 0))
        for f in findings
        if isinstance(f.get("metadata", {}).get("relevance_score"), (int, float))
    ]
    avg_rel = round(sum(scores) / len(scores), 2) if scores else 0.0

    max_sections = len(COMPANY_SECTIONS) if subject_type == "company" else len(PERSON_SECTIONS)
    sections = 0
    if vault_path:
        sections = _count_sections(vault_path, labeled_subject, subject_type)

    result = {
        "label": label,
        "labeled_subject": labeled_subject,
        "brain": brain,
        "model": model,
        "time_total": elapsed,
        "tasks_done": task_stats.get("done", 0),
        "tasks_failed": task_stats.get("failed", 0),
        "findings_saved": len(findings),
        "avg_relevance": avg_rel,
        "mean_query_score": mean_qs,
        "sections_complete": f"{sections}/{max_sections}",
        "overview_written": sections > 0,
    }

    print(f"  ✓ Done in {elapsed}s")
    print(f"    Tasks  : {result['tasks_done']} done / {result['tasks_failed']} failed")
    print(f"    Findings: {result['findings_saved']}  avg_relevance: {avg_rel}")
    print(f"    Query score (mean): {mean_qs or '–'}")
    print(f"    Sections: {result['sections_complete']}  overview: {'✓' if sections > 0 else '✗'}")
    if vault_path:
        vault_dir = vault_path / "research" / _slugify(labeled_subject)
        print(f"    Vault dir: {vault_dir}")

    return result


def _render_table(results: list[dict], base_subject: str, subject_type: str) -> str:
    max_sec = len(COMPANY_SECTIONS) if subject_type == "company" else len(PERSON_SECTIONS)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Research Benchmark: {base_subject} ({subject_type}) — isolated per model",
        f"*Generated {ts}*",
        "",
        "Each model researched a separately labeled client (e.g. `Bayer [qwen3.5]`) "
        "so findings are fully isolated — no cross-contamination, every model starts from zero.",
        "",
        f"| Model | Client label | Time | Tasks ✓/✗ | Findings | Avg Rel | Query Score | Sections | Manual Quality |",
        f"|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        if "error" in r:
            lines.append(f"| {r['label']} | {r.get('labeled_subject', '?')} | ERROR | — | — | — | — | — | — |")
            continue
        qs = r.get("mean_query_score")
        lines.append(
            f"| {r['label']} "
            f"| `{r.get('labeled_subject', '?')}` "
            f"| {r['time_total']}s "
            f"| {r['tasks_done']}/{r['tasks_failed']} "
            f"| {r['findings_saved']} "
            f"| {r['avg_relevance']} "
            f"| {qs if qs is not None else '–'} "
            f"| {r['sections_complete']} "
            f"| (fill in 1–5) |"
        )
    lines += [
        "",
        "**Column guide:**",
        "- **Client label** — the isolated client name; vault files are at `north-info/research/<slug>/`",
        "- **Time** — wall clock from trigger to all tasks terminal",
        "- **Tasks ✓/✗** — tasks completed / failed (excludes aggregate tasks)",
        "- **Findings** — `type=finding` docs saved and linked to the labeled client",
        "- **Avg Rel** — mean `relevance_score` across all findings (1–5 scale)",
        f"- **Query Score** — mean of `query_score` on `web_search` tasks (1–5)",
        f"- **Sections** — sections present in aggregated overview (/{max_sec})",
        "- **Manual Quality** — fill in after reading each model's `overview.md`: 1=poor, 5=excellent",
    ]
    return "\n".join(lines)


async def _main(base_subject: str, subject_type: str, timeout: int) -> None:
    cfg = _load_config()
    db_url = cfg.get("db_url", "postgresql://whisper:whisper@localhost:5432/whisper")
    vault_path_str = cfg.get("vault_path", "")
    vault_path = Path(vault_path_str) if vault_path_str else None

    benchmark_models = cfg.get("benchmark_models", [
        {"brain": "ollama", "model": "qwen3.5",  "label": "ollama/qwen3.5"},
        {"brain": "ollama", "model": "llama3.2", "label": "ollama/llama3.2"},
    ])

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=3, init=_init_conn)

    async with pool.acquire() as conn:
        org = await conn.fetchrow("SELECT id FROM orgs ORDER BY id ASC LIMIT 1")
        if not org:
            print("No org found — is the DB populated? (run `docker compose up -d` then `python server.py`)")
            await pool.close()
            return
        org_id = org["id"]

    print(f"\nBenchmarking '{base_subject}' ({subject_type}) — {len(benchmark_models)} model(s), fully isolated")
    print(f"Server  : {SERVER}")
    print(f"Timeout : {timeout}s per model")
    print(f"Vault   : {vault_path or '(not configured)'}")
    print()
    print("Isolated client names per model:")
    for m in benchmark_models:
        tag = _model_short_tag(m.get("label", m.get("model", "?")))
        print(f"  {m.get('label', '?'):35s} → '{base_subject} [{tag}]'")

    results = []
    for m in benchmark_models:
        tag = _model_short_tag(m.get("label", m.get("model", "?")))
        labeled_subject = f"{base_subject} [{tag}]"
        result = await _run_one(
            pool=pool,
            org_id=org_id,
            labeled_subject=labeled_subject,
            base_subject=base_subject,
            subject_type=subject_type,
            brain=m.get("brain", "ollama"),
            model=m.get("model", cfg.get("agent_model", "llama3.2")),
            label=m.get("label", f"{m.get('brain', 'ollama')}/{m.get('model', '?')}"),
            vault_path=vault_path,
            timeout=timeout,
        )
        results.append(result)

    await pool.close()

    table = _render_table(results, base_subject, subject_type)

    print(f"\n{'=' * 60}")
    print("RESULTS TABLE")
    print("=" * 60)
    print(table)

    out_dir = Path(__file__).parent.parent / "data" / "benchmarks"
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = _slugify(base_subject)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    md_path = out_dir / f"{today}-{slug}.md"
    json_path = out_dir / f"{today}-{slug}.json"

    md_path.write_text(table, encoding="utf-8")
    json_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    print(f"\nResults saved to {md_path}")
    print(f"Raw JSON saved to {json_path}")
    print()
    print("Next steps:")
    print("  For each model, open its vault overview and fill in Manual Quality:")
    if vault_path:
        for r in results:
            if "error" not in r:
                slug_label = _slugify(r.get("labeled_subject", "?"))
                overview = vault_path / "research" / slug_label / "overview.md"
                print(f"    {r['label']:40s} → {overview}")
    print(f"  Then edit {md_path.name} with your scores.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Deep Research Engine model benchmark — isolated per model")
    parser.add_argument(
        "--subject",
        default="Bayer",
        help="Base research subject — each model gets its own labeled client (default: Bayer)",
    )
    parser.add_argument(
        "--subject-type",
        default="company",
        choices=["company", "person"],
        help="Subject type (default: company)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=900,
        help="Max seconds to wait per model run (default: 900)",
    )
    args = parser.parse_args()
    asyncio.run(_main(args.subject, args.subject_type, args.timeout))


if __name__ == "__main__":
    main()
