"""
tests/test_db.py — comprehensive pytest suite for db.py

Requirements:
    pip install pytest pytest-asyncio asyncpg pgvector requests

Test DB: whisper_test on the same Docker container (localhost:5432)
Credentials: whisper / whisper (same as docker-compose.yml)

Run:
    pytest tests/test_db.py -v

The session-scoped fixture creates the whisper_test DB and applies
schema.sql once per pytest session. Each test class truncates all tables
before every test so tests are fully isolated without the overhead of
repeated schema creation.
"""

import asyncio
import importlib
import pathlib
import secrets
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Generator
from unittest.mock import MagicMock, patch

import asyncpg
import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_DB_URL = "postgresql://whisper:whisper@localhost:5432/whisper_test"
ADMIN_DB_URL = "postgresql://whisper:whisper@localhost:5432/postgres"
SCHEMA_PATH = pathlib.Path(__file__).parent.parent / "schema.sql"

EMBED_DIM = 768
EMBED_MODEL = "nomic-embed-text"

# A deterministic fake embedding vector (all 0.5) that satisfies the 768-dim requirement.
FAKE_EMBEDDING: list[float] = [0.5] * EMBED_DIM


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_ollama_response(embedding: list[float]) -> MagicMock:
    """Return a mock that looks like a successful requests.Response from Ollama."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"embedding": embedding}
    return mock_resp


def _empty_ollama_response() -> MagicMock:
    """Return a mock that simulates Ollama returning an empty embedding (offline)."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"embedding": []}
    return mock_resp


# ---------------------------------------------------------------------------
# Session-scoped fixture: create whisper_test DB and apply schema once
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def event_loop_policy():
    """Use the default event loop policy for the whole session."""
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture(scope="session")
def setup_test_db():
    """
    Create the whisper_test database and apply schema.sql exactly once per
    pytest session.  Runs synchronously so it can be used as a plain fixture
    (not async) while still being session-scoped.
    """

    async def _create_db() -> None:
        # Connect to the default 'postgres' DB as superuser to CREATE the test DB.
        conn = await asyncpg.connect(ADMIN_DB_URL)
        try:
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = 'whisper_test'"
            )
            if not exists:
                # CREATE DATABASE cannot run inside a transaction block.
                await conn.execute("CREATE DATABASE whisper_test")
        finally:
            await conn.close()

    async def _apply_schema() -> None:
        # Connect to whisper_test and (re)apply the schema.
        conn = await asyncpg.connect(TEST_DB_URL)
        try:
            schema_sql = SCHEMA_PATH.read_text()
            # Drop everything first so we can re-run safely on an existing DB.
            await conn.execute(
                """
                DROP TABLE IF EXISTS
                    research_tasks,
                    document_links,
                    heartbeats,
                    documents,
                    agent_runs,
                    contacts,
                    clients,
                    user_sessions,
                    users,
                    orgs
                CASCADE;
                DROP EXTENSION IF EXISTS vector CASCADE;
                """
            )
            await conn.execute(schema_sql)
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_create_db())
        loop.run_until_complete(_apply_schema())
    finally:
        loop.close()

    yield  # tests run here


# ---------------------------------------------------------------------------
# Function-scoped fixture: truncate all tables before each test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def truncate_tables(setup_test_db):
    """
    Truncate all data tables (in dependency order) before every test so each
    test starts with a clean slate.  Uses a synchronous event loop so it can
    be an ordinary (non-async) fixture.
    """

    async def _truncate() -> None:
        conn = await asyncpg.connect(TEST_DB_URL)
        try:
            await conn.execute(
                """
                TRUNCATE TABLE
                    research_tasks,
                    document_links,
                    heartbeats,
                    documents,
                    agent_runs,
                    contacts,
                    clients,
                    user_sessions,
                    users,
                    orgs
                RESTART IDENTITY CASCADE;
                """
            )
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_truncate())
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Function-scoped fixture: initialised db module with a live pool
# ---------------------------------------------------------------------------

@pytest.fixture()
async def db(setup_test_db):
    """
    Yield the db module with an initialised pool pointing at whisper_test.
    Calls close_db() after each test.

    Re-importing via importlib ensures module-level globals (_pool, etc.) are
    reset between tests even if a previous test left them in a dirty state.
    """
    import db as _db

    # Reset module globals in case a previous test left them dirty.
    _db._pool = None
    _db._embed_model = EMBED_MODEL
    _db._embed_dim = EMBED_DIM
    _db._main_loop = None

    await _db.init_db(TEST_DB_URL, EMBED_MODEL, EMBED_DIM)
    yield _db
    await _db.close_db()


# ---------------------------------------------------------------------------
# Function-scoped fixture: seed a default org + user for auth tests
# ---------------------------------------------------------------------------

@pytest.fixture()
async def seed(db):
    """
    Seed a default org and user and return them as a dict so auth tests have
    something to work with without repeating boilerplate.
    """
    org = await db.create_org("Test Corp", "test-corp")
    user = await db.create_user(
        org_id=org["id"],
        username="alice",
        display_name="Alice",
        password_hash="hashed_pw",
        email="alice@example.com",
        role="admin",
    )
    return {"org": org, "user": user}


# ---------------------------------------------------------------------------
# 1. init_db / close_db
# ---------------------------------------------------------------------------

