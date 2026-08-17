"""
tests/test_embed_config.py — embedding-config correctness (no DB required).

Guards the three holes that let bring-your-own-embedding-provider setups
silently produce mixed vector spaces:

1. mcp_server must pass the FULL embed config (backend/url/key/model/dim) to
   db.init_db — otherwise it defaults to the "ollama" backend and writes
   vectors in a different embedding space than the main server.
2. mcp_server must honour EMBED_* env overrides the same way the main server
   does (context.py).
3. db.warn_on_embed_dim_mismatch must warn loudly on a stored-vs-configured
   dimension mismatch, and stay silent + non-fatal in every other case.

All tests run without postgres: init_db is monkeypatched and the dim probe is
exercised against a fake pool/connection.
"""

import importlib
import inspect
import logging

import pytest

import db
import mcp_server


# ---------------------------------------------------------------------------
# Fakes for the dim-mismatch probe
# ---------------------------------------------------------------------------

class FakeConn:
    """Connection whose fetchval returns a canned value or raises."""

    def __init__(self, value=None, exc: Exception = None):
        self.value = value
        self.exc = exc
        self.queries = []

    async def fetchval(self, query, *args):
        self.queries.append(query)
        if self.exc is not None:
            raise self.exc
        return self.value


class FakePool:
    def __init__(self, conn: FakeConn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


# ---------------------------------------------------------------------------
# 1. mcp_server → init_db plumbing
# ---------------------------------------------------------------------------

def test_init_db_signature_accepts_embed_kwargs():
    """Signature drift in db.init_db would break the mcp_server call."""
    params = inspect.signature(db.init_db).parameters
    for name in ("embed_model", "embed_dim", "embed_backend", "embed_url", "embed_api_key"):
        assert name in params, f"db.init_db lost parameter {name!r}"


async def test_mcp_server_lifespan_passes_full_embed_config(monkeypatch):
    """The MCP server must hand init_db the same backend/url/key as the main
    server — model+dim alone silently falls back to the ollama backend."""
    calls = {}

    async def fake_init_db(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs

    async def fake_close_db():
        calls["closed"] = True

    monkeypatch.setattr(db, "init_db", fake_init_db)
    monkeypatch.setattr(db, "close_db", fake_close_db)

    async with mcp_server.lifespan(None):
        pass

    assert calls["args"] == (
        mcp_server._DB_URL,
        mcp_server._EMBED_MODEL,
        mcp_server._EMBED_DIM,
    )
    assert calls["kwargs"] == {
        "embed_backend": mcp_server._EMBED_BACKEND,
        "embed_url": mcp_server._EMBED_URL,
        "embed_api_key": mcp_server._EMBED_API_KEY,
    }
    assert calls.get("closed") is True


def test_mcp_server_honours_embed_env_overrides(monkeypatch):
    """EMBED_* env vars must win over config.yaml, mirroring context.py."""
    monkeypatch.setenv("EMBED_BACKEND", "openai")
    monkeypatch.setenv("EMBED_URL", "https://example.test/api")
    monkeypatch.setenv("EMBED_API_KEY", "test-key-123")
    monkeypatch.setenv("EMBED_MODEL", "test/embed-model")
    monkeypatch.setenv("EMBED_DIM", "512")
    try:
        importlib.reload(mcp_server)
        assert mcp_server._EMBED_BACKEND == "openai"
        assert mcp_server._EMBED_URL == "https://example.test/api"
        assert mcp_server._EMBED_API_KEY == "test-key-123"
        assert mcp_server._EMBED_MODEL == "test/embed-model"
        assert mcp_server._EMBED_DIM == 512
    finally:
        monkeypatch.undo()
        importlib.reload(mcp_server)


def test_mcp_server_falls_back_to_config_yaml():
    """Without env overrides the module reads config.yaml directly."""
    # After the reload in the previous test the module reflects the real
    # config.yaml (plus whatever EMBED_* the outer environment carries) —
    # assert the values match the same precedence rule used by context.py.
    import os

    import yaml

    with open(mcp_server._cfg_path) as f:
        cfg = yaml.safe_load(f)
    expected_backend = os.environ.get("EMBED_BACKEND") or cfg.get("embed_backend", "")
    expected_dim = int(os.environ.get("EMBED_DIM") or cfg.get("embed_dim", 768))
    assert mcp_server._EMBED_BACKEND == expected_backend
    assert mcp_server._EMBED_DIM == expected_dim


# ---------------------------------------------------------------------------
# 2. warn_on_embed_dim_mismatch
# ---------------------------------------------------------------------------

async def test_dim_mismatch_warns_loudly(monkeypatch, caplog):
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(value=1536)))
    monkeypatch.setattr(db, "_embed_dim", 768)
    with caplog.at_level(logging.WARNING, logger="whisper.db"):
        await db.warn_on_embed_dim_mismatch()
    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    msg = warnings[0]
    assert "1536" in msg and "768" in msg, "warning must name both dimensions"
    assert "scripts/backfill_embeddings.py --all" in msg, "warning must point at the backfill script"


async def test_dim_match_stays_silent(monkeypatch, caplog):
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(value=768)))
    monkeypatch.setattr(db, "_embed_dim", 768)
    with caplog.at_level(logging.WARNING, logger="whisper.db"):
        await db.warn_on_embed_dim_mismatch()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_empty_table_stays_silent(monkeypatch, caplog):
    """No embedded documents yet — nothing to compare, no noise."""
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(value=None)))
    monkeypatch.setattr(db, "_embed_dim", 768)
    with caplog.at_level(logging.WARNING, logger="whisper.db"):
        await db.warn_on_embed_dim_mismatch()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_probe_failure_is_non_fatal(monkeypatch, caplog):
    """A broken query (e.g. missing vector_dims, no documents table) must
    never raise — graceful degradation is a repo rule."""
    monkeypatch.setattr(
        db, "_pool", FakePool(FakeConn(exc=RuntimeError("function vector_dims does not exist")))
    )
    monkeypatch.setattr(db, "_embed_dim", 768)
    with caplog.at_level(logging.WARNING, logger="whisper.db"):
        await db.warn_on_embed_dim_mismatch()  # must not raise
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_no_pool_returns_silently(monkeypatch):
    monkeypatch.setattr(db, "_pool", None)
    await db.warn_on_embed_dim_mismatch()  # must not raise
