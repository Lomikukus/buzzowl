"""
tests/test_migrations.py — schema migration runner semantics (no DB required).

The contract documented in docs/upgrading.md and migrations/README.md:

  (a) Database unreachable  → graceful degradation. compose starts the server
      alongside db, so a not-yet-up database must never stop the boot.
  (b) A migration FAILS on a reachable database → fatal. The file's transaction
      rolls back and startup aborts, so the server can never serve traffic
      against a stale or half-migrated schema.

Everything here runs against fake pool/connection objects — no postgres.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

import db


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeTransaction:
    """conn.transaction() stand-in that records commit vs rollback."""

    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.transactions.append(self)
        self.committed = False
        self.rolled_back = False
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.committed = True
        else:
            self.rolled_back = True
        return False  # never swallow — the runner must see the error


class FakeConn:
    """Connection that answers the runner's probes and can fail on demand.

    fail_on: substring of the SQL that should raise; error: what to raise.
    """

    def __init__(self, current_version=1, fail_on=None, error=None,
                 orgs_exists=True, version_table_exists=True):
        self.current_version = current_version
        self.fail_on = fail_on
        self.error = error
        self.orgs_exists = orgs_exists
        self.version_table_exists = version_table_exists
        self.executed: list[str] = []
        self.transactions: list[FakeTransaction] = []

    async def fetchval(self, query, *args):
        if "information_schema.tables" in query:
            table = args[0]
            return self.orgs_exists if table == "orgs" else self.version_table_exists
        if "MAX(version)" in query:
            return self.current_version
        return None

    async def execute(self, sql, *args):
        self.executed.append(sql)
        if self.fail_on and self.fail_on in sql:
            raise self.error
        return "OK"

    def transaction(self):
        return FakeTransaction(self)


class FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def _reset_db_globals():
    """init_db mutates module globals — restore them around every test."""
    pool, version = db._pool, db._schema_version
    yield
    db._pool, db._schema_version = pool, version


def _write_migration(directory: Path, name: str, sql: str = "SELECT 1;") -> Path:
    path = directory / name
    path.write_text(sql)
    return path


# ---------------------------------------------------------------------------
# (b) A failing migration is fatal
# ---------------------------------------------------------------------------

async def test_failing_migration_raises_schema_migration_error(tmp_path):
    """Bad SQL in a migration file → SchemaMigrationError naming file + error."""
    _write_migration(tmp_path, "002_bad.sql", "ALTER TABLE nope ADD COLUMN x INT;")
    sql_error = asyncpg.UndefinedTableError('relation "nope" does not exist')
    conn = FakeConn(current_version=1, fail_on="ALTER TABLE nope", error=sql_error)

    with pytest.raises(db.SchemaMigrationError) as excinfo:
        await db._run_schema_migrations(FakePool(conn), migrations_dir=tmp_path)

    assert excinfo.value.source == "002_bad.sql"
    assert excinfo.value.original is sql_error
    assert "002_bad.sql" in str(excinfo.value)
    assert 'relation "nope" does not exist' in str(excinfo.value)


async def test_failing_migration_rolls_back_its_transaction(tmp_path):
    """Per-file transaction semantics: the failing file rolls back, nothing else."""
    _write_migration(tmp_path, "002_ok.sql", "CREATE TABLE fine ();")
    _write_migration(tmp_path, "003_bad.sql", "BOOM;")
    conn = FakeConn(current_version=1, fail_on="BOOM", error=asyncpg.PostgresSyntaxError("syntax"))

    with pytest.raises(db.SchemaMigrationError):
        await db._run_schema_migrations(FakePool(conn), migrations_dir=tmp_path)

    assert len(conn.transactions) == 2
    assert conn.transactions[0].committed and not conn.transactions[0].rolled_back
    assert conn.transactions[1].rolled_back and not conn.transactions[1].committed


async def test_failing_migration_stops_the_chain(tmp_path):
    """Migrations after the failing one must not be attempted."""
    _write_migration(tmp_path, "002_bad.sql", "BOOM;")
    _write_migration(tmp_path, "003_later.sql", "SELECT 'later';")
    conn = FakeConn(current_version=1, fail_on="BOOM", error=asyncpg.PostgresSyntaxError("syntax"))

    with pytest.raises(db.SchemaMigrationError):
        await db._run_schema_migrations(FakePool(conn), migrations_dir=tmp_path)

    assert not any("later" in sql for sql in conn.executed)


async def test_duplicate_migration_versions_are_fatal(tmp_path):
    """Two files claiming the same version is a repo bug — refuse to boot."""
    _write_migration(tmp_path, "002_one.sql")
    _write_migration(tmp_path, "002_two.sql")
    conn = FakeConn(current_version=1)

    with pytest.raises(db.SchemaMigrationError):
        await db._run_schema_migrations(FakePool(conn), migrations_dir=tmp_path)


async def test_unreadable_migration_file_is_fatal_not_infrastructure(tmp_path):
    """A file error is a broken build, not an unreachable DB — both are OSError."""
    path = _write_migration(tmp_path, "002_gone.sql")
    conn = FakeConn(current_version=1)

    with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
        with pytest.raises(db.SchemaMigrationError) as excinfo:
            await db._run_schema_migrations(FakePool(conn), migrations_dir=tmp_path)

    assert excinfo.value.source == path.name
    assert isinstance(excinfo.value.original, PermissionError)


async def test_init_db_aborts_startup_when_a_migration_fails():
    """The whole point: init_db propagates, so FastAPI startup fails."""
    failure = db.SchemaMigrationError("007_boom.sql", asyncpg.PostgresSyntaxError("syntax"))

    with patch.object(db.asyncpg, "connect", new=AsyncMock(return_value=AsyncMock())), \
         patch.object(db.asyncpg, "create_pool", new=AsyncMock(return_value=FakePool(FakeConn()))), \
         patch.object(db, "_run_schema_migrations", new=AsyncMock(side_effect=failure)), \
         patch.object(db, "warn_on_embed_dim_mismatch", new=AsyncMock()) as probe:
        with pytest.raises(db.SchemaMigrationError):
            await db.init_db("postgresql://x/y", "m", 768)

    # Startup aborted before the rest of the boot sequence ran.
    probe.assert_not_awaited()


async def test_init_db_logs_the_failure_loudly(caplog):
    failure = db.SchemaMigrationError("007_boom.sql", asyncpg.PostgresSyntaxError("bad syntax"))

    with patch.object(db.asyncpg, "connect", new=AsyncMock(return_value=AsyncMock())), \
         patch.object(db.asyncpg, "create_pool", new=AsyncMock(return_value=FakePool(FakeConn()))), \
         patch.object(db, "_run_schema_migrations", new=AsyncMock(side_effect=failure)), \
         patch.object(db, "warn_on_embed_dim_mismatch", new=AsyncMock()):
        with caplog.at_level("ERROR", logger="whisper.db"):
            with pytest.raises(db.SchemaMigrationError):
                await db.init_db("postgresql://x/y", "m", 768)

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors, "a failed migration must log at ERROR level"
    text = " ".join(r.getMessage() for r in errors)
    assert "007_boom.sql" in text and "bad syntax" in text


# ---------------------------------------------------------------------------
# (a) An unreachable database still degrades gracefully
# ---------------------------------------------------------------------------

async def test_init_db_degrades_when_pool_cannot_be_created():
    """DB not up yet (compose starts server next to db) → boot, don't crash."""
    with patch.object(db.asyncpg, "connect", new=AsyncMock(side_effect=OSError("refused"))), \
         patch.object(db.asyncpg, "create_pool",
                      new=AsyncMock(side_effect=ConnectionRefusedError("refused"))), \
         patch.object(db, "_run_schema_migrations", new=AsyncMock()) as runner, \
         patch.object(db, "warn_on_embed_dim_mismatch", new=AsyncMock()):
        await db.init_db("postgresql://x/y", "m", 768)   # must not raise

    assert db._pool is None
    runner.assert_not_awaited()
    assert db.get_schema_version() is None