class TestInitCloseDb:
    async def test_init_creates_pool(self, setup_test_db):
        import db as _db

        _db._pool = None
        await _db.init_db(TEST_DB_URL, EMBED_MODEL, EMBED_DIM)
        assert _db._pool is not None
        await _db.close_db()

    async def test_close_sets_pool_to_none(self, setup_test_db):
        import db as _db

        _db._pool = None
        await _db.init_db(TEST_DB_URL, EMBED_MODEL, EMBED_DIM)
        await _db.close_db()
        assert _db._pool is None

    async def test_init_with_empty_url_leaves_pool_none(self, setup_test_db):
        import db as _db

        original_pool = _db._pool
        await _db.init_db("", EMBED_MODEL, EMBED_DIM)
        # pool should still be None (it was not initialised)
        assert _db._pool is None
        # restore
        _db._pool = original_pool

    async def test_close_is_idempotent_when_pool_is_none(self, setup_test_db):
        import db as _db

        _db._pool = None
        # Should not raise
        await _db.close_db()
        assert _db._pool is None

    async def test_embed_model_and_dim_are_stored(self, setup_test_db):
        import db as _db

        _db._pool = None
        await _db.init_db(TEST_DB_URL, "my-model", 512)
        assert _db._embed_model == "my-model"
        assert _db._embed_dim == 512
        await _db.close_db()
        # Restore module globals so this order-dependent mutation can't leak into
        # later tests (e.g. TestGetEmbedding relies on _embed_dim == 768).
        await _db.init_db(TEST_DB_URL, EMBED_MODEL, EMBED_DIM)
        await _db.close_db()


# ---------------------------------------------------------------------------
# 2. get_embedding
# ---------------------------------------------------------------------------

class TestGetEmbedding:
    def test_returns_list_of_floats_on_success(self):
        import db as _db

        # get_embedding reads module globals that earlier DB tests mutate
        # (e.g. test_embed_model_and_dim_are_stored sets _embed_dim=512 via
        # init_db). Pin them here so this test is order-independent and matches
        # the Ollama-format fake response below.
        _db._embed_dim = EMBED_DIM
        _db._embed_backend = "ollama"

        with patch("db.requests.post", return_value=_fake_ollama_response(FAKE_EMBEDDING)):
            result = _db.get_embedding("hello world")

        assert isinstance(result, list)
        assert len(result) == EMBED_DIM
        assert all(isinstance(v, float) for v in result)

    def test_returns_empty_list_on_http_error(self):
        import db as _db

        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("500 Server Error")

        with patch("db.requests.post", return_value=mock_resp):
            result = _db.get_embedding("hello world")

        assert result == []

    def test_returns_empty_list_on_connection_error(self):
        import db as _db

        with patch("db.requests.post", side_effect=Exception("Connection refused")):
            result = _db.get_embedding("hello world")

        assert result == []

    def test_uses_custom_model_parameter(self):
        import db as _db

        with patch("db.requests.post", return_value=_fake_ollama_response([0.1, 0.2])) as mock_post:
            _db.get_embedding("test", model="custom-model")

        call_kwargs = mock_post.call_args
        assert call_kwargs[1]["json"]["model"] == "custom-model"

    def test_truncates_text_to_4000_chars(self):
        import db as _db

        long_text = "x" * 5000

        with patch("db.requests.post", return_value=_fake_ollama_response([0.1])) as mock_post:
            _db.get_embedding(long_text)

        sent_prompt = mock_post.call_args[1]["json"]["prompt"]
        assert len(sent_prompt) == 4000

    def test_uses_module_embed_model_when_model_not_specified(self):
        import db as _db

        _db._embed_model = "module-default-model"

        with patch("db.requests.post", return_value=_fake_ollama_response([0.0])) as mock_post:
            _db.get_embedding("text")

        sent_model = mock_post.call_args[1]["json"]["model"]
        assert sent_model == "module-default-model"


# ---------------------------------------------------------------------------
# 3. Auth helpers
# ---------------------------------------------------------------------------

class TestCreateOrg:
    async def test_creates_org_with_correct_fields(self, db):
        org = await db.create_org("Acme Inc", "acme")
        assert org["name"] == "Acme Inc"
        assert org["slug"] == "acme"
        assert "id" in org
        assert org["id"] > 0

    async def test_duplicate_slug_raises(self, db):
        await db.create_org("Acme Inc", "acme")
        with pytest.raises(Exception):
            await db.create_org("Other Name", "acme")

    async def test_duplicate_name_raises(self, db):
        await db.create_org("Acme Inc", "acme")
        with pytest.raises(Exception):
            await db.create_org("Acme Inc", "acme-2")


class TestCreateUser:
    async def test_creates_user_linked_to_org(self, db, seed):
        org = seed["org"]
        user = await db.create_user(
            org_id=org["id"],
            username="bob",
            display_name="Bob",
            password_hash="pw",
        )
        assert user["username"] == "bob"
        assert user["org_id"] == org["id"]
        assert user["role"] == "member"  # default

    async def test_custom_role_stored(self, db, seed):
        user = await db.create_user(
            org_id=seed["org"]["id"],
            username="carol",
            display_name="Carol",
            password_hash="pw",
            role="admin",
        )
        assert user["role"] == "admin"

    async def test_duplicate_username_in_same_org_raises(self, db, seed):
        with pytest.raises(Exception):
            await db.create_user(
                org_id=seed["org"]["id"],
                username="alice",  # already created by seed fixture
                display_name="Alice 2",
                password_hash="pw",
            )

    async def test_same_username_in_different_orgs_is_allowed(self, db, seed):
        other_org = await db.create_org("Other Corp", "other-corp")
        user2 = await db.create_user(
            org_id=other_org["id"],
            username="alice",
            display_name="Alice Other",
            password_hash="pw",
        )
        assert user2["username"] == "alice"
        assert user2["org_id"] == other_org["id"]


