"""
tests/conftest.py — shared fixtures for the Buzzowl test suite.

Fixtures defined here are available to all test modules automatically.
"""

import json
import shutil
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_SESSION_DIR = FIXTURES / "sessions" / "fixture_session"
FIXTURE_TRANSCRIPT = FIXTURES / "transcripts" / "acme_gmbh.txt"
FIXTURE_SUMMARY = FIXTURES / "summaries" / "acme_gmbh.md"
FIXTURE_SESSION_ID = "20260101-120000"


# ---------------------------------------------------------------------------
# Vault fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def temp_vault_dir(tmp_path_factory) -> Path:
    """Temporary vault directory with all expected subdirectories pre-created."""
    vault = tmp_path_factory.mktemp("vault")
    for sub in ("raw", "clients", "people", "research", "briefs", "_templates"):
        (vault / sub).mkdir()
    return vault


# ---------------------------------------------------------------------------
# Staged session fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def staged_session(tmp_path) -> tuple[Path, str]:
    """Seed the Acme GmbH fixture session into a temp data/ tree.

    Returns (base_dir, session_id).  The caller should patch server.BASE_DIR
    with base_dir so that server functions read/write from the temp tree.
    """
    session_id = FIXTURE_SESSION_ID

    raw_dir = tmp_path / "data" / "raw" / session_id
    staged_dir = tmp_path / "data" / "staged" / session_id
    sorted_dir = tmp_path / "data" / "sorted"
    for d in (raw_dir, staged_dir, sorted_dir):
        d.mkdir(parents=True)

    shutil.copy(FIXTURE_TRANSCRIPT, raw_dir / "transcript.txt")
    shutil.copy(FIXTURE_SUMMARY, staged_dir / "summary.md")

    meta = json.loads((FIXTURE_SESSION_DIR / "metadata.json").read_text())
    meta["session_id"] = session_id
    (staged_dir / "metadata.json").write_text(json.dumps(meta, indent=2))

    return tmp_path, session_id


# ---------------------------------------------------------------------------
# DB org fixture (stub — org_id 1 used in routing tests that don't hit a real DB)
# ---------------------------------------------------------------------------

@pytest.fixture()
def test_db_org_id() -> int:
    """Returns a stub org_id (1) for tests that need the parameter but don't use a real DB."""
    return 1


# ---------------------------------------------------------------------------
# TTL micro-cache isolation — endpoint responses must never leak across tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_ttl_cache():
    try:
        import context
        context.cache_clear()
    except Exception:
        pass
    yield
