#!/usr/bin/env bash
# tests/run_integration.sh — Structured integration test runner.
#
# Usage:
#   bash tests/run_integration.sh           # all suites
#   bash tests/run_integration.sh --fast    # skip ollama + slow suites
#
# Exits 0 if every enabled suite passes, 1 if any fail.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

FAST=0
[[ "${1:-}" == "--fast" ]] && FAST=1

cd "$PROJECT_ROOT"
source .venv/bin/activate 2>/dev/null || true

PASS=()
FAIL=()

run_suite() {
    local name="$1"
    local marker="$2"
    local path="$3"

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Suite: $name"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    if pytest $marker "$path" -v --tb=short 2>&1; then
        PASS+=("$name")
    else
        FAIL+=("$name")
    fi
}

# --- Always-on suites (no external deps) ---
run_suite "Pipeline"   "-m 'not ollama and not slow'"  "tests/test_pipeline.py"
run_suite "Search"     "-m 'not ollama and not slow'"  "tests/test_search_integration.py"
run_suite "DB"         "-m 'not ollama and not slow'"  "tests/test_db.py"
run_suite "API"        "-m 'not ollama and not slow'"  "tests/test_api.py"
run_suite "Agents"     "-m 'not ollama and not slow'"  "tests/test_agents.py"

# --- Ollama-dependent suites ---
if [[ $FAST -eq 0 ]]; then
    run_suite "Entity Extraction (Ollama)" "-m ollama" "tests/test_entity_extraction.py"
fi

# --- Slow suites (WhisperX model load) ---
if [[ $FAST -eq 0 ]]; then
    run_suite "Transcription (slow)"  "-m slow" "tests/test_transcription.py"
fi

# --- Summary ---
echo ""
echo "══════════════════════════════════════════════════"
echo "  RESULTS"
echo "══════════════════════════════════════════════════"

for s in "${PASS[@]:-}"; do
    [[ -n "$s" ]] && echo "  ✓  $s"
done

for s in "${FAIL[@]:-}"; do
    [[ -n "$s" ]] && echo "  ✗  $s"
done

echo ""
if [[ ${#FAIL[@]} -gt 0 ]]; then
    echo "  ${#FAIL[@]} suite(s) FAILED — see output above."
    exit 1
else
    echo "  All ${#PASS[@]} suite(s) passed."
    exit 0
fi