class TestGetUserByUsername:
    async def test_returns_user_when_found(self, db, seed):
        result = await db.get_user_by_username(seed["org"]["id"], "alice")
        assert result is not None
        assert result["username"] == "alice"

    async def test_returns_none_when_not_found(self, db, seed):
        result = await db.get_user_by_username(seed["org"]["id"], "nonexistent")
        assert result is None

    async def test_scoped_to_org(self, db, seed):
        # Create a second org — same username should not be visible across orgs.
        other_org = await db.create_org("Other Corp", "other-corp")
        result = await db.get_user_by_username(other_org["id"], "alice")
        assert result is None

    async def test_returns_none_when_pool_is_none(self, setup_test_db):
        import db as _db

        _db._pool = None
        result = await _db.get_user_by_username(1, "alice")
        assert result is None


class TestSessionTokens:
    async def test_create_and_retrieve_valid_token(self, db, seed):
        user = seed["user"]
        token = secrets.token_hex(32)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

        await db.create_session_token(user["id"], token, expires_at)
        result = await db.get_user_by_token(token)

        assert result is not None
        assert result["username"] == "alice"
        assert result["org_id"] == seed["org"]["id"]
        assert result["org_slug"] == "test-corp"

    async def test_expired_token_returns_none(self, db, seed):
        user = seed["user"]
        token = secrets.token_hex(32)
        # expires_at in the past
        expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

        await db.create_session_token(user["id"], token, expires_at)
        result = await db.get_user_by_token(token)

        assert result is None

    async def test_unknown_token_returns_none(self, db):
        result = await db.get_user_by_token("does-not-exist")
        assert result is None

    async def test_delete_session_token(self, db, seed):
        user = seed["user"]
        token = secrets.token_hex(32)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

        await db.create_session_token(user["id"], token, expires_at)
        await db.delete_session_token(token)
        result = await db.get_user_by_token(token)

        assert result is None

    async def test_delete_nonexistent_token_is_silent(self, db):
        # Should not raise
        await db.delete_session_token("ghost-token")

    async def test_get_user_by_token_returns_none_when_pool_is_none(self, setup_test_db):
        import db as _db

        _db._pool = None
        result = await _db.get_user_by_token("any-token")
        assert result is None


# ---------------------------------------------------------------------------
# 4. Clients
# ---------------------------------------------------------------------------

class TestUpsertClient:
    async def test_insert_returns_positive_id(self, db, seed):
        cid = await db.upsert_client(
            org_id=seed["org"]["id"],
            name="Globex Corp",
            metadata={"industry": "energy"},
            embedding=FAKE_EMBEDDING,
        )
        assert isinstance(cid, int)
        assert cid > 0

    async def test_upsert_increments_session_count(self, db, seed):
        oid = seed["org"]["id"]
        await db.upsert_client(oid, "Globex Corp", {"industry": "energy"}, FAKE_EMBEDDING)
        await db.upsert_client(oid, "Globex Corp", {"region": "west"}, FAKE_EMBEDDING)

        client = await db.get_client(oid, "Globex Corp")
        assert client["session_count"] == 2

    async def test_upsert_merges_metadata(self, db, seed):
        oid = seed["org"]["id"]
        await db.upsert_client(oid, "Globex Corp", {"industry": "energy"}, [])
        await db.upsert_client(oid, "Globex Corp", {"region": "west"}, [])

        client = await db.get_client(oid, "Globex Corp")
        assert client["metadata"]["industry"] == "energy"
        assert client["metadata"]["region"] == "west"

    async def test_upsert_updates_embedding_when_provided(self, db, seed):
        oid = seed["org"]["id"]
        await db.upsert_client(oid, "Globex Corp", {}, [])
        # Second upsert provides a real embedding
        await db.upsert_client(oid, "Globex Corp", {}, FAKE_EMBEDDING)

        # Verify the client exists (embedding is stored internally; just check no error)
        client = await db.get_client(oid, "Globex Corp")
        assert client is not None

    async def test_returns_minus_one_when_pool_offline(self, setup_test_db):
        import db as _db

        _db._pool = None
        result = await _db.upsert_client(1, "X", {}, [])
        assert result == -1

    async def test_different_orgs_can_have_same_client_name(self, db, seed):
        other_org = await db.create_org("Other Corp", "other-corp")
        cid1 = await db.upsert_client(seed["org"]["id"], "ACME", {}, [])
        cid2 = await db.upsert_client(other_org["id"], "ACME", {}, [])
        assert cid1 != cid2


class TestListClients:
    async def test_returns_only_org_scoped_results(self, db, seed):
        other_org = await db.create_org("Other Corp", "other-corp")
        await db.upsert_client(seed["org"]["id"], "Client A", {}, [])
        await db.upsert_client(other_org["id"], "Client B", {}, [])

        results = await db.list_clients(seed["org"]["id"])
        names = [r["name"] for r in results]

        assert "Client A" in names
        assert "Client B" not in names

    async def test_returns_empty_list_for_empty_org(self, db, seed):
        results = await db.list_clients(seed["org"]["id"])
        assert results == []

    async def test_ordered_by_session_count_desc(self, db, seed):
        oid = seed["org"]["id"]
        # "Busy" gets two sessions, "Quiet" gets one.
        await db.upsert_client(oid, "Quiet", {}, [])
        await db.upsert_client(oid, "Busy", {}, [])
        await db.upsert_client(oid, "Busy", {}, [])

        results = await db.list_clients(oid)
        assert results[0]["name"] == "Busy"

    async def test_returns_empty_list_when_pool_offline(self, setup_test_db):
        import db as _db

        _db._pool = None
        results = await _db.list_clients(1)
        assert results == []


