-- Re-enable all heartbeat jobs (focus_osint, focus_research, match_monitor, news_sweep).
--
-- They were disabled in Session 81 because unbounded parallel agent runs
-- overloaded the Pi service. The Pi service now has a concurrency cap
-- (AGENT_MAX_CONCURRENT, default 2) with a FIFO queue, so heartbeat bursts
-- queue instead of overloading — safe to re-enable.
--
-- Run on the server after deploying the queue changes:
--   docker compose exec db psql -U whisper -d whisper -f - < scripts/enable_heartbeats.sql
-- (or paste into pgweb at :5433)
-- Then restart the server so the scheduler picks the jobs up:
--   docker compose restart server

UPDATE heartbeats SET enabled = true;

SELECT agent_type, cron_expr, enabled, last_run_at FROM heartbeats ORDER BY agent_type;
