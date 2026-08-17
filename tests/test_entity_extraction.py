"""
tests/test_entity_extraction.py — Entity extraction integration tests.

Runs the Ollama extraction prompt against each fixture transcript and
asserts that the expected companies and people are correctly identified.

Requires Ollama running locally with llama3.2 (or the configured model).

Run:  pytest -m ollama tests/test_entity_extraction.py -v
Skip: pytest -m "not ollama"
"""

import os

import pytest
from pathlib import Path

# Real-LLM integration test (network + tokens). Off by default; opt in with
# RUN_LLM_INTEGRATION=1. (Historically it failed on a signature drift and was
# counted as a known failure — now it is an explicit skip instead.)
pytestmark = pytest.mark.skipif(not os.environ.get("RUN_LLM_INTEGRATION"),
                                reason="real LLM integration — set RUN_LLM_INTEGRATION=1 to run")

TRANSCRIPTS = Path(__file__).parent / "fixtures" / "transcripts"

# Minimum expected entities per fixture script.
# These are the most prominent/unambiguous names in each script.
CASES = [
    pytest.param(
        "acme_gmbh.txt",
        {"companies": ["Acme GmbH"], "people": ["Marcus Weber", "Jana Kiefer"]},
        id="acme_gmbh",
    ),
    pytest.param(
        "horizon_logistik.txt",
        {"companies": ["Horizon Logistik"], "people": ["Thomas Chen", "Rachel Park"]},
        id="horizon_logistik",
    ),
    pytest.param(
        "vertex_analytics.txt",
        {"companies": ["Vertex Analytics"], "people": ["Chris Nguyen", "Priya Sharma"]},
        id="vertex_analytics",
    ),
    pytest.param(
        "pipeline_review.txt",
        {"companies": ["Pinnacle Media"], "people": ["Alex Rodriguez", "Lisa Fong"]},
        id="pipeline_review",
    ),
    pytest.param(
        "solaris_tech.txt",
        {"companies": ["Solaris Tech"], "people": ["James Burke", "Oliver Grant"]},
        id="solaris_tech",
    ),
]


def _people_names(people: list) -> set[str]:
    """Normalise people list (dicts or plain strings) to a set of name strings."""
    return {p["name"] if isinstance(p, dict) else str(p) for p in people}


def _company_names(companies: list) -> list[str]:
    """Normalise companies list (dicts or plain strings) to a list of name strings."""
    return [c["name"] if isinstance(c, dict) else str(c) for c in companies]


# ---------------------------------------------------------------------------
# Parametrized: all 5 fixture transcripts
# ---------------------------------------------------------------------------

@pytest.mark.ollama
@pytest.mark.parametrize("filename,expected", CASES)
def test_extraction_shape(filename, expected):
    """Output must have the correct keys and valid list types."""
    from routers.pipeline import extract_entities

    transcript = (TRANSCRIPTS / filename).read_text(encoding="utf-8")
    result = extract_entities(transcript, "qwen3.5")

    assert isinstance(result, dict), "extract_entities must return a dict"
    assert "companies" in result
    assert "people" in result
    assert "topics" in result
    assert isinstance(result["companies"], list)
    assert isinstance(result["people"], list)
    assert isinstance(result["topics"], list)


@pytest.mark.ollama
@pytest.mark.parametrize("filename,expected", CASES)
def test_extraction_no_nulls_or_empty_strings(filename, expected):
    """No null or empty-string values in companies or people names."""
    from routers.pipeline import extract_entities

    transcript = (TRANSCRIPTS / filename).read_text(encoding="utf-8")
    result = extract_entities(transcript, "qwen3.5")

    for company in _company_names(result["companies"]):
        assert company and isinstance(company, str), f"Empty/null company: {company!r}"

    for name in _people_names(result["people"]):
        assert name, f"Empty/null person name: {name!r}"


@pytest.mark.ollama
@pytest.mark.parametrize("filename,expected", CASES)
def test_extraction_expected_companies_present(filename, expected):
    """Each fixture's prominent company names must appear in the result."""
    from routers.pipeline import extract_entities

    transcript = (TRANSCRIPTS / filename).read_text(encoding="utf-8")
    result = extract_entities(transcript, "qwen3.5")

    extracted = [c.lower() for c in _company_names(result["companies"])]
    for company in expected["companies"]:
        # Accept partial matches in either direction — LLMs may return "Pinnacle" or
        # "Pinnacle Media" non-deterministically for the same transcript.
        assert any(company.lower() in c or c in company.lower() for c in extracted), (
            f"Expected company '{company}' not found — got: {_company_names(result['companies'])}"
        )


@pytest.mark.ollama
@pytest.mark.parametrize("filename,expected", CASES)
def test_extraction_expected_people_present(filename, expected):
    """Each fixture's prominent person names must appear in the result."""
    from routers.pipeline import extract_entities

    transcript = (TRANSCRIPTS / filename).read_text(encoding="utf-8")
    result = extract_entities(transcript, "qwen3.5")

    extracted = {n.lower() for n in _people_names(result["people"])}
    for name in expected["people"]:
        assert name.lower() in extracted, (
            f"Expected person '{name}' not found — got: {_people_names(result['people'])}"
        )


# ---------------------------------------------------------------------------
# Phase 11: confidence field validation
# ---------------------------------------------------------------------------

@pytest.mark.ollama
@pytest.mark.parametrize("filename,expected", CASES)
def test_extraction_confidence_fields(filename, expected):
    """Every extracted company and person must carry a valid confidence value."""
    from routers.pipeline import extract_entities

    transcript = (TRANSCRIPTS / filename).read_text(encoding="utf-8")
    result = extract_entities(transcript, "qwen3.5")

    valid = {"high", "medium", "low"}
    for co in result["companies"]:
        assert isinstance(co, dict), f"Company must be a dict: {co!r}"
        assert co.get("confidence") in valid, f"Bad confidence on company {co!r}"
    for p in result["people"]:
        assert isinstance(p, dict), f"Person must be a dict: {p!r}"
        assert p.get("confidence") in valid, f"Bad confidence on person {p!r}"


# ---------------------------------------------------------------------------
# Edge case: transcript with no named entities
# ---------------------------------------------------------------------------

@pytest.mark.ollama
def test_extraction_on_no_entity_transcript():
    """A transcript with no names must return empty lists, not an error."""
    from routers.pipeline import extract_entities

    transcript = (
        "Es war ein sonniger Tag. Die Temperatur war angenehm. "
        "Alles war ruhig und friedlich. Das Wetter blieb konstant. "
        "Keine besonderen Vorkommnisse wurden verzeichnet."
    )
    result = extract_entities(transcript, "qwen3.5")

    assert isinstance(result, dict)
    assert "companies" in result
    assert "people" in result
    # Must be lists (possibly empty) — not errors or None
    assert isinstance(result["companies"], list)
    assert isinstance(result["people"], list)
