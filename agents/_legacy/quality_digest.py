"""
agents/quality_digest.py — Weekly research quality digest.

Queries last 7 days of findings grouped by subject, computes coverage and
relevance metrics, flags low-quality subjects, and writes a summary document
to the DB (type=research) and vault.

Called weekly by the heartbeat scheduler (agent_type: quality_digest).
No LLM required — purely data-driven.
"""
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

import db as _db

logger = logging.getLogger(__name__)

# Thresholds for flagging subjects as needing attention
MIN_FINDINGS = 3        # fewer findings than this = low coverage
MIN_AVG_RELEVANCE = 3.0 # avg relevance below this = low quality


async def run_quality_digest(org_id: int, run_id: int = -1) -> dict:
    """Generate weekly quality digest. Returns summary dict."""
    if not _db._pool:
        return {"error": "DB unavailable"}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    async with _db._pool.acquire() as conn:
        # Findings from last 7 days, grouped by linked subject
        rows = await conn.fetch(
            """
            SELECT
                COALESCE(c.name, ct.name) AS subject,
                dl.entity_type,
                COUNT(d.id)::int AS finding_count,
                ROUND(AVG((d.metadata->>'relevance_score')::float)::numeric, 2) AS avg_relevance
            FROM documents d
            JOIN document_links dl ON dl.document_id = d.id
            LEFT JOIN clients c
                ON dl.entity_type = 'client' AND dl.entity_id = c.id AND c.org_id = $1
            LEFT JOIN contacts ct
                ON dl.entity_type = 'contact' AND dl.entity_id = ct.id AND ct.org_id = $1
            WHERE d.org_id = $1
              AND d.type = 'finding'
              AND d.created_at >= NOW() - INTERVAL '7 days'
            GROUP BY subject, dl.entity_type
            ORDER BY avg_relevance ASC NULLS LAST
            """,
            org_id,
        )
        subject_stats = [dict(r) for r in rows]

        # Clients that have never had any findings
        no_research_rows = await conn.fetch(
            """
            SELECT c.name FROM clients c
            WHERE c.org_id = $1
              AND NOT EXISTS (
                SELECT 1 FROM documents d
                JOIN document_links dl ON dl.document_id = d.id
                WHERE d.org_id = $1
                  AND d.type = 'finding'
                  AND dl.entity_type = 'client'
                  AND dl.entity_id = c.id
              )
            ORDER BY c.name
            """,
            org_id,
        )
        unresearched = [r["name"] for r in no_research_rows]

    total_findings = sum(r["finding_count"] for r in subject_stats)
    avg_list = [float(r["avg_relevance"]) for r in subject_stats if r["avg_relevance"] is not None]
    overall_avg = round(sum(avg_list) / len(avg_list), 2) if avg_list else 0.0

    flagged = [
        r for r in subject_stats
        if (r["finding_count"] or 0) < MIN_FINDINGS
        or float(r["avg_relevance"] or 0) < MIN_AVG_RELEVANCE
    ]

    content = _build_markdown(today, ts, subject_stats, flagged, unresearched, total_findings, overall_avg)

    # Write to DB
    doc_id = f"quality-digest-{today}"
    embedding = _db.get_embedding(f"weekly quality digest {today}\n{content[:500]}")
    db_id = await _db.index_document(
        org_id=org_id,
        doc_id=doc_id,
        doc_type="research",
        title=f"Weekly Quality Digest — {today}",
        content=content,
        metadata={
            "digest_date": today,
            "total_findings": total_findings,
            "overall_avg_relevance": overall_avg,
            "subjects_researched": len(subject_stats),
            "flagged_subjects": len(flagged),
            "unresearched_clients": len(unresearched),
        },
        embedding=embedding,
        source="agent",
        agent_run_id=run_id if run_id != -1 else None,
    )

    # Write to vault
    _write_vault(content, today, run_id, total_findings, len(flagged))

    logger.info(
        "Quality digest written — %d subjects, %d findings, %d flagged, db_id=%s",
        len(subject_stats), total_findings, len(flagged), db_id,
    )
    return {
        "digest_date": today,
        "total_findings": total_findings,
        "overall_avg_relevance": overall_avg,
        "subjects_researched": len(subject_stats),
        "flagged": len(flagged),
        "unresearched_clients": len(unresearched),
        "db_id": db_id,
    }


