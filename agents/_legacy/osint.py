"""
agents/osint.py — OSINT agent for company research (Phase 6).

run_osint(client_name, org_id, run_id) is called by:
  1. _trigger_osint in server.py — fired on new client creation
  2. The daily OSINT heartbeat (via _run_heartbeat_job → per-client call)

For each company:
  1. Six targeted web searches (overview, news, leadership, financials, executives, press)
  2. Fetch top pages for richer content (up to 3 pages, 1000 chars each to reduce entity confusion)
  3. Synthesise with Ollama (qwen3.5, think=False) into a structured OSINT report
  4. Post-synthesis cross-check on Leadership + Financial sections (hallucination reduction)
  5. Write type=osint document to DB + vault file (north-info/research/{slug}/) + run log

Graceful degradation:
  - Ollama offline → write raw search snippets as content
  - Search error → skip that angle, continue with others
  - fetch_page error → skip that page, use snippets only
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Optional

import requests
import yaml

from agents.tools import _web_search, _fetch_page, _write_document

logger = logging.getLogger("whisper.agents.osint")

_cfg_path = Path(__file__).parent.parent / "config.yaml"
_base_dir = Path(__file__).parent.parent


def _load_config() -> dict:
    try:
        with open(_cfg_path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _load_agent_config() -> tuple[str, int]:
    cfg = _load_config()
    return cfg.get("agent_model", "llama3.2"), cfg.get("agent_num_ctx", 16384)


def _load_vault_path() -> Optional[Path]:
    cfg = _load_config()
    vp = cfg.get("vault_path", "")
    return Path(vp) if vp else None


def _slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug


def _call_ollama(prompt: str, model: str, num_ctx: int) -> str:
    try:
        resp = requests.post(
            "http://localhost:11434/api/chat",
            json={
                "model": model,
                "think": False,
                "stream": False,
                "messages": [{"role": "user", "content": prompt}],
                "options": {"num_ctx": num_ctx},
            },
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "").strip()
    except Exception as exc:
        logger.warning("Ollama unavailable: %s", exc)
        return ""


async def _synthesize(prompt: str, model: str, num_ctx: int) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(_call_ollama, prompt, model, num_ctx))


async def _gather_web_data(company_name: str) -> dict:
    """Run six targeted searches and fetch top pages. Returns all raw data."""
    searches = {
        "overview":   f"{company_name} company overview",
        "news":       f"{company_name} news 2026",
        "leadership": f"{company_name} CEO leadership team",
        "financials": f"{company_name} funding revenue growth",
        "executives": f'"{company_name}" CEO CFO CRO "executive team" 2025 2026',
        "press":      f'"{company_name}" news 2026 site:reuters.com OR site:bloomberg.com OR site:techcrunch.com',
    }

    all_snippets: dict[str, list[str]] = {}
    urls_seen: list[str] = []

    for angle, query in searches.items():
        result = await _web_search(query, n_results=4)
        results_list = result.get("results") or []
        all_snippets[angle] = [
            f"[{r['title']}] {r['snippet']}"
            for r in results_list if r.get("snippet")
        ]
        for r in results_list[:2]:
            url = r.get("url", "")
            if url and url not in urls_seen:
                urls_seen.append(url)

    pages_fetched: list[str] = []
    for url in urls_seen[:3]:
        page = await _fetch_page(url)
        if page.get("text") and not page.get("error"):
            # 1000 chars max — reduces entity confusion when multiple companies appear on a page
            pages_fetched.append(f"[{url}]\n{page['text'][:1000]}")

    return {"snippets": all_snippets, "pages": pages_fetched}


def _build_prompt(company_name: str, data: dict) -> str:
    snippets = data["snippets"]
    pages = data["pages"]

    def section(key: str) -> str:
        items = snippets.get(key, [])
        return "\n".join(f"  • {s}" for s in items[:3]) if items else "  (no results)"

    page_block = "\n\n---\n".join(pages[:2]) if pages else "(no page content fetched)"

    return f"""You are a B2B sales intelligence analyst. Based ONLY on the web data below, write a comprehensive OSINT report for {company_name}.

## Search Results

### Company Overview
{section("overview")}

### Recent News (2026)
{section("news")}

### Leadership
{section("leadership")}

### Financial Signals
{section("financials")}

### Executive Team
{section("executives")}

### Press Coverage
{section("press")}

## Page Content
{page_block[:3000]}

---

Write a structured OSINT report in markdown with these exact sections:

## Company Overview
What they do, their market position, and size (2-3 sentences).

## Products & Services
Their key offerings (3-5 bullets).

## Recent Developments
News, launches, or changes from 2025-2026 (3-5 bullets). Only include developments with a clear source in the data above.

## Financial Signals
Extract every financial figure you see in the data above, even if partial — revenue, growth %, headcount, margins, deal sizes, funding rounds. Include ranges if exact figures are not available. Do not say "no data found" — if you see any numbers at all, report them.

## Leadership & Key People
Known executives — include name, title, and one notable fact where available (3-6 bullets). Include CEO, CFO, CRO, or any other C-suite found in the data.

## Sales Intelligence
3-5 actionable insights for a sales team targeting this company. Each bullet must cite a specific fact from the data above — no generic statements like "they are a global leader" or "they value innovation".

## Sources
List the source URLs that informed this report.