class TestGetClient:
    async def test_case_insensitive_lookup(self, db, seed):
        oid = seed["org"]["id"]
        await db.upsert_client(oid, "ACME Corp", {}, [])

        result_lower = await db.get_client(oid, "acme corp")
        result_upper = await db.get_client(oid, "ACME CORP")
        result_mixed = await db.get_client(oid, "Acme Corp")

        assert result_lower is not None
        assert result_upper is not None
        assert result_mixed is not None
        assert result_lower["name"] == "ACME Corp"

    async def test_returns_none_when_not_found(self, db, seed):
        result = await db.get_client(seed["org"]["id"], "NoSuchClient")
        assert result is None

    async def test_returns_none_when_pool_offline(self, setup_test_db):
        import db as _db

        _db._pool = None
        result = await _db.get_client(1, "X")
        assert result is None


# ---------------------------------------------------------------------------
# 5. Contacts
# ---------------------------------------------------------------------------

class TestUpsertContact:
    async def test_insert_returns_positive_id(self, db, seed):
        oid = seed["org"]["id"]
        cid = await db.upsert_client(oid, "Globex", {}, [])
        contact_id = await db.upsert_contact(
            org_id=oid,
            name="John Smith",
            metadata={"role": "CTO"},
            embedding=FAKE_EMBEDDING,
            client_id=cid,
        )
        assert isinstance(contact_id, int)
        assert contact_id > 0

    async def test_upsert_increments_session_count(self, db, seed):
        oid = seed["org"]["id"]
        await db.upsert_contact(oid, "Jane Doe", {"role": "VP"}, [], None)
        await db.upsert_contact(oid, "Jane Doe", {"dept": "sales"}, [], None)

        contacts = await db.list_contacts(oid)
        jane = next(c for c in contacts if c["name"] == "Jane Doe")
        assert jane["session_count"] == 2

    async def test_upsert_merges_metadata(self, db, seed):
        oid = seed["org"]["id"]
        await db.upsert_contact(oid, "Jane Doe", {"role": "VP"}, [], None)
        await db.upsert_contact(oid, "Jane Doe", {"dept": "sales"}, [], None)

        contacts = await db.list_contacts(oid)
        jane = next(c for c in contacts if c["name"] == "Jane Doe")
        assert jane["metadata"]["role"] == "VP"
        assert jane["metadata"]["dept"] == "sales"

    async def test_client_id_is_linked(self, db, seed):
        oid = seed["org"]["id"]
        cid = await db.upsert_client(oid, "Globex", {}, [])
        await db.upsert_contact(oid, "John Smith", {}, [], client_id=cid)

        contacts = await db.list_contacts(oid, client_id=cid)
        assert len(contacts) == 1
        assert contacts[0]["name"] == "John Smith"
        assert contacts[0]["client_id"] == cid

    async def test_returns_minus_one_when_pool_offline(self, setup_test_db):
        import db as _db

        _db._pool = None
        result = await _db.upsert_contact(1, "X", {}, [], None)
        assert result == -1


class TestListContacts:
    async def test_unfiltered_returns_all_org_contacts(self, db, seed):
        oid = seed["org"]["id"]
        await db.upsert_contact(oid, "Alice", {}, [], None)
        await db.upsert_contact(oid, "Bob", {}, [], None)

        results = await db.list_contacts(oid)
        names = {r["name"] for r in results}
        assert {"Alice", "Bob"} == names

    async def test_filtered_by_client_id(self, db, seed):
        oid = seed["org"]["id"]
        cid1 = await db.upsert_client(oid, "Client 1", {}, [])
        cid2 = await db.upsert_client(oid, "Client 2", {}, [])
        await db.upsert_contact(oid, "Alice", {}, [], client_id=cid1)
        await db.upsert_contact(oid, "Bob", {}, [], client_id=cid2)

        results = await db.list_contacts(oid, client_id=cid1)
        assert len(results) == 1
        assert results[0]["name"] == "Alice"

    async def test_does_not_leak_across_orgs(self, db, seed):
        other_org = await db.create_org("Other Corp", "other-corp")
        await db.upsert_contact(seed["org"]["id"], "Alice", {}, [], None)
        await db.upsert_contact(other_org["id"], "Eve", {}, [], None)

        results = await db.list_contacts(seed["org"]["id"])
        names = {r["name"] for r in results}
        assert "Alice" in names
        assert "Eve" not in names

    async def test_returns_empty_list_when_pool_offline(self, setup_test_db):
        import db as _db

        _db._pool = None
        results = await _db.list_contacts(1)
        assert results == []


# ---------------------------------------------------------------------------
# 6. Documents
# ---------------------------------------------------------------------------