@pytest.mark.parametrize("exc", [
    asyncpg.ConnectionDoesNotExistError("gone"),
    asyncpg.CannotConnectNowError("starting up"),
    asyncpg.InterfaceError("pool is closed"),
    ConnectionResetError("reset"),
    OSError("socket"),
    asyncio.TimeoutError(),
])
async def test_init_db_degrades_when_connection_lost_mid_migration(exc):
    """Losing the DB while migrating is infrastructure, not a bad migration."""
    with patch.object(db.asyncpg, "connect", new=AsyncMock(return_value=AsyncMock())), \
         patch.object(db.asyncpg, "create_pool", new=AsyncMock(return_value=FakePool(FakeConn()))), \
         patch.object(db, "_run_schema_migrations", new=AsyncMock(side_effect=exc)), \
         patch.object(db, "warn_on_embed_dim_mismatch", new=AsyncMock()) as probe:
        await db.init_db("postgresql://x/y", "m", 768)   # must not raise

    probe.assert_awaited()   # boot continued


@pytest.mark.parametrize("exc", [
    asyncpg.ConnectionDoesNotExistError("gone"),
    asyncpg.InterfaceError("pool is closed"),
    OSError("socket"),
])
async def test_runner_propagates_connection_errors_unwrapped(tmp_path, exc):
    """The runner must not disguise a lost connection as a migration failure."""
    _write_migration(tmp_path, "002_fine.sql", "CREATE TABLE fine ();")
    conn = FakeConn(current_version=1, fail_on="CREATE TABLE fine", error=exc)

    with pytest.raises(type(exc)):
        await db._run_schema_migrations(FakePool(conn), migrations_dir=tmp_path)


# ---------------------------------------------------------------------------
# Happy path + version reporting
# ---------------------------------------------------------------------------

async def test_successful_run_applies_and_stamps_each_file(tmp_path):
    _write_migration(tmp_path, "002_a.sql", "CREATE TABLE a ();")
    _write_migration(tmp_path, "003_b.sql", "CREATE TABLE b ();")
    conn = FakeConn(current_version=1)

    await db._run_schema_migrations(FakePool(conn), migrations_dir=tmp_path)

    assert "CREATE TABLE a ();" in conn.executed
    assert "CREATE TABLE b ();" in conn.executed
    assert all(t.committed for t in conn.transactions)
    assert db.get_schema_version() == 3


async def test_schema_version_reflects_already_current_database(tmp_path):
    """Nothing pending → the recorded version is what the DB already reports."""
    conn = FakeConn(current_version=5)

    await db._run_schema_migrations(FakePool(conn), migrations_dir=tmp_path)

    assert db.get_schema_version() == 5


async def test_health_payload_exposes_schema_version():
    """Operators must be able to see the live schema version from /api/health."""
    import server

    fake_db = MagicMock()
    fake_db.get_schema_version.return_value = 12
    fake_db.embed_stats = {"ok": 0, "fail": 0, "last_error": None}
    fake_db._pool = MagicMock()
    fake_db._pool.fetchval = AsyncMock(return_value=1)
    fake_db.embed_text = AsyncMock(return_value=[0.1])

    with patch.object(server, "DB_AVAILABLE", True), \
         patch.object(server, "db_module", fake_db), \
         patch.dict(server.config, {"agent_service_url_pi": ""}):
        payload = await server.health_check()

    assert payload["checks"]["schema_version"] == 12