Rules:
- Only include facts supported by the data above.
- Mark uncertain information with "(unconfirmed)".
- If a fact appears to be from before 2024, mark it with "(may be outdated)".
- Prefer press releases, earnings announcements, and news articles over Wikipedia. If a fact comes only from Wikipedia, note it.
- Do not invent data."""


_CROSS_CHECK_PROMPT = """\
Review the Leadership and Financial Signals sections from this OSINT report. \
For any claim that sounds uncertain, contradictory, or that appears to confuse this company with another entity \
(e.g. a person's title that seems implausible, a financial figure that directly contradicts another, \
a name that likely belongs to a different company), append "(unconfirmed)" to that specific bullet. \
Return ONLY the corrected ## Leadership & Key People and ## Financial Signals sections, preserving all formatting exactly.

{content}"""


def _extract_section(content: str, heading: str) -> tuple[str, int, int]:
    """Return (section_text, start_pos, end_pos) for a ## heading block."""
    m = re.search(rf"(## {re.escape(heading)}.*?)(?=\n## |\Z)", content, re.DOTALL)
    if m:
        return m.group(1), m.start(), m.end()
    return "", -1, -1


async def _cross_check(content: str, model: str, num_ctx: int) -> str:
    """Lightweight hallucination check — flags suspicious claims in Leadership + Financial sections."""
    fin_text, *_ = _extract_section(content, "Financial Signals")
    lead_text, *_ = _extract_section(content, "Leadership & Key People")
    if not fin_text and not lead_text:
        return content

    excerpt = "\n\n".join(filter(None, [lead_text, fin_text]))
    corrected = await _synthesize(_CROSS_CHECK_PROMPT.format(content=excerpt), model, num_ctx)
    if not corrected:
        return content

    result = content
    for heading in ("Leadership & Key People", "Financial Signals"):
        _, start, end = _extract_section(result, heading)
        if start == -1:
            continue
        new_m = re.search(rf"(## {re.escape(heading)}.*?)(?=\n## |\Z)", corrected, re.DOTALL)
        if new_m:
            result = result[:start] + new_m.group(1) + result[end:]
    return result


def _write_vault_file(
    vault_path: Path,
    client_name: str,
    content: str,
    run_id: Optional[int],
    today: str,
) -> Optional[Path]:
    """Write OSINT report to north-info/research/{slug}/{date}-osint.md."""
    try:
        slug = _slugify(client_name)
        research_dir = vault_path / "research" / slug
        research_dir.mkdir(parents=True, exist_ok=True)
        file_path = research_dir / f"{today}-osint.md"
        frontmatter = (
            f"---\n"
            f"source: agent\n"
            f"agent_run_id: {run_id if run_id is not None else 'null'}\n"
            f"subject: {client_name}\n"
            f"type: osint\n"
            f"osint_date: {today}\n"
            f"---\n\n"
        )
        file_path.write_text(frontmatter + content, encoding="utf-8")
        logger.info("Vault file written: %s", file_path)
        return file_path
    except Exception as exc:
        logger.warning("Failed to write vault file for '%s': %s", client_name, exc)
        return None


def _append_run_log(
    client_name: str,
    today: str,
    timestamp: str,
    data: dict,
    error: Optional[str],
) -> None:
    """Append a structured entry to data/agent_logs/osint_YYYYMMDD.md."""
    try:
        log_dir = _base_dir / "data" / "agent_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"osint_{today.replace('-', '')}.md"
        angles = list(data.get("snippets", {}).keys())
        pages = len(data.get("pages", []))
        entry = (
            f"\n## {timestamp} — {client_name}\n"
            f"- Angles: {', '.join(angles) if angles else 'none'}\n"
            f"- Pages fetched: {pages}\n"
            f"- Error: {error or 'none'}\n"
        )
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception as exc:
        logger.warning("Failed to write run log: %s", exc)


async def run_osint(
    client_name: str,
    org_id: int,
    run_id: Optional[int],
) -> dict:
    """
    Main entry point. Called by _trigger_osint in server.py.

    Returns: {"client_name": str, "doc": dict | None, "error": str | None}
    """
    model, num_ctx = _load_agent_config()
    vault_path = _load_vault_path()
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    timestamp = now.strftime("%Y-%m-%d %H:%M UTC")

    logger.info("Starting OSINT for client: %s (org=%d)", client_name, org_id)

    data: dict = {"snippets": {}, "pages": []}
    error: Optional[str] = None

    try:
        data = await _gather_web_data(client_name)

        has_data = any(data["snippets"].values()) or data["pages"]
        if not has_data:
            logger.info("No web data found for '%s' — skipping OSINT doc", client_name)
            error = "no web data found"
            _append_run_log(client_name, today, timestamp, data, error)
            return {"client_name": client_name, "doc": None, "error": error}

        prompt = _build_prompt(client_name, data)
        content = await _synthesize(prompt, model, num_ctx)

        if not content:
            # Fallback: write raw snippets so something is recorded
            lines = []
            for angle, snips in data["snippets"].items():
                if snips:
                    lines.append(f"## {angle.title()}\n" + "\n".join(f"- {s}" for s in snips))
            content = "\n\n".join(lines) or "No data retrieved."
        else:
            content = await _cross_check(content, model, num_ctx)

        # Write to DB
        doc = await _write_document(
            org_id=org_id,
            agent_run_id=run_id,
            type="osint",
            title=f"OSINT Report: {client_name}",
            content=content,
            client_name=client_name,
            metadata={
                "osint_date": today,
                "pages_fetched": len(data["pages"]),
                "angles": list(data["snippets"].keys()),
            },
        )
        logger.info("OSINT report written for '%s' (db_id=%s)", client_name, doc.get("db_id"))

        # Write to vault
        if vault_path:
            _write_vault_file(vault_path, client_name, content, run_id, today)

        # Append to run log
        _append_run_log(client_name, today, timestamp, data, None)

        return {"client_name": client_name, "doc": doc, "error": None}

    except Exception as exc:
        logger.error("OSINT failed for '%s': %s", client_name, exc)
        error = str(exc)
        _append_run_log(client_name, today, timestamp, data, error)
        return {"client_name": client_name, "doc": None, "error": error}