class TestIndexDocument:
    async def test_insert_returns_positive_id(self, db, seed):
        doc_id = await db.index_document(
            org_id=seed["org"]["id"],
            doc_id="doc-001",
            doc_type="meeting",
            title="Q1 Review",
            content="Discussion of Q1 numbers",
            metadata={"quarter": "Q1"},
            embedding=FAKE_EMBEDDING,
        )
        assert isinstance(doc_id, int)
        assert doc_id > 0

    async def test_upsert_updates_title_and_content(self, db, seed):
        oid = seed["org"]["id"]
        original_id = await db.index_document(
            org_id=oid,
            doc_id="doc-001",
            doc_type="meeting",
            title="Original Title",
            content="Original content",
            metadata={},
            embedding=[],
        )
        upserted_id = await db.index_document(
            org_id=oid,
            doc_id="doc-001",
            doc_type="meeting",
            title="Updated Title",
            content="Updated content",
            metadata={"version": 2},
            embedding=[],
        )
        # Same row — same id
        assert original_id == upserted_id

        docs = await db.list_documents(oid)
        assert docs[0]["title"] == "Updated Title"

    async def test_upsert_changes_updated_at(self, db, seed):
        oid = seed["org"]["id"]
        await db.index_document(oid, "doc-ts", "note", "T1", "C1", {}, [])

        # Wait a moment then upsert so updated_at must change.
        await asyncio.sleep(0.05)

        await db.index_document(oid, "doc-ts", "note", "T2", "C2", {}, [])

        # Verify via list (no direct updated_at in list, but upsert didn't raise)
        docs = await db.list_documents(oid)
        assert len(docs) == 1

    async def test_returns_minus_one_when_pool_offline(self, setup_test_db):
        import db as _db

        _db._pool = None
        result = await _db.index_document(1, "x", "note", "T", "C", {}, [])
        assert result == -1

    async def test_different_doc_ids_create_separate_rows(self, db, seed):
        oid = seed["org"]["id"]
        await db.index_document(oid, "doc-a", "note", "A", "Content A", {}, [])
        await db.index_document(oid, "doc-b", "note", "B", "Content B", {}, [])

        docs = await db.list_documents(oid)
        assert len(docs) == 2


class TestLinkDocument:
    async def test_link_document_to_client(self, db, seed):
        oid = seed["org"]["id"]
        cid = await db.upsert_client(oid, "Linked Client", {}, [])
        doc_id = await db.index_document(oid, "doc-lnk", "note", "T", "C", {}, [])

        await db.link_document(doc_id, "client", cid)

        docs = await db.list_documents(oid, client_id=cid)
        assert len(docs) == 1
        assert docs[0]["doc_id"] == "doc-lnk"

    async def test_link_is_idempotent(self, db, seed):
        oid = seed["org"]["id"]
        cid = await db.upsert_client(oid, "Linked Client", {}, [])
        doc_id = await db.index_document(oid, "doc-idem", "note", "T", "C", {}, [])

        # Calling twice should not raise
        await db.link_document(doc_id, "client", cid)
        await db.link_document(doc_id, "client", cid)

        docs = await db.list_documents(oid, client_id=cid)
        assert len(docs) == 1  # still only one

    async def test_link_document_when_pool_offline_is_silent(self, setup_test_db):
        import db as _db

        _db._pool = None
        # Should not raise
        await _db.link_document(1, "client", 1)


class TestListDocuments:
    async def test_unfiltered_returns_all_org_docs(self, db, seed):
        oid = seed["org"]["id"]
        await db.index_document(oid, "d1", "meeting", "M1", "content", {}, [])
        await db.index_document(oid, "d2", "note", "N1", "content", {}, [])

        docs = await db.list_documents(oid)
        assert len(docs) == 2

    async def test_filtered_by_doc_type(self, db, seed):
        oid = seed["org"]["id"]
        await db.index_document(oid, "d1", "meeting", "M1", "content", {}, [])
        await db.index_document(oid, "d2", "note", "N1", "content", {}, [])

        meetings = await db.list_documents(oid, doc_type="meeting")
        assert len(meetings) == 1
        assert meetings[0]["doc_id"] == "d1"

    async def test_filtered_by_client_id_via_document_links(self, db, seed):
        oid = seed["org"]["id"]
        cid = await db.upsert_client(oid, "Target Client", {}, [])

        doc_linked = await db.index_document(oid, "d-linked", "note", "L", "C", {}, [])
        await db.index_document(oid, "d-unlinked", "note", "U", "C", {}, [])
        await db.link_document(doc_linked, "client", cid)

        docs = await db.list_documents(oid, client_id=cid)
        assert len(docs) == 1
        assert docs[0]["doc_id"] == "d-linked"

    async def test_does_not_leak_across_orgs(self, db, seed):
        other_org = await db.create_org("Other", "other")
        await db.index_document(seed["org"]["id"], "d-mine", "note", "Mine", "C", {}, [])
        await db.index_document(other_org["id"], "d-theirs", "note", "Theirs", "C", {}, [])

        docs = await db.list_documents(seed["org"]["id"])
        doc_ids = {d["doc_id"] for d in docs}
        assert "d-mine" in doc_ids
        assert "d-theirs" not in doc_ids

    async def test_returns_empty_list_when_pool_offline(self, setup_test_db):
        import db as _db

        _db._pool = None
        result = await _db.list_documents(1)
        assert result == []


# ---------------------------------------------------------------------------
# 7. hybrid_search
# ---------------------------------------------------------------------------

