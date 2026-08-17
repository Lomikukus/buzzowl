"""
Phase 12.5 — Agent service 4-way benchmark.

Tests: Pi (port 8001) and Hermes (port 8002), each with and without browser-fetch.
Same subject, same task, isolated client names — fully comparable results.

Metrics per run:
  - time_s          — wall clock to completion
  - tool_calls      — total tool calls made
  - fetches         — fetch_page calls (proxy for page-reading depth)
  - findings_saved  — type=finding documents written
  - citations       — [N] citation count in final report
  - sources         — unique domains in ## Sources section
  - linkedin_hit    — whether LinkedIn content appears in the report
  - sections        — H2 headings in the report body (excl. Sources)
  - words           — word count of the report
  - manual_quality  — fill in 1–5 after reading each report

Usage:
  python scripts/agent_benchmark.py
  python scripts/agent_benchmark.py --subject "Fresenius SE" --timeout 600
  python scripts/agent_benchmark.py --subject "Fresenius SE" --hermes-port 8002 --pi-port 8001
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only — no extra deps)
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
# Metric extraction from vault overview.md
# ---------------------------------------------------------------------------

def _slugify(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')


def _analyse_report(vault_path: Path, subject: str) -> dict:
    slug = _slugify(subject)
    candidates = [
        vault_path / "research" / slug / "overview.md",
        vault_path / "research" / f"{slug}-overview.md",
    ]
    for path in candidates:
        if path.exists():
            content = path.read_text(encoding="utf-8")
            # strip frontmatter
            body = re.sub(r'^---[\s\S]*?---\n', '', content).strip()
            citations = len(re.findall(r'\[\d+\]', body))
            sources_block = re.search(r'## Sources\s*([\s\S]*?)(?=##|$)', body)
            urls = re.findall(r'https?://[^\s\]]+', sources_block.group(1) if sources_block else "")
            domains = set(re.sub(r'^https?://([^/]+).*', r'\1', u) for u in urls)
            headings = [h for h in re.findall(r'^## (.+)', body, re.MULTILINE)
                        if 'source' not in h.lower()]
            words = len(body.split())
            linkedin_hit = 'linkedin.com' in body.lower() or 'linkedin' in body.lower()
            return {
                "citations": citations,
                "sources": len(domains),
                "linkedin_hit": linkedin_hit,
                "sections": len(headings),
                "words": words,
                "report_path": str(path),
            }
    return {"citations": 0, "sources": 0, "linkedin_hit": False, "sections": 0, "words": 0, "report_path": None}


# ---------------------------------------------------------------------------
# Single run
# ---------------------------------------------------------------------------

TASK_TEMPLATE = (
    "Research {subject} in depth. Cover: "
    "(1) exact 2024/2025 financials — revenue, operating profit, R&D spend, headcount; "
    "(2) full leadership team with LinkedIn profiles for each executive; "
    "(3) strategic priorities, recent product launches, M&A activity; "
    "(4) sales intelligence signals — pain points, technology investments, vendor relationships, org changes; "
    "(5) recent news from the last 12 months. "
    "Fetch full pages, not just snippets. "
    "Go deep on LinkedIn profiles of named executives. "
    "Write findings as you go, then produce a comprehensive final report."
)


def run_one(
    label: str,
    subject: str,
    base_url: str,
    use_browser_fetch: bool,
    poll_interval: float,
    timeout: int,
    vault_path: Path,
) -> dict:
    task = TASK_TEMPLATE.format(subject=subject)
    payload = {
        "task": task,
        "subject": subject,
        "org_id": 1,
        "use_browser_fetch": use_browser_fetch,
    }

    print(f"\n{'─' * 60}")
    print(f"  {label}")
    print(f"  Subject : {subject}")
    print(f"  Browser : {'Jina.ai reader' if use_browser_fetch else 'plain httpx'}")
    print(f"{'─' * 60}")

    try:
        resp = _post(f"{base_url}/runs", payload)
    except Exception as e:
        print(f"  FAILED to trigger: {e}")
        return {"label": label, "subject": subject, "error": str(e)}

    run_id = resp["run_id"]
    print(f"  Run #{run_id} started. Polling every {poll_interval}s (max {timeout}s)...")

    t0 = time.monotonic()
    deadline = t0 + timeout
    last_seen = 0

    while time.monotonic() < deadline:
        try:
            state = _get(f"{base_url}/runs/{run_id}")
        except Exception:
            time.sleep(poll_interval)
            continue

        status = state.get("status", "?")
        tcs = state.get("tool_calls", [])
        if len(tcs) > last_seen:
            new = tcs[last_seen:]
            for tc in new:
                tool = tc.get("tool", "?")
                detail = ""
                args = tc.get("args", {})
                if isinstance(args, dict):
                    detail = args.get("query", args.get("url", args.get("title", "")))
                print(f"    {tool:<18} {str(detail)[:60]}")
            last_seen = len(tcs)

        if status in ("done", "failed", "timeout", "cancelled"):
            elapsed = round(time.monotonic() - t0, 1)
            output = state.get("output", {})
            error = state.get("error")
            print(f"\n  Status : {status}  ({elapsed}s)")
            if error:
                print(f"  Error  : {error}")

            fetches = sum(1 for tc in tcs if tc.get("tool") == "fetch_page")
            report = _analyse_report(vault_path, subject)

            result = {
                "label": label,
                "subject": subject,
                "status": status,
                "time_s": elapsed,
                "tool_calls": len(tcs),
                "fetches": fetches,
                "findings_saved": output.get("findings_saved", output.get("documents_written", 0)),
                **report,
                "manual_quality": "(fill in 1–5)",
            }
            print(f"  Tool calls: {result['tool_calls']}  Fetches: {result['fetches']}  "
                  f"Findings: {result['findings_saved']}")
            print(f"  Citations : {result['citations']}  Sources: {result['sources']}  "
                  f"LinkedIn: {'✓' if result['linkedin_hit'] else '✗'}  "
                  f"Sections: {result['sections']}  Words: {result['words']}")
            if report["report_path"]:
                print(f"  Report    : {report['report_path']}")
            return result

        time.sleep(poll_interval)

    elapsed = round(time.monotonic() - t0, 1)
    print(f"\n  ⚠ Timed out after {elapsed}s")
    return {"label": label, "subject": subject, "status": "timeout", "time_s": elapsed, "error": "client timeout"}


# ---------------------------------------------------------------------------
# Table rendering
# ---------------------------------------------------------------------------

def _render_table(results: list[dict]) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = [
        f"# Agent Service Benchmark — {ts}",
        "",
        "4-way comparison: Pi vs Hermes × plain fetch vs browser-rendered (Jina.ai).",
        "Same subject, same task for all runs. Isolated subjects prevent KB cross-contamination.",
        "",
        "| Candidate | Browser | Time | Calls | Fetches | Findings | Citations | Sources | LinkedIn | Sections | Words | Manual |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    rows = []
    for r in results:
        if "error" in r and "time_s" not in r:
            rows.append(f"| {r['label']} | — | ERROR | — | — | — | — | — | — | — | — | — |")
            continue
        candidate = r["label"].split(" / ")[0] if " / " in r["label"] else r["label"]
        browser = "✓ Jina" if "browser" in r["label"].lower() else "✗ plain"
        rows.append(
            f"| {candidate} "
            f"| {browser} "
            f"| {r.get('time_s', '?')}s "
            f"| {r.get('tool_calls', '?')} "
            f"| {r.get('fetches', '?')} "
            f"| {r.get('findings_saved', '?')} "
            f"| {r.get('citations', '?')} "
            f"| {r.get('sources', '?')} "
            f"| {'✓' if r.get('linkedin_hit') else '✗'} "
            f"| {r.get('sections', '?')} "
            f"| {r.get('words', '?')} "
            f"| {r.get('manual_quality', '—')} |"
        )
    guide = [
        "",
        "**Column guide:**",
        "- **Browser** — fetch_page backend: plain httpx vs Jina.ai (JS-rendered markdown)",
        "- **Calls** — total tool calls (search + fetch + write)",
        "- **Fetches** — number of fetch_page calls (page-reading depth)",
        "- **Citations** — [N] inline citation count in final report",
        "- **Sources** — unique domains in ## Sources section",
        "- **LinkedIn** — whether LinkedIn profile data appeared in the report",
        "- **Sections** — H2 headings in report body (coverage breadth)",
        "- **Words** — final report word count",
        "- **Manual** — fill in 1–5 after reading each report (1=poor, 5=excellent)",
        "",
        "## Report paths",
    ]
    paths = []
    for r in results:
        path = r.get("report_path") or "(not written)"
        paths.append(f"- **{r['label']}**: `{path}`")

    return "\n".join(header + rows + guide + paths)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 12.5 agent service 4-way benchmark")
    parser.add_argument("--subject", default="Fresenius SE",
                        help="Research subject (default: Fresenius SE)")
    parser.add_argument("--hermes-port", type=int, default=8002)
    parser.add_argument("--pi-port", type=int, default=8001)
    parser.add_argument("--timeout", type=int, default=600,
                        help="Max seconds per run (default: 600)")
    parser.add_argument("--poll", type=float, default=3.0,
                        help="Poll interval in seconds (default: 3)")
    parser.add_argument("--vault", default="",
                        help="Path to north-info vault (auto-detected from config.yaml if omitted)")
    args = parser.parse_args()

    # Resolve vault path
    vault_path: Path
    if args.vault:
        vault_path = Path(args.vault)
    else:
        cfg_path = Path(__file__).parent.parent / "config.yaml"
        try:
            import yaml  # type: ignore
            cfg = yaml.safe_load(cfg_path.read_text()) or {}
            vault_path = Path(cfg.get("vault_path", "north-info"))
        except Exception:
            vault_path = Path(__file__).parent.parent / "north-info"

    hermes_url = f"http://localhost:{args.hermes_port}"
    pi_url = f"http://localhost:{args.pi_port}"

    # Verify both services are up
    for name, url in [("Hermes", hermes_url), ("Pi", pi_url)]:
        try:
            _get(f"{url}/health")
            print(f"✓ {name} at {url}")
        except Exception as e:
            print(f"✗ {name} at {url} — {e}", file=sys.stderr)
            sys.exit(1)

    runs = [
        ("Hermes / plain",   args.subject, hermes_url, False),
        ("Pi / plain",       args.subject, pi_url,     False),
        ("Hermes / browser", args.subject, hermes_url, True),
        ("Pi / browser",     args.subject, pi_url,     True),
    ]

    print(f"\nBenchmark subject : {args.subject}")
    print(f"Vault             : {vault_path}")
    print(f"Timeout per run   : {args.timeout}s")
    print(f"Runs              : {len(runs)}")

    results = []
    for label, subject, base_url, use_browser in runs:
        result = run_one(
            label=label,
            subject=subject,
            base_url=base_url,
            use_browser_fetch=use_browser,
            poll_interval=args.poll,
            timeout=args.timeout,
            vault_path=vault_path,
        )
        results.append(result)

    table = _render_table(results)

    print(f"\n{'=' * 60}")
    print("RESULTS TABLE")
    print("=" * 60)
    print(table)

    out_dir = Path(__file__).parent.parent / "data" / "benchmarks"
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r'[^a-z0-9]+', '-', args.subject.lower()).strip('-')
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    md_path = out_dir / f"{today}-agent-bench-{slug}.md"
    json_path = out_dir / f"{today}-agent-bench-{slug}.json"

    md_path.write_text(table, encoding="utf-8")
    json_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    print(f"\nSaved: {md_path}")
    print(f"JSON : {json_path}")
    print("\nNext: open each report path above, read carefully, fill in Manual Quality column.")


if __name__ == "__main__":
    main()
