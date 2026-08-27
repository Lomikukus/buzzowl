"""
tests/test_retention.py — nightly telemetry prune.

Covers db.py's three prune helpers (batching, cutoffs, SQL shape) and
retention.run_retention (config merge, fail-safe step isolation, logging).

The DB is faked the same way the rest of the suite does it: a MagicMock pool
whose acquire() returns an async context manager yielding a MagicMock conn.
"""

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import db as db_module
import retention


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_pool(fetchval_returns):
    """MagicMock pool whose conn.fetchval yields the given values in order."""
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=list(fetchval_returns))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


def _sql_of(call):
    return call.args[0]


# ---------------------------------------------------------------------------
# db.delete_agent_runs_before
# ---------------------------------------------------------------------------

class TestDeleteAgentRuns:
    @pytest.mark.asyncio
    async def test_no_pool_is_a_noop(self):
        with patch.object(db_module, "_pool", None):
            assert await db_module.delete_agent_runs_before(datetime.now(timezone.utc)) == 0

    @pytest.mark.asyncio
    async def test_cutoff_and_batch_size_are_bound_params(self):
        pool, conn = _fake_pool([7])
        cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with patch.object(db_module, "_pool", pool):
            n = await db_module.delete_agent_runs_before(cutoff, batch_size=100)
        assert n == 7
        call = conn.fetchval.await_args
        assert call.args[1] is cutoff          # $1 = cutoff
        assert call.args[2] == 100             # $2 = batch size
        sql = _sql_of(call)
        assert "DELETE FROM agent_runs" in sql
        assert "created_at < $1" in sql
        assert "LIMIT $2" in sql
        # knowledge must never appear in a prune statement
        assert "documents" not in sql and "clients" not in sql

    @pytest.mark.asyncio
    async def test_loops_until_a_short_batch(self):
        pool, conn = _fake_pool([100, 100, 4])
        with patch.object(db_module, "_pool", pool):
            n = await db_module.delete_agent_runs_before(
                datetime.now(timezone.utc), batch_size=100)
        assert n == 204
        assert conn.fetchval.await_count == 3

    @pytest.mark.asyncio
    async def test_stops_at_max_batches(self):
        pool, conn = _fake_pool([50] * 10)
        with patch.object(db_module, "_pool", pool):
            n = await db_module.delete_agent_runs_before(
                datetime.now(timezone.utc), batch_size=50, max_batches=3)
        assert n == 150
        assert conn.fetchval.await_count == 3

    @pytest.mark.asyncio
    async def test_null_count_is_treated_as_zero(self):
        pool, _ = _fake_pool([None])
        with patch.object(db_module, "_pool", pool):
            assert await db_module.delete_agent_runs_before(datetime.now(timezone.utc)) == 0


# ---------------------------------------------------------------------------
# db.delete_prompt_log_before
# ---------------------------------------------------------------------------

class TestDeletePromptLog:
    @pytest.mark.asyncio
    async def test_no_pool_is_a_noop(self):
        with patch.object(db_module, "_pool", None):
            assert await db_module.delete_prompt_log_before(datetime.now(timezone.utc)) == 0

    @pytest.mark.asyncio
    async def test_sql_targets_prompt_log_only(self):
        pool, conn = _fake_pool([3])
        cutoff = datetime(2025, 6, 1, tzinfo=timezone.utc)
        with patch.object(db_module, "_pool", pool):
            n = await db_module.delete_prompt_log_before(cutoff, batch_size=10)
        assert n == 3
        sql = _sql_of(conn.fetchval.await_args)
        assert "DELETE FROM prompt_log" in sql
        assert "created_at < $1" in sql
        assert "agent_runs" not in sql and "documents" not in sql
        assert conn.fetchval.await_args.args[1] is cutoff


# ---------------------------------------------------------------------------
# db.compact_agent_run_tool_calls
# ---------------------------------------------------------------------------