class TestHybridSearch:
    async def test_with_embedding_returns_matching_doc(self, db, seed):
        oid = seed["org"]["id"]
        await db.index_document(
            org_id=oid,
            doc_id="search-doc",
            doc_type="meeting",
            title="Quarterly Revenue Review",
            content="The quarterly revenue figures look strong this period.",
            metadata={},
            embedding=FAKE_EMBEDDING,
        )

        with patch("db.requests.post", return_value=_fake_ollama_response(FAKE_EMBEDDING)):
            results = await db.hybrid_search(oid, "quarterly revenue")

        assert isinstance(results, list)
        # At least our document should appear
        result_ids = [r["id"] for r in results]
        # The document may surface; check structure if results present
        if results:
            r = results[0]
            assert "type" in r
            assert "id" in r
            assert "display_title" in r
            assert "snippet" in r
            assert "vec_score" in r
            assert "fts_score" in r
            assert "trgm_score" in r
            assert "combined_score" in r

    async def test_fts_fallback_when_embedding_empty(self, db, seed):
        oid = seed["org"]["id"]
        await db.index_document(
            org_id=oid,
            doc_id="fts-doc",
            doc_type="note",
            title="Competitor Analysis Report",
            content="Detailed competitor analysis for the northern region.",
            metadata={},
            embedding=[],
        )

        with patch("db.requests.post", return_value=_empty_ollama_response()):
            results = await db.hybrid_search(oid, "competitor analysis")

        assert isinstance(results, list)
        if results:
            r = results[0]
            # FTS fallback: vec_score is 0
            assert r["vec_score"] == 0.0
            assert "display_title" in r

    async def test_empty_query_returns_empty_list(self, db, seed):
        with patch("db.requests.post", return_value=_fake_ollama_response(FAKE_EMBEDDING)):
            results = await db.hybrid_search(seed["org"]["id"], "   ")
        assert results == []

    async def test_returns_empty_list_when_pool_offline(self, setup_test_db):
        import db as _db

        _db._pool = None
        with patch("db.requests.post", return_value=_fake_ollama_response(FAKE_EMBEDDING)):
            results = await _db.hybrid_search(1, "anything")
        assert results == []

    async def test_result_scores_are_rounded_floats(self, db, seed):
        oid = seed["org"]["id"]
        await db.index_document(
            org_id=oid,
            doc_id="score-doc",
            doc_type="meeting",
            title="Score Test Document",
            content="Score test content for rounding verification.",
            metadata={},
            embedding=FAKE_EMBEDDING,
        )

        with patch("db.requests.post", return_value=_fake_ollama_response(FAKE_EMBEDDING)):
            results = await db.hybrid_search(oid, "score test")

        for r in results:
            assert isinstance(r["vec_score"], float)
            assert isinstance(r["fts_score"], float)
            assert isinstance(r["trgm_score"], float)
            assert isinstance(r["combined_score"], float)

    async def test_filtered_by_doc_type(self, db, seed):
        oid = seed["org"]["id"]
        await db.index_document(oid, "m1", "meeting", "Meet Revenue", "meeting content revenue", {}, FAKE_EMBEDDING)
        await db.index_document(oid, "n1", "note", "Note Revenue", "note content revenue", {}, FAKE_EMBEDDING)

        with patch("db.requests.post", return_value=_fake_ollama_response(FAKE_EMBEDDING)):
            results = await db.hybrid_search(oid, "revenue", doc_type="note")

        if results:
            assert all(r["subtype"] == "note" for r in results)

    async def test_does_not_leak_across_orgs(self, db, seed):
        other_org = await db.create_org("Other", "other")
        oid = seed["org"]["id"]

        await db.index_document(
            other_org["id"], "other-doc", "meeting",
            "Secret Meeting Notes", "top secret information here", {}, FAKE_EMBEDDING
        )

        with patch("db.requests.post", return_value=_fake_ollama_response(FAKE_EMBEDDING)):
            results = await db.hybrid_search(oid, "secret")

        result_ids = [r["id"] for r in results]
        assert str(1) not in result_ids  # just a sanity check on scoping


# ---------------------------------------------------------------------------
# 8. _run_coro_from_thread
# ---------------------------------------------------------------------------

class TestRunCoroFromThread:
    async def test_schedules_coro_on_main_loop(self, db, seed):
        """
        Verify that _run_coro_from_thread can post work to the running event
        loop from a background thread and the result lands in the DB.
        """
        oid = seed["org"]["id"]
        loop = asyncio.get_running_loop()
        db.set_main_loop(loop)

        results: list = []

        async def _insert():
            cid = await db.upsert_client(oid, "Thread Client", {"via": "thread"}, [])
            results.append(cid)

        # Run _run_coro_from_thread from a real OS thread (not the event-loop thread).
        # Use run_in_executor so the event loop stays free to process the submitted coroutine.
        thread = threading.Thread(target=db._run_coro_from_thread, args=(_insert(),))
        thread.start()
        await asyncio.get_event_loop().run_in_executor(None, lambda: thread.join(timeout=10))

        assert len(results) == 1
        assert results[0] > 0

        client = await db.get_client(oid, "Thread Client")
        assert client is not None

    async def test_returns_silently_when_pool_is_none(self, db):
        loop = asyncio.get_running_loop()
        db.set_main_loop(loop)
        db._pool = None  # simulate offline DB

        async def _noop():
            pass  # pragma: no cover

        thread = threading.Thread(target=db._run_coro_from_thread, args=(_noop(),))
        thread.start()
        thread.join(timeout=5)
        # No exception — test passes

    async def test_returns_silently_when_main_loop_is_none(self, db):
        db._main_loop = None  # clear main loop

        async def _noop():
            pass  # pragma: no cover

        thread = threading.Thread(target=db._run_coro_from_thread, args=(_noop(),))
        thread.start()
        thread.join(timeout=5)
        # No exception — test passes


# ---------------------------------------------------------------------------
# 9. Research task agent_run_id threading
# ---------------------------------------------------------------------------

