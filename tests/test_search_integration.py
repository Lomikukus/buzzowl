"""
tests/test_search_integration.py — Search integration tests.

Indexes fixture session data into the whisper_test DB (no Ollama — all
embeddings are a deterministic FAKE_EMBEDDING) and asserts that
hybrid_search returns the expected results, stores embeddings, and handles
typos via trigram matching.

Requirements:
    Docker running with the postgres container:
        docker compose up -d
    Schema applied once:
        bash scripts/db_init.sh   (or let setup_test_db fixture do it)

Run: pytest tests/test_search_integration.py -v
"""

import asyncio
import pathlib
from unittest.mock import MagicMock, patch

import asyncpg
import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_DB_URL = "postgresql://whisper:whisper@localhost:5432/whisper_test"
ADMIN_DB_URL = "postgresql://whisper:whisper@localhost:5432/postgres"
SCHEMA_PATH = pathlib.Path(__file__).parent.parent / "schema.sql"

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
FIXTURE_TRANSCRIPT = FIXTURES / "transcripts" / "acme_gmbh.txt"
FIXTURE_SUMMARY = FIXTURES / "summaries" / "acme_gmbh.md"

EMBED_DIM = 768
FAKE_EMBEDDING: list[float] = [0.5] * EMBED_DIM

SESSION_DOC_ID = "search-test-20260101-120000"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_ollama(embedding: list[float]) -> MagicMock:
    m = MagicMock()
    m.raise_for_status = MagicMock()
    m.json.return_value = {"embedding": embedding}
    return m


def _empty_ollama() -> MagicMock:
    m = MagicMock()
    m.raise_for_status = MagicMock()
    m.json.return_value = {"embedding": []}
    return m


# ---------------------------------------------------------------------------
# Session-scoped: create whisper_test DB and apply schema once
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def setup_test_db():
    async def _create() -> None:
        conn = await asyncpg.connect(ADMIN_DB_URL)
        try:
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = 'whisper_test'"
            )
            if not exists:
                await conn.execute("CREATE DATABASE whisper_test")
        finally:
            await conn.close()

    async def _schema() -> None:
        conn = await asyncpg.connect(TEST_DB_URL)
        try:
            await conn.execute(
                """
                DROP TABLE IF EXISTS
                    document_links, heartbeats, documents, agent_runs,
                    contacts, clients, user_sessions, users, orgs
                CASCADE;
                DROP EXTENSION IF EXISTS vector CASCADE;
                """
            )
            await conn.execute(SCHEMA_PATH.read_text())
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_create())
        loop.run_until_complete(_schema())
    finally:
        loop.close()

    yield


# ---------------------------------------------------------------------------
# Function-scoped: initialised db module + clean tables
# ---------------------------------------------------------------------------

@pytest.fixture()
async def db(setup_test_db):
    """Yield an initialised db module pointing at whisper_test, tables clean."""
    import db as _db

    _db._pool = None
    _db._embed_model = "nomic-embed-text"
    _db._embed_dim = EMBED_DIM
    _db._main_loop = None

    await _db.init_db(TEST_DB_URL, "nomic-embed-text", EMBED_DIM)

    # Truncate all data tables before the test
    async with _db._pool.acquire() as conn:
        await conn.execute(
            """
            TRUNCATE TABLE
                document_links, heartbeats, documents, agent_runs,
                contacts, clients, user_sessions, users, orgs
            RESTART IDENTITY CASCADE;
            """
        )

    yield _db
    await _db.close_db()


# ---------------------------------------------------------------------------
# Function-scoped: seeded org + indexed fixture data
# ---------------------------------------------------------------------------