class TestCompactToolCalls:
    @pytest.mark.asyncio
    async def test_no_pool_is_a_noop(self):
        with patch.object(db_module, "_pool", None):
            assert await db_module.compact_agent_run_tool_calls(datetime.now(timezone.utc)) == 0

    @pytest.mark.asyncio
    async def test_updates_rather_than_deletes_and_keeps_the_row(self):
        pool, conn = _fake_pool([12])
        cutoff = datetime(2026, 2, 2, tzinfo=timezone.utc)
        with patch.object(db_module, "_pool", pool):
            n = await db_module.compact_agent_run_tool_calls(cutoff, batch_size=500)
        assert n == 12
        sql = _sql_of(conn.fetchval.await_args)
        assert "UPDATE agent_runs" in sql
        assert "DELETE" not in sql                       # stage 1 never removes rows
        assert "SET tool_calls" in sql
        assert conn.fetchval.await_args.args[1] is cutoff
        assert conn.fetchval.await_args.args[2] == 500

    @pytest.mark.asyncio
    async def test_result_stays_a_jsonb_array_with_one_entry_per_call(self):
        """Consumers count calls with jsonb_array_length / Array.isArray — the
        column is NOT NULL, so it is rebuilt as an array, never nulled."""
        pool, conn = _fake_pool([1])
        with patch.object(db_module, "_pool", pool):
            await db_module.compact_agent_run_tool_calls(datetime.now(timezone.utc))
        sql = _sql_of(conn.fetchval.await_args)
        assert "jsonb_agg" in sql                        # one entry per original call
        assert "jsonb_array_elements(r.tool_calls)" in sql
        assert "'[]'::jsonb" in sql                      # COALESCE fallback, still an array
        assert "tool_calls = NULL" not in sql

    @pytest.mark.asyncio
    async def test_skips_rows_already_compacted(self):
        pool, conn = _fake_pool([0])
        with patch.object(db_module, "_pool", pool):
            await db_module.compact_agent_run_tool_calls(datetime.now(timezone.utc))
        call = conn.fetchval.await_args
        assert "NOT tool_calls @> $3::jsonb" in _sql_of(call)
        assert call.args[3] == db_module._PRUNED_TOOL_CALL_MARKER
        assert "pruned" in db_module._PRUNED_TOOL_CALL_MARKER


# ---------------------------------------------------------------------------
# retention.get_settings
# ---------------------------------------------------------------------------

class TestSettings:
    def test_defaults_when_block_missing(self):
        s = retention.get_settings({})
        assert s == retention.DEFAULTS
        assert s["enabled"] is True
        assert s["tool_call_payload_days"] == 14
        assert s["agent_runs_days"] == 90
        assert s["prompt_log_days"] == 180        # generous: evaluation looks back <= 365d

    def test_partial_override_keeps_other_defaults(self):
        s = retention.get_settings({"retention": {"prompt_log_days": 365}})
        assert s["prompt_log_days"] == 365
        assert s["agent_runs_days"] == 90
        assert s["cron"] == retention.DEFAULTS["cron"]

    def test_garbage_values_fall_back_to_defaults(self):
        s = retention.get_settings({"retention": {"agent_runs_days": "soon", "batch_size": None}})
        assert s["agent_runs_days"] == 90
        assert s["batch_size"] == retention.DEFAULTS["batch_size"]

    def test_non_dict_block_is_ignored(self):
        assert retention.get_settings({"retention": True}) == retention.DEFAULTS

    def test_batch_size_is_clamped(self):
        assert retention.get_settings({"retention": {"batch_size": 0}})["batch_size"] == 1
        assert retention.get_settings({"retention": {"batch_size": 10**9}})["batch_size"] == 50_000


# ---------------------------------------------------------------------------
# retention.run_retention
# ---------------------------------------------------------------------------

def _patch_context(config=None, db=None, available=True):
    ctx = MagicMock()
    ctx.DB_AVAILABLE = available
    ctx.config = config if config is not None else {}
    ctx.db_module = db
    return patch.dict("sys.modules", {"context": ctx}), ctx


def _mock_db():
    db = MagicMock()
    db._pool = MagicMock()
    db.delete_agent_runs_before = AsyncMock(return_value=3)
    db.compact_agent_run_tool_calls = AsyncMock(return_value=5)
    db.delete_prompt_log_before = AsyncMock(return_value=7)
    return db