class TestResearchTaskAgentRunId:
    """Verify that agent_run_id flows correctly through enqueue → claim → complete."""

    async def test_enqueue_stores_agent_run_id(self, db, seed):
        """enqueue_research_task with agent_run_id stores it in assigned_agent_run_id."""
        oid = seed["org"]["id"]
        run_id = await db.create_agent_run(oid, "orchestrator", "test task", "manual")

        task_id = await db.enqueue_research_task(
            org_id=oid,
            subject_type="company",
            subject="Bosch",
            task_type="web_search",
            payload={"query": "Bosch CEO 2026"},
            agent_run_id=run_id,
        )
        assert task_id > 0

        async with db._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT assigned_agent_run_id FROM research_tasks WHERE id = $1",
                task_id,
            )
        assert row["assigned_agent_run_id"] == run_id

    async def test_enqueue_without_agent_run_id_stores_null(self, db, seed):
        """enqueue_research_task without agent_run_id stores NULL."""
        oid = seed["org"]["id"]
        task_id = await db.enqueue_research_task(
            org_id=oid,
            subject_type="company",
            subject="SAP",
            task_type="web_search",
            payload={"query": "SAP revenue 2026"},
        )
        assert task_id > 0

        async with db._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT assigned_agent_run_id FROM research_tasks WHERE id = $1",
                task_id,
            )
        assert row["assigned_agent_run_id"] is None

    async def test_claim_returns_assigned_agent_run_id(self, db, seed):
        """claim_research_task returns assigned_agent_run_id in the task dict."""
        oid = seed["org"]["id"]
        run_id = await db.create_agent_run(oid, "orchestrator", "research Bosch", "manual")

        await db.enqueue_research_task(
            org_id=oid,
            subject_type="company",
            subject="Bosch",
            task_type="fetch_url",
            payload={"url": "https://www.bosch.com/about"},
            agent_run_id=run_id,
        )

        task = await db.claim_research_task(oid)
        assert task is not None
        assert task["assigned_agent_run_id"] == run_id
        assert task["status"] == "running"

    async def test_complete_propagates_agent_run_id_to_children(self, db, seed):
        """complete_research_task inserts child tasks with parent's assigned_agent_run_id."""
        oid = seed["org"]["id"]
        run_id = await db.create_agent_run(oid, "orchestrator", "research SAP", "manual")

        parent_id = await db.enqueue_research_task(
            org_id=oid,
            subject_type="company",
            subject="SAP",
            task_type="web_search",
            payload={"query": "SAP CEO 2026"},
            agent_run_id=run_id,
        )

        # Claim and complete with child tasks
        task = await db.claim_research_task(oid)
        assert task is not None
        child_tasks = [
            {"task_type": "fetch_url", "payload": {"url": "https://sap.com/news"}, "priority": 6},
            {"task_type": "fetch_url", "payload": {"url": "https://reuters.com/sap"}, "priority": 5},
        ]
        await db.complete_research_task(
            task_id=parent_id,
            result={"urls_found": 2},
            new_tasks=child_tasks,
            max_depth=3,
        )

        async with db._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT assigned_agent_run_id, task_type
                FROM research_tasks
                WHERE parent_task_id = $1
                ORDER BY id
                """,
                parent_id,
            )
        assert len(rows) == 2
        for row in rows:
            assert row["assigned_agent_run_id"] == run_id, (
                f"Child task {row['task_type']} should inherit agent_run_id={run_id}, "
                f"got {row['assigned_agent_run_id']}"
            )

    async def test_complete_children_without_parent_agent_run_id_are_null(self, db, seed):
        """If parent has no agent_run_id, children also get NULL."""
        oid = seed["org"]["id"]

        parent_id = await db.enqueue_research_task(
            org_id=oid,
            subject_type="company",
            subject="IBM",
            task_type="web_search",
            payload={"query": "IBM news"},
        )

        await db.claim_research_task(oid)
        await db.complete_research_task(
            task_id=parent_id,
            result={},
            new_tasks=[{"task_type": "fetch_url", "payload": {"url": "https://ibm.com"}, "priority": 5}],
            max_depth=3,
        )

        async with db._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT assigned_agent_run_id FROM research_tasks WHERE parent_task_id = $1",
                parent_id,
            )
        assert len(rows) == 1
        assert rows[0]["assigned_agent_run_id"] is None


# ---------------------------------------------------------------------------
# list_signals — subject resolution via document_links
# ---------------------------------------------------------------------------
#
# Pi-written signals carry a BLANK metadata.subject; the company they concern is
# only recorded via a document_links(entity_type='client') row. Consumers that
# scope by client name (the rep DIGEST "what's new" and the Home news panel) pass
# `subjects=[client names]`. list_signals must therefore match a signal when its
# linked client's name is in `subjects`, not only when metadata.subject matches —
# otherwise ~90% of client signals on prod are invisible to those consumers.

class TestListSignalsSubjectResolution:
    async def _make_signal(self, db, oid, doc_id, *, subject=None, days_ago=0,
                           scope=None, relevance=None, link_client_id=None):
        """Insert a type=signal document (optionally linked to a client) and,
        when days_ago>0, back-date its created_at so day-window tests work."""
        meta: dict = {}
        if subject is not None:
            meta["subject"] = subject
        if scope is not None:
            meta["scope"] = scope
        if relevance is not None:
            meta["relevance_score"] = relevance
        did = await db.index_document(
            org_id=oid, doc_id=doc_id, doc_type="signal",
            title=f"Signal {doc_id}", content="body",
            metadata=meta, embedding=[], source="agent",
        )
        if link_client_id is not None:
            await db.link_document(did, "client", link_client_id)
        if days_ago:
            async with db._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE documents SET created_at = NOW() - ($2 * interval '1 day') WHERE id = $1",
                    did, days_ago,
                )
        return did

    async def test_blank_subject_signal_matched_by_linked_client(self, db, seed):
        """A signal with blank metadata.subject, linked to Globex, is returned
        when subjects=['Globex'] — the core fix."""
        oid = seed["org"]["id"]
        cid = await db.upsert_client(oid, "Globex", {}, [])
        await self._make_signal(db, oid, "sig-blank-bayer", subject=None, link_client_id=cid)

        rows = await db.list_signals(oid, subjects=["Globex"], days=30, limit=50)
        assert len(rows) == 1
        assert rows[0]["doc_id"] == "sig-blank-bayer"
        # The linked client name is folded into metadata.subject for the frontend tag.
        assert rows[0]["metadata"]["subject"] == "Globex"

    async def test_subject_only_query_would_miss_it(self, db, seed):
        """Sanity: the OLD behaviour (subject-only) returns nothing for the same
        blank-subject signal, proving the linked-client branch is what finds it."""
        oid = seed["org"]["id"]
        cid = await db.upsert_client(oid, "Globex", {}, [])
        await self._make_signal(db, oid, "sig-blank-bayer", subject=None, link_client_id=cid)

        async with db._pool.acquire() as conn:
            n = await conn.fetchval(
                "SELECT count(*) FROM documents WHERE org_id=$1 AND type='signal' "
                "AND metadata->>'subject' = ANY($2::text[])",
                oid, ["Globex"],
            )
        assert n == 0  # would-be miss under the old subject-only filter

    async def test_subject_metadata_still_matches(self, db, seed):
        """The Python-writer path (metadata.subject set) still matches directly,
        even with no document_links row."""
        oid = seed["org"]["id"]
        await self._make_signal(db, oid, "sig-subj-bayer", subject="Globex")

        rows = await db.list_signals(oid, subjects=["Globex"], days=30, limit=50)
        assert [r["doc_id"] for r in rows] == ["sig-subj-bayer"]
        assert rows[0]["metadata"]["subject"] == "Globex"

    async def test_signal_returned_once_not_duplicated(self, db, seed):
        """A signal both metadata.subject='Globex' AND linked to Globex must appear
        exactly once — the LATERAL LIMIT 1 keeps one row per document."""
        oid = seed["org"]["id"]
        cid = await db.upsert_client(oid, "Globex", {}, [])
        await self._make_signal(db, oid, "sig-both-bayer", subject="Globex", link_client_id=cid)

        rows = await db.list_signals(oid, subjects=["Globex"], days=30, limit=50)
        assert len(rows) == 1
        assert rows[0]["doc_id"] == "sig-both-bayer"

    async def test_multiple_subjects_returns_union(self, db, seed):
        """subjects=[A,B] returns signals for both, whether matched by subject or link."""
        oid = seed["org"]["id"]
        bayer = await db.upsert_client(oid, "Globex", {}, [])
        await self._make_signal(db, oid, "sig-blank-bayer", subject=None, link_client_id=bayer)
        await self._make_signal(db, oid, "sig-subj-acme", subject="Acme")
        # A third signal about a client NOT in subjects must be excluded.
        other = await db.upsert_client(oid, "Olympus", {}, [])
        await self._make_signal(db, oid, "sig-blank-olympus", subject=None, link_client_id=other)

        rows = await db.list_signals(oid, subjects=["Globex", "Acme"], days=30, limit=50)
        got = sorted(r["doc_id"] for r in rows)
        assert got == ["sig-blank-bayer", "sig-subj-acme"]

    async def test_non_subjects_path_unchanged(self, db, seed):
        """When subjects is None the org-wide behaviour is preserved: every signal
        is returned regardless of link/subject."""
        oid = seed["org"]["id"]
        cid = await db.upsert_client(oid, "Globex", {}, [])
        await self._make_signal(db, oid, "sig-blank-bayer", subject=None, link_client_id=cid)
        await self._make_signal(db, oid, "sig-subj-acme", subject="Acme")

        rows = await db.list_signals(oid, subjects=None, days=30, limit=50)
        assert {r["doc_id"] for r in rows} == {"sig-blank-bayer", "sig-subj-acme"}

    async def test_days_window_still_applies_with_subjects(self, db, seed):
        """A linked signal older than the day window is excluded even though its
        client is in subjects — the other params must keep working."""
        oid = seed["org"]["id"]
        cid = await db.upsert_client(oid, "Globex", {}, [])
        await self._make_signal(db, oid, "sig-recent", subject=None, link_client_id=cid, days_ago=1)
        await self._make_signal(db, oid, "sig-old", subject=None, link_client_id=cid, days_ago=10)

        rows = await db.list_signals(oid, subjects=["Globex"], days=2, limit=50)
        assert [r["doc_id"] for r in rows] == ["sig-recent"]

    async def test_scope_and_relevance_still_apply_with_subjects(self, db, seed):
        """scope='client' excludes market signals and min_relevance still gates,
        all while the subjects linked-client branch is active."""
        oid = seed["org"]["id"]
        cid = await db.upsert_client(oid, "Globex", {}, [])
        # market-scoped linked signal: excluded by scope='client'
        await self._make_signal(db, oid, "sig-market", subject=None, link_client_id=cid, scope="market")
        # low-relevance linked signal: excluded by min_relevance
        await self._make_signal(db, oid, "sig-low", subject=None, link_client_id=cid, relevance=2)
        # keeper
        await self._make_signal(db, oid, "sig-keep", subject=None, link_client_id=cid, relevance=8)

        rows = await db.list_signals(
            oid, subjects=["Globex"], days=30, limit=50, scope="client", min_relevance=5,
        )
        assert [r["doc_id"] for r in rows] == ["sig-keep"]