@pytest.fixture()
async def indexed_session(db):
    """
    Create an org, index the Acme GmbH fixture transcript as a meeting
    document, upsert client and 2 contacts, link everything.

    Returns {"org_id", "doc_id", "client_id", "contact_id"}.
    """
    org = await db.create_org("Search Test Corp", "search-test")
    oid = org["id"]

    transcript = FIXTURE_TRANSCRIPT.read_text(encoding="utf-8")
    summary = FIXTURE_SUMMARY.read_text(encoding="utf-8")

    doc_id = await db.index_document(
        org_id=oid,
        doc_id=SESSION_DOC_ID,
        doc_type="meeting",
        title="Erstkontakt mit Acme GmbH",
        content=f"{transcript}\n\n{summary}",
        metadata={"date": "2026-01-01", "language": "de"},
        embedding=FAKE_EMBEDDING,
    )

    client_id = await db.upsert_client(
        oid, "Acme GmbH", {"industry": "Software"}, FAKE_EMBEDDING, "2026-01-01"
    )
    await db.link_document(doc_id, "client", client_id)

    contact_id = await db.upsert_contact(
        oid, "Marcus Weber",
        {"role": "Kunde", "company": "Acme GmbH"},
        FAKE_EMBEDDING,
        client_id=client_id,
        date_str="2026-01-01",
    )
    await db.link_document(doc_id, "contact", contact_id)

    return {"org_id": oid, "doc_id": doc_id, "client_id": client_id, "contact_id": contact_id}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPromoteToSearchable:
    """Indexed fixture data is found by hybrid_search."""

    async def test_meeting_document_found_by_client_name(self, db, indexed_session):
        oid = indexed_session["org_id"]
        with patch("db.requests.post", return_value=_fake_ollama(FAKE_EMBEDDING)):
            results = await db.hybrid_search(oid, "Acme")

        assert len(results) > 0
        titles = [r["display_title"] for r in results]
        assert any("Acme" in t for t in titles), (
            f"'Acme' not in any result title: {titles}"
        )
        assert any(r["combined_score"] > 0 for r in results)

    async def test_meeting_document_has_correct_subtype(self, db, indexed_session):
        oid = indexed_session["org_id"]
        with patch("db.requests.post", return_value=_fake_ollama(FAKE_EMBEDDING)):
            results = await db.hybrid_search(oid, "Erstkontakt Acme", doc_type="meeting")

        # Any document rows in results must have subtype="meeting"
        assert results
        doc_rows = [r for r in results if r["type"] == "document"]
        assert doc_rows, "Expected at least one document result"
        for r in doc_rows:
            assert r["subtype"] == "meeting"

    async def test_result_has_required_fields(self, db, indexed_session):
        oid = indexed_session["org_id"]
        with patch("db.requests.post", return_value=_fake_ollama(FAKE_EMBEDDING)):
            results = await db.hybrid_search(oid, "Acme")

        assert results
        r = results[0]
        for field in ("type", "id", "display_title", "snippet", "vec_score",
                      "fts_score", "trgm_score", "combined_score"):
            assert field in r, f"Missing field '{field}' in result"


class TestClientSearchableAfterPromotion:
    """Contacts indexed during promotion are searchable by name."""

    async def test_contact_found_by_full_name(self, db, indexed_session):
        oid = indexed_session["org_id"]
        with patch("db.requests.post", return_value=_fake_ollama(FAKE_EMBEDDING)):
            results = await db.hybrid_search(oid, "Marcus Weber")

        assert len(results) > 0
        titles = [r["display_title"] for r in results]
        assert any("Marcus" in t or "Weber" in t for t in titles), (
            f"'Marcus Weber' not in any result title: {titles}"
        )

    async def test_client_found_by_exact_name(self, db, indexed_session):
        oid = indexed_session["org_id"]
        with patch("db.requests.post", return_value=_fake_ollama(FAKE_EMBEDDING)):
            results = await db.hybrid_search(oid, "Acme GmbH")

        assert len(results) > 0
        titles = {r["display_title"] for r in results}
        assert "Acme GmbH" in titles


class TestEmbeddingStoredAfterIndexing:
    """Documents and entities indexed with a non-empty embedding persist it in the DB."""

    async def test_document_embedding_is_non_null(self, db, indexed_session):
        async with db._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT embedding IS NOT NULL AS has_embedding FROM documents WHERE doc_id = $1",
                SESSION_DOC_ID,
            )
        assert row is not None, "Document row not found in DB"
        assert row["has_embedding"] is True

    async def test_client_embedding_is_non_null(self, db, indexed_session):
        async with db._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT embedding IS NOT NULL AS has_embedding FROM clients "
                "WHERE org_id = $1 AND name = 'Acme GmbH'",
                indexed_session["org_id"],
            )
        assert row is not None
        assert row["has_embedding"] is True

    async def test_contact_embedding_is_non_null(self, db, indexed_session):
        async with db._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT embedding IS NOT NULL AS has_embedding FROM contacts "
                "WHERE org_id = $1 AND name = 'Marcus Weber'",
                indexed_session["org_id"],
            )
        assert row is not None
        assert row["has_embedding"] is True


class TestFuzzySearch:
    """Trigram search surfaces results despite typos."""

    async def test_typo_in_client_name_returns_result(self, db, indexed_session):
        """'Acme GmbbH' (double-b typo) should match 'Acme GmbH' via trigram."""
        oid = indexed_session["org_id"]
        with patch("db.requests.post", return_value=_fake_ollama(FAKE_EMBEDDING)):
            results = await db.hybrid_search(oid, "Acme GmbbH")

        assert len(results) > 0
        client_results = [r for r in results if r["display_title"] == "Acme GmbH"]
        assert client_results, (
            f"'Acme GmbH' not found for typo query — got: "
            f"{[r['display_title'] for r in results]}"
        )
        assert client_results[0]["trgm_score"] > 0

    async def test_partial_name_returns_result(self, db, indexed_session):
        """Searching 'Marcus' (first name only) should still surface the contact."""
        oid = indexed_session["org_id"]
        with patch("db.requests.post", return_value=_fake_ollama(FAKE_EMBEDDING)):
            results = await db.hybrid_search(oid, "Marcus")

        assert len(results) > 0
        titles = [r["display_title"] for r in results]
        assert any("Marcus" in t for t in titles)