def _write_vault(content: str, today: str, run_id: int, total_findings: int, flagged: int) -> None:
    try:
        cfg_path = Path(__file__).parent.parent / "config.yaml"
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
        vault_path_str = cfg.get("vault_path", "")
        if not vault_path_str:
            return
        research_dir = Path(vault_path_str) / "research"
        research_dir.mkdir(parents=True, exist_ok=True)
        out_path = research_dir / f"{today}-quality-digest.md"
        frontmatter = (
            f"---\n"
            f"source: agent\n"
            f"agent_run_id: {run_id}\n"
            f"type: quality_digest\n"
            f"digest_date: {today}\n"
            f"total_findings: {total_findings}\n"
            f"flagged_subjects: {flagged}\n"
            f"---\n\n"
        )
        out_path.write_text(frontmatter + content, encoding="utf-8")
        logger.info("Quality digest vault file: %s", out_path)
    except Exception as exc:
        logger.warning("Failed to write quality digest to vault: %s", exc)


def _build_markdown(
    today: str,
    ts: str,
    subject_stats: list,
    flagged: list,
    unresearched: list,
    total_findings: int,
    overall_avg: float,
) -> str:
    lines = [
        f"# Weekly Quality Digest — {today}",
        f"*Generated {ts} by quality_digest agent*",
        "",
        "## Summary",
        f"- **{len(subject_stats)}** subject(s) have new findings this week",
        f"- **{total_findings}** total findings saved",
        f"- **Overall avg relevance:** {overall_avg}/5",
        f"- **{len(flagged)}** subject(s) flagged for low coverage or quality",
        f"- **{len(unresearched)}** client(s) with no research in the knowledge base",
        "",
    ]

    if subject_stats:
        lines += [
            "## This Week's Research",
            "",
            "| Subject | Type | Findings | Avg Relevance | Status |",
            "|---|---|---|---|---|",
        ]
        for r in sorted(subject_stats, key=lambda x: -(x["finding_count"] or 0)):
            count = r["finding_count"] or 0
            avg = float(r["avg_relevance"] or 0)
            is_low = count < MIN_FINDINGS or avg < MIN_AVG_RELEVANCE
            status = "⚠ Low coverage" if is_low else "✓ Good"
            lines.append(
                f"| {r['subject'] or '(unknown)'} "
                f"| {r['entity_type']} "
                f"| {count} "
                f"| {avg:.1f} "
                f"| {status} |"
            )
        lines.append("")

    if flagged:
        lines += ["## Flagged — Needs Attention", ""]
        for r in flagged:
            count = r["finding_count"] or 0
            avg = float(r["avg_relevance"] or 0)
            reasons = []
            if count < MIN_FINDINGS:
                reasons.append(f"only {count} finding(s) this week (min: {MIN_FINDINGS})")
            if avg < MIN_AVG_RELEVANCE:
                reasons.append(f"avg relevance {avg:.1f}/5 (min: {MIN_AVG_RELEVANCE})")
            lines.append(f"- **{r['subject']}** — {'; '.join(reasons)}")
        lines.append("")

    if unresearched:
        lines += ["## Clients With No Research", ""]
        for name in unresearched:
            lines.append(f"- **{name}** — no findings in knowledge base")
        lines.append("")

    lines += ["## Recommended Actions", ""]
    actions = []
    for r in flagged:
        actions.append(f"- Re-run research on **{r['subject']}**")
    for name in unresearched:
        actions.append(f"- Trigger initial research on **{name}**")
    if not actions:
        actions.append("- No action items — all researched subjects have good coverage ✓")
    lines.extend(actions)

    return "\n".join(lines)
