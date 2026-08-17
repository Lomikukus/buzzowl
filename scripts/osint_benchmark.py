"""
Phase 6 model evaluation — OSINT synthesis benchmark.

Tests the core OSINT task: given raw web snippets about a company,
produce a structured research document suitable for a sales KB.

Run: python scripts/osint_benchmark.py
"""
# PHASE20: superseded — this benchmark was written for Phase 6 OSINT agent development.
# Replaced by scripts/three_way_benchmark.py and scripts/agent_benchmark.py.
# Safe to archive; do not delete until confirmed no longer referenced.
import json
import time
import textwrap
import requests

OLLAMA = "http://localhost:11434"

# Realistic web snippets simulating DuckDuckGo results for an OSINT query.
# Based on a fictional company ("Meridian Software") so no real data leakage.
FAKE_SNIPPETS = """
Search results for "Meridian Software company overview":
- Meridian Software GmbH: Enterprise workflow automation platform for mid-market manufacturing companies. Founded 2017 in Stuttgart. Series B funded.
- Meridian Software raises €18M Series B: The Stuttgart-based B2B SaaS company closed an €18M round led by HV Capital in March 2024. Plans to expand to Austria and Switzerland.
- Meridian Software CEO Thomas Bauer on digital transformation: "We help factories cut manual reporting by 70%" — interview with Handelsblatt, Jan 2025.
- Meridian Software LinkedIn: 180 employees. Offices in Stuttgart and Vienna. Key products: MeridianFlow (workflow), MeridianReports (analytics).
- Trustpilot reviews Meridian Software: Customers praise ease of integration with SAP. Some note steep learning curve for non-technical users.
- Crunchbase Meridian Software: Founded 2017. Seed 2019 (€1.5M, btov Partners). Series A 2022 (€6M, Earlybird). Series B 2024 (€18M, HV Capital). Total raised: ~€25.5M.

Top page content (from meridian-software.de):
Meridian Software GmbH is an enterprise software company specialising in workflow automation for manufacturing and logistics. Our platform, MeridianFlow, integrates with ERP systems including SAP, Oracle, and Microsoft Dynamics. Customers include Bosch, Daimler Truck, and several Mittelstand manufacturers across DACH. Our analytics module, MeridianReports, provides real-time operational dashboards. CEO: Thomas Bauer (co-founder). CTO: Dr. Anna Schreiber (ex-SAP). Head of Sales: Markus Held. The company is headquartered at Königstraße 40, Stuttgart.
"""

OSINT_PROMPT = f"""You are a B2B sales intelligence assistant. Based ONLY on the following web data, produce a structured OSINT report for "Meridian Software".

{FAKE_SNIPPETS}

Respond in markdown with exactly these sections:

## Overview
What the company does and who they serve (2-3 sentences).

## Leadership
Key named individuals and their roles.

## Funding & Growth
Funding history and any growth signals.

## Products
Their main products/offerings.

## Sales Signals
3-5 bullet points most actionable for a sales team approaching this company as a potential client or partner.

Only include facts supported by the data above. Do not invent information. Be concise."""


def call_generate(model: str, prompt: str, timeout: int = 120) -> tuple[str, float]:
    """Call /api/generate — used for models that don't support think param."""
    t0 = time.time()
    resp = requests.post(
        f"{OLLAMA}/api/generate",
        json={"model": model, "prompt": prompt, "stream": False},
        timeout=timeout,
    )
    resp.raise_for_status()
    elapsed = time.time() - t0
    return resp.json().get("response", "").strip(), elapsed


def call_chat(model: str, prompt: str, think: bool, timeout: int = 300) -> tuple[str, float]:
    """Call /api/chat with think param — for qwen3.5."""
    t0 = time.time()
    resp = requests.post(
        f"{OLLAMA}/api/chat",
        json={
            "model": model,
            "think": think,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    elapsed = time.time() - t0
    content = resp.json().get("message", {}).get("content", "").strip()
    return content, elapsed


def score_output(text: str) -> dict:
    """Heuristic quality checks on the output."""
    sections = ["## Overview", "## Leadership", "## Funding", "## Products", "## Sales Signals"]
    expected_facts = ["Thomas Bauer", "€18M", "HV Capital", "MeridianFlow", "SAP", "Stuttgart", "Series B"]

    present_sections = sum(1 for s in sections if s in text)
    present_facts = sum(1 for f in expected_facts if f in text)
    word_count = len(text.split())

    return {
        "sections": f"{present_sections}/{len(sections)}",
        "facts":    f"{present_facts}/{len(expected_facts)}",
        "words":    word_count,
    }


def divider(title: str):
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


def print_result(label: str, text: str, elapsed: float):
    scores = score_output(text)
    print(f"\n--- {label} ---")
    print(f"Time:     {elapsed:.1f}s")
    print(f"Sections: {scores['sections']}  |  Facts: {scores['facts']}  |  Words: {scores['words']}")
    print("\nOutput preview (first 800 chars):")
    print(textwrap.indent(text[:800], "  "))
    if len(text) > 800:
        print(f"  ... ({len(text) - 800} more chars)")
    return {"label": label, "elapsed": elapsed, **scores, "full_output": text}


results = []

# ── 1. qwen3.5 — think: False (standard extraction mode) ──────────────────────
divider("TEST 1: qwen3.5  think=False  (/api/chat)")
print("Running...")
try:
    text, elapsed = call_chat("qwen3.5", OSINT_PROMPT, think=False, timeout=120)
    results.append(print_result("qwen3.5 think=False", text, elapsed))
except Exception as e:
    print(f"FAILED: {e}")

# ── 2. qwen3.5 — think: True (extended reasoning) ─────────────────────────────
divider("TEST 2: qwen3.5  think=True   (/api/chat)")
print("Running (may take 60–180s)...")
try:
    text, elapsed = call_chat("qwen3.5", OSINT_PROMPT, think=True, timeout=300)
    results.append(print_result("qwen3.5 think=True", text, elapsed))
except Exception as e:
    print(f"FAILED: {e}")

# ── 3. llama3.2 — baseline ────────────────────────────────────────────────────
divider("TEST 3: llama3.2  (/api/generate)")
print("Running...")
try:
    text, elapsed = call_generate("llama3.2", OSINT_PROMPT, timeout=120)
    results.append(print_result("llama3.2", text, elapsed))
except Exception as e:
    print(f"FAILED: {e}")

# ── Summary table ─────────────────────────────────────────────────────────────
divider("SUMMARY")
print(f"{'Model':<28} {'Time':>7}  {'Sections':>10}  {'Facts':>8}  {'Words':>7}")
print("-" * 70)
for r in results:
    print(f"{r['label']:<28} {r['elapsed']:>6.1f}s  {r['sections']:>10}  {r['facts']:>8}  {r['words']:>7}")

print("\nVerdict guidance:")
print("  - think=False should be ~15-30s and match think=True quality for extraction tasks")
print("  - think=True adds latency; only worth it if Facts score improves meaningfully")
print("  - llama3.2 baseline: fast but smaller — check if sections/facts match")

# Save full outputs for manual review
out_path = "scripts/osint_benchmark_results.json"
with open(out_path, "w") as f:
    json.dump([{k: v for k, v in r.items()} for r in results], f, indent=2)
print(f"\nFull outputs saved to {out_path}")