class TestRunRetention:
    @pytest.mark.asyncio
    async def test_skips_when_db_unavailable(self):
        p, _ = _patch_context(db=None, available=False)
        with p:
            assert await retention.run_retention() == {"skipped": "db unavailable"}

    @pytest.mark.asyncio
    async def test_skips_when_pool_is_none(self):
        db = _mock_db()
        db._pool = None
        p, _ = _patch_context(db=db)
        with p:
            assert await retention.run_retention() == {"skipped": "db unavailable"}
        db.delete_agent_runs_before.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_master_switch_off(self):
        db = _mock_db()
        p, _ = _patch_context({"retention": {"enabled": False}}, db)
        with p:
            assert await retention.run_retention() == {"skipped": "disabled"}
        db.delete_prompt_log_before.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_all_three_steps_run_with_their_own_cutoffs(self):
        db = _mock_db()
        p, _ = _patch_context({}, db)
        before = datetime.now(timezone.utc)
        with p:
            stats = await retention.run_retention()
        assert stats == {"tool_calls_compacted": 5, "agent_runs_deleted": 3, "prompt_log_deleted": 7}

        cut_runs = db.delete_agent_runs_before.await_args.args[0]
        cut_payload = db.compact_agent_run_tool_calls.await_args.args[0]
        cut_prompts = db.delete_prompt_log_before.await_args.args[0]
        # each cutoff is "now minus its window", within a second of the call
        for cutoff, days in ((cut_runs, 90), (cut_payload, 14), (cut_prompts, 180)):
            assert abs((before - timedelta(days=days) - cutoff).total_seconds()) < 5
        # the payload strip must reach back LESS far than the row delete
        assert cut_payload > cut_runs

    @pytest.mark.asyncio
    async def test_windows_come_from_config(self):
        db = _mock_db()
        cfg = {"retention": {"agent_runs_days": 30, "tool_call_payload_days": 2,
                             "prompt_log_days": 365, "batch_size": 42}}
        p, _ = _patch_context(cfg, db)
        now = datetime.now(timezone.utc)
        with p:
            await retention.run_retention()
        assert abs((now - timedelta(days=30) - db.delete_agent_runs_before.await_args.args[0])
                   .total_seconds()) < 5
        assert abs((now - timedelta(days=2) - db.compact_agent_run_tool_calls.await_args.args[0])
                   .total_seconds()) < 5
        assert abs((now - timedelta(days=365) - db.delete_prompt_log_before.await_args.args[0])
                   .total_seconds()) < 5
        assert db.delete_agent_runs_before.await_args.args[1] == 42
        assert db.compact_agent_run_tool_calls.await_args.args[1] == 42
        assert db.delete_agent_runs_before.await_args.args[2] == retention.MAX_BATCHES

    @pytest.mark.asyncio
    async def test_a_zero_window_skips_that_step_only(self):
        db = _mock_db()
        p, _ = _patch_context({"retention": {"agent_runs_days": 0}}, db)
        with p:
            stats = await retention.run_retention()
        db.delete_agent_runs_before.assert_not_awaited()
        assert stats["agent_runs_deleted"] == 0
        db.compact_agent_run_tool_calls.assert_awaited_once()
        db.delete_prompt_log_before.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_one_failing_step_does_not_abort_the_others(self):
        db = _mock_db()
        db.delete_agent_runs_before = AsyncMock(side_effect=RuntimeError("deadlock"))
        p, _ = _patch_context({}, db)
        with p:
            stats = await retention.run_retention()
        assert stats["agent_runs_deleted"] == 0
        assert stats["tool_calls_compacted"] == 5      # still ran
        assert stats["prompt_log_deleted"] == 7        # still ran
        assert any("deadlock" in e for e in stats["errors"])

    @pytest.mark.asyncio
    async def test_every_step_failing_still_returns_instead_of_raising(self):
        db = _mock_db()
        for name in ("delete_agent_runs_before", "compact_agent_run_tool_calls",
                     "delete_prompt_log_before"):
            setattr(db, name, AsyncMock(side_effect=OSError("db gone")))
        p, _ = _patch_context({}, db)
        with p:
            stats = await retention.run_retention()
        assert len(stats["errors"]) == 3
        assert stats["agent_runs_deleted"] == 0

    @pytest.mark.asyncio
    async def test_logs_one_info_line_when_something_was_pruned(self, caplog):
        db = _mock_db()
        p, _ = _patch_context({}, db)
        with p, caplog.at_level(logging.INFO, logger="wk.retention"):
            await retention.run_retention()
        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(infos) == 1
        assert "3 agent_runs deleted" in infos[0].getMessage()
        assert "5 tool_call payloads stripped" in infos[0].getMessage()
        assert "7 prompt_log rows deleted" in infos[0].getMessage()

    @pytest.mark.asyncio
    async def test_quiet_when_nothing_to_prune(self, caplog):
        db = _mock_db()
        for name in ("delete_agent_runs_before", "compact_agent_run_tool_calls",
                     "delete_prompt_log_before"):
            setattr(db, name, AsyncMock(return_value=0))
        p, _ = _patch_context({}, db)
        with p, caplog.at_level(logging.DEBUG, logger="wk.retention"):
            await retention.run_retention()
        assert not [r for r in caplog.records if r.levelno >= logging.INFO]
        assert [r for r in caplog.records if r.levelno == logging.DEBUG]


# ---------------------------------------------------------------------------
# Scheduler wiring
# ---------------------------------------------------------------------------

class TestSchedulerWiring:
    def test_config_yaml_ships_a_valid_five_part_cron(self):
        import context
        cron = retention.get_settings(context.config)["cron"]
        assert len(cron.split()) == 5

    def test_shipped_defaults_are_sane(self):
        import context
        s = retention.get_settings(context.config)
        assert s["enabled"] is True
        assert s["tool_call_payload_days"] < s["agent_runs_days"]
        assert s["prompt_log_days"] >= 180
