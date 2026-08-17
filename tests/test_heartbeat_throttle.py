"""
tests/test_heartbeat_throttle.py — tiered heartbeat client selection + news
change-detection gate (Session 85).

Covers routers.pipeline._select_heartbeat_clients and _client_news_changed.
"""

import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routers import pipeline


NOW = datetime.now(timezone.utc)


def _client(cid, name, is_focus=False):
    return {"id": cid, "name": name, "metadata": {"is_focus": is_focus}}


def _patch_db(clients, last_docs):
    db = MagicMock()
    db.list_clients = AsyncMock(return_value=clients)
    db.get_client_last_doc_dates = AsyncMock(return_value=last_docs)
    db.update_client_metadata = AsyncMock()
    return patch.object(pipeline, "db_module", db)


def _patch_config(**overrides):
    cfg = {
        "heartbeat_stale_days": 14,
        "heartbeat_max_nonfocus_per_run": 3,
        "news_change_detection": True,
        "searxng_url": "http://localhost:8080",
    }
    cfg.update(overrides)
    return patch.object(pipeline.context, "config", cfg)


# ---------------------------------------------------------------------------
# _select_heartbeat_clients
# ---------------------------------------------------------------------------

class TestSelectHeartbeatClients:
    @pytest.mark.asyncio
    async def test_focus_clients_always_selected_for_research(self):
        clients = [_client(1, "Acme", is_focus=True), _client(2, "Beta")]
        with _patch_db(clients, {2: NOW}), _patch_config():
            selected, summary = await pipeline._select_heartbeat_clients(1, "research")
        names = [c["name"] for c in selected]
        assert "Acme" in names
        assert "Beta" not in names           # fresh non-focus is skipped
        assert summary["focus_selected"] == 1

    @pytest.mark.asyncio
    async def test_osint_news_gate_skips_unchanged_focus(self):
        clients = [_client(1, "Acme", is_focus=True), _client(2, "Bosch", is_focus=True)]
        async def fake_gate(org_id, client):
            return client["name"] == "Bosch"   # only Bosch changed
        with _patch_db(clients, {}), _patch_config(), \
             patch.object(pipeline, "_client_news_changed", side_effect=fake_gate):
            selected, summary = await pipeline._select_heartbeat_clients(1, "osint")
        assert [c["name"] for c in selected] == ["Bosch"]
        assert summary["focus_skipped_unchanged"] == 1

    @pytest.mark.asyncio
    async def test_news_gate_not_applied_to_research_type(self):
        clients = [_client(1, "Acme", is_focus=True)]
        gate = AsyncMock(return_value=False)
        with _patch_db(clients, {}), _patch_config(), \
             patch.object(pipeline, "_client_news_changed", gate):
            selected, _ = await pipeline._select_heartbeat_clients(1, "research")
        assert [c["name"] for c in selected] == ["Acme"]
        gate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stale_nonfocus_capped_and_oldest_first(self):
        clients = [_client(i, f"C{i}") for i in range(1, 7)]
        last_docs = {
            1: NOW - timedelta(days=20),
            2: NOW - timedelta(days=50),
            3: NOW - timedelta(days=2),    # fresh — excluded
            4: NOW - timedelta(days=30),
            # 5 has no docs at all — treated as oldest
            6: NOW - timedelta(days=15),
        }
        with _patch_db(clients, last_docs), _patch_config(heartbeat_max_nonfocus_per_run=3):
            selected, summary = await pipeline._select_heartbeat_clients(1, "research")
        # eligible stale: C5 (never), C2 (50d), C4 (30d), C1 (20d), C6 (15d) — capped to 3 oldest
        assert [c["name"] for c in selected] == ["C5", "C2", "C4"]
        assert summary["stale_nonfocus_selected"] == 3
        assert summary["skipped_total"] == 3

    @pytest.mark.asyncio
    async def test_no_clients_selected_when_all_fresh_and_no_focus(self):
        clients = [_client(1, "A"), _client(2, "B")]
        last_docs = {1: NOW, 2: NOW - timedelta(days=1)}
        with _patch_db(clients, last_docs), _patch_config():
            selected, summary = await pipeline._select_heartbeat_clients(1, "osint")
        assert selected == []
        assert summary["skipped_total"] == 2


# ---------------------------------------------------------------------------
# _client_news_changed
# ---------------------------------------------------------------------------

def _searxng_response(results):
    resp = MagicMock()
    resp.json.return_value = {"results": results}
    resp.raise_for_status.return_value = None
    return resp


def _patch_httpx(resp=None, error=None):
    client = MagicMock()
    if error:
        client.get = AsyncMock(side_effect=error)
    else:
        client.get = AsyncMock(return_value=resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return patch.object(pipeline.httpx, "AsyncClient", return_value=ctx)


class TestClientNewsChanged:
    RESULTS = [{"url": "https://a.example/1", "title": "Acme wins deal"}]

    def _expected_fp(self):
        fp_input = "\n".join(f"{r['url']}|{r['title']}" for r in self.RESULTS)
        return hashlib.sha256(fp_input.encode()).hexdigest()

    @pytest.mark.asyncio
    async def test_changed_when_no_previous_fingerprint(self):
        db = MagicMock(); db.update_client_metadata = AsyncMock()
        client = {"name": "Acme", "metadata": {}}
        with _patch_config(), _patch_httpx(_searxng_response(self.RESULTS)), \
             patch.object(pipeline, "db_module", db):
            assert await pipeline._client_news_changed(1, client) is True
        patch_arg = db.update_client_metadata.await_args.args[2]
        assert patch_arg["news_fp"] == self._expected_fp()

    @pytest.mark.asyncio
    async def test_unchanged_when_fingerprint_matches(self):
        db = MagicMock(); db.update_client_metadata = AsyncMock()
        client = {"name": "Acme", "metadata": {"news_fp": self._expected_fp()}}
        with _patch_config(), _patch_httpx(_searxng_response(self.RESULTS)), \
             patch.object(pipeline, "db_module", db):
            assert await pipeline._client_news_changed(1, client) is False

    @pytest.mark.asyncio
    async def test_changed_when_results_differ(self):
        db = MagicMock(); db.update_client_metadata = AsyncMock()
        client = {"name": "Acme", "metadata": {"news_fp": "deadbeef"}}
        with _patch_config(), _patch_httpx(_searxng_response(self.RESULTS)), \
             patch.object(pipeline, "db_module", db):
            assert await pipeline._client_news_changed(1, client) is True

    @pytest.mark.asyncio
    async def test_fail_open_when_searxng_down(self):
        db = MagicMock(); db.update_client_metadata = AsyncMock()
        client = {"name": "Acme", "metadata": {"news_fp": "deadbeef"}}
        with _patch_config(), _patch_httpx(error=ConnectionError("refused")), \
             patch.object(pipeline, "db_module", db):
            assert await pipeline._client_news_changed(1, client) is True
        db.update_client_metadata.assert_not_awaited()
