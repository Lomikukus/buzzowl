"""
tests/test_pipeline.py — Pipeline integration tests.

Tests session promotion end-to-end: file I/O, DB indexing,
metadata state transitions, pipeline sweep behaviour, and the manual
promote HTTP endpoint.

Strategy:
  - server.BASE_DIR is patched to a tmp_path so all data/ reads/writes
    go to a temp tree and never touch real data.
  - DB calls are either disabled (DB_AVAILABLE=False) or wired through
    _sync_run so AsyncMocks return their configured values without needing
    the real DB pool.
  - Ollama calls (extract_entities) are patched to return fixture entities.
"""

import asyncio
import json
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_SESSION_DIR = FIXTURES / "sessions" / "fixture_session"
FIXTURE_TRANSCRIPT = FIXTURES / "transcripts" / "acme_gmbh.txt"
FIXTURE_SUMMARY = FIXTURES / "summaries" / "acme_gmbh.md"
SESSION_ID = "20260101-120000"

FIXTURE_ENTITIES = {
    "companies": [
        {"name": "Acme GmbH", "confidence": "high"},
        {"name": "NorthStar Solutions", "confidence": "medium"},
    ],
    "people": [
        {"name": "Sarah Berger", "role": "Account Executive", "confidence": "high"},
        {"name": "Marcus Weber", "role": "Kunde bei Acme GmbH", "confidence": "high"},
        {"name": "Jana Kiefer", "role": "Vertriebsleiterin", "confidence": "high"},
    ],
    "topics": ["CRM-System Salesforce", "Demo-Vereinbarung"],
}

BASE_CONFIG = {
    "vault_path": "",  # filled per test
    "language": "de",
    "ollama_model": "llama3.2",
    "embed_model": "nomic-embed-text",
    "embed_dim": 768,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_session(base_dir: Path, session_id: str, status: str = "staged") -> None:
    """Write fixture files into base_dir/data/{raw,staged}/{session_id}/."""
    raw_dir = base_dir / "data" / "raw" / session_id
    staged_dir = base_dir / "data" / "staged" / session_id
    for d in (raw_dir, staged_dir, base_dir / "data" / "sorted"):
        d.mkdir(parents=True, exist_ok=True)

    shutil.copy(FIXTURE_TRANSCRIPT, raw_dir / "transcript.txt")
    shutil.copy(FIXTURE_SUMMARY, staged_dir / "summary.md")

    meta = json.loads((FIXTURE_SESSION_DIR / "metadata.json").read_text())
    meta.update({"session_id": session_id, "status": status})
    (staged_dir / "metadata.json").write_text(json.dumps(meta, indent=2))


def _read_meta(base_dir: Path, session_id: str) -> dict:
    return json.loads(
        (base_dir / "data" / "staged" / session_id / "metadata.json").read_text()
    )


def _sync_run(coro, timeout=60):
    """Run a coroutine in a fresh, isolated event loop.

    Used as a drop-in replacement for db._run_coro_from_thread so that
    AsyncMock-based db stubs are actually awaited in sync test contexts.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _promote(base_dir: Path, vault: Path, session_id: str = SESSION_ID, db: bool = False):
    """Call _promote_session with a patched BASE_DIR and vault config."""
    from routers.pipeline import _promote_session

    cfg = {**BASE_CONFIG, "vault_path": str(vault)}
    with (
        patch("routers.pipeline.BASE_DIR", base_dir),
        patch("routers.pipeline.config", cfg),
        patch("routers.pipeline.DB_AVAILABLE", db),
    ):
        return _promote_session(session_id)


# ---------------------------------------------------------------------------
# TestPromotionRoundTrip
# ---------------------------------------------------------------------------

class TestPromotionRoundTrip:
    """_promote_session produces all expected outputs for a staged fixture session."""

    def test_returns_ok_and_title(self, staged_session, temp_vault_dir):
        base, sid = staged_session
        result = _promote(base, temp_vault_dir, sid)
        assert result["ok"] is True
        assert "Acme" in result["title"]

    def test_sorted_files_written(self, staged_session, temp_vault_dir):
        base, sid = staged_session
        _promote(base, temp_vault_dir, sid)
        assert (base / "data" / "sorted" / sid / "transcript.txt").is_file()
        assert (base / "data" / "sorted" / sid / "summary.md").is_file()

    def test_metadata_status_is_promoted(self, staged_session, temp_vault_dir):
        base, sid = staged_session
        _promote(base, temp_vault_dir, sid)
        meta = _read_meta(base, sid)
        assert meta["status"] == "promoted"
        assert meta["promoted_at"] is not None

    def test_db_document_created_with_meeting_type(self, staged_session, temp_vault_dir):
        base, sid = staged_session
        from routers.pipeline import _promote_session

        mock_index = AsyncMock(return_value=10)
        mock_upsert_client = AsyncMock(return_value=20)
        mock_upsert_contact = AsyncMock(return_value=30)
        mock_get_client = AsyncMock(return_value={"id": 20})
        mock_link = AsyncMock(return_value=None)
        mock_first_org = AsyncMock(return_value={"id": 1, "name": "North", "slug": "north"})

        mock_find_client  = AsyncMock(return_value=None)  # no existing similar client
        mock_find_contact = AsyncMock(return_value=None)  # no existing similar contact
        cfg = {**BASE_CONFIG, "vault_path": str(temp_vault_dir)}
        with (
            patch("routers.pipeline.BASE_DIR", base),
            patch("routers.pipeline.config", cfg),
            patch("routers.pipeline.DB_AVAILABLE", True),
            patch("routers.pipeline.db_module._run_coro_from_thread", side_effect=_sync_run),
            patch("routers.pipeline.db_module.get_embedding", return_value=[0.1] * 768),
            patch("routers.pipeline.db_module.get_first_org", mock_first_org),
            patch("routers.pipeline.db_module.index_document", mock_index),
            patch("routers.pipeline.db_module.find_similar_client", mock_find_client),
            patch("routers.pipeline.db_module.find_similar_contact", mock_find_contact),
            patch("routers.pipeline.db_module.upsert_client", mock_upsert_client),
            patch("routers.pipeline.db_module.upsert_contact", mock_upsert_contact),
            patch("routers.pipeline.db_module.get_client", mock_get_client),
            patch("routers.pipeline.db_module.link_document", mock_link),
        ):
            result = _promote_session(sid)

        assert result["ok"] is True
        mock_index.assert_called_once()
        _, kwargs = mock_index.call_args
        assert kwargs["doc_type"] == "meeting"
        assert kwargs["doc_id"] == sid
        # 2 companies → 2 upsert_client; 3 contacts → 3 upsert_contact
        assert mock_upsert_client.call_count == 2
        assert mock_upsert_contact.call_count == 3
        # 2 client links + 3 contact links
        assert mock_link.call_count == 5


# ---------------------------------------------------------------------------
# TestIdempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    """Calling _promote_session twice on the same session is a no-op the second time."""

    def test_second_promote_returns_already_promoted(self, staged_session, temp_vault_dir):
        base, sid = staged_session
        _promote(base, temp_vault_dir, sid)
        result2 = _promote(base, temp_vault_dir, sid)
        assert result2["ok"] is True
        assert result2.get("already_promoted") is True

    def test_second_promote_keeps_metadata_promoted(self, staged_session, temp_vault_dir):
        base, sid = staged_session
        _promote(base, temp_vault_dir, sid)
        _promote(base, temp_vault_dir, sid)
        assert _read_meta(base, sid)["status"] == "promoted"


# ---------------------------------------------------------------------------
# TestMetadataTransitions
# ---------------------------------------------------------------------------

class TestMetadataTransitions:
    """metadata.json progresses correctly through the pipeline states."""

    def test_initial_status_is_staged(self, staged_session):
        base, sid = staged_session
        assert _read_meta(base, sid)["status"] == "staged"

    def test_update_metadata_patches_fields(self, staged_session):
        base, sid = staged_session
        from routers.pipeline import _update_session_metadata, _read_session_metadata

        with patch("routers.pipeline.BASE_DIR", base):
            _update_session_metadata(sid, status="agent_working", agent_run_id=42)
            meta = _read_session_metadata(sid)

        assert meta["status"] == "agent_working"
        assert meta["agent_run_id"] == 42

    async def test_trigger_enrichment_transitions_staged_to_promoted(
        self, staged_session, temp_vault_dir
    ):
        base, sid = staged_session
        from routers.pipeline import _trigger_enrichment

        status_log: list[str] = []
        import routers.pipeline as _pl

        original_update = _pl._update_session_metadata

        def _track_update(session_id, **fields):
            if "status" in fields:
                status_log.append(fields["status"])
            original_update(session_id, **fields)

        cfg = {**BASE_CONFIG, "vault_path": str(temp_vault_dir)}
        mock_run_id = 99
        with (
            patch("routers.pipeline.BASE_DIR", base),
            patch("routers.pipeline.config", cfg),
            patch("routers.pipeline.DB_AVAILABLE", True),
            patch("routers.pipeline._update_session_metadata", side_effect=_track_update),
            patch("routers.pipeline.extract_entities", return_value=FIXTURE_ENTITIES),
            patch("routers.pipeline.db_module.create_agent_run", new_callable=AsyncMock,
                  return_value=mock_run_id),
            patch("routers.pipeline.db_module.update_agent_run", new_callable=AsyncMock),
            patch("agents._legacy.enrichment.run_enrichment", new_callable=AsyncMock,
                  return_value={"enriched": 2, "docs": ["d1"], "errors": []}),
            # _promote_session's DB block: get_first_org returns None → block skipped gracefully.
            # _run_coro_from_thread must be patched too so the unawaited coroutine is consumed.
            patch("routers.pipeline.db_module.get_first_org", new_callable=AsyncMock, return_value=None),
            patch("routers.pipeline.db_module._run_coro_from_thread", side_effect=_sync_run),
        ):
            await _trigger_enrichment(sid, org_id=1)

        # Status must have passed through agent_working → agent_done → promoted
        assert "agent_working" in status_log
        assert "agent_done" in status_log
        assert status_log[-1] == "promoted"
        assert status_log.index("agent_working") < status_log.index("agent_done")
        assert status_log.index("agent_done") < status_log.index("promoted")


# ---------------------------------------------------------------------------
# TestPipelineSweep
# ---------------------------------------------------------------------------

class TestPipelineSweep:
    """_pipeline_sweep triggers enrichment for unprocessed sessions."""

    async def test_sweep_triggers_staged_and_failed_sessions(
        self, tmp_path, temp_vault_dir
    ):
        for d in ("data/raw", "data/staged", "data/sorted"):
            (tmp_path / d).mkdir(parents=True)

        _seed_session(tmp_path, "session-a", status="staged")
        _seed_session(tmp_path, "session-b", status="failed")

        from routers.pipeline import _pipeline_sweep

        cfg = {**BASE_CONFIG, "vault_path": str(temp_vault_dir)}
        with (
            patch("routers.pipeline.BASE_DIR", tmp_path),
            patch("routers.pipeline.config", cfg),
            patch("routers.pipeline.DB_AVAILABLE", False),
            patch("routers.pipeline._trigger_enrichment", new_callable=AsyncMock) as mock_trigger,
        ):
            await _pipeline_sweep()
            # Let any created tasks complete
            await asyncio.sleep(0)

        triggered = {c.args[0] for c in mock_trigger.call_args_list}
        assert "session-a" in triggered
        assert "session-b" in triggered
        assert mock_trigger.call_count == 2

    async def test_sweep_skips_promoted_and_agent_working(
        self, tmp_path, temp_vault_dir
    ):
        for d in ("data/raw", "data/staged", "data/sorted"):
            (tmp_path / d).mkdir(parents=True)

        _seed_session(tmp_path, "done-session", status="promoted")
        _seed_session(tmp_path, "working-session", status="agent_working")
        _seed_session(tmp_path, "new-session", status="staged")

        from routers.pipeline import _pipeline_sweep

        cfg = {**BASE_CONFIG, "vault_path": str(temp_vault_dir)}
        with (
            patch("routers.pipeline.BASE_DIR", tmp_path),
            patch("routers.pipeline.config", cfg),
            patch("routers.pipeline.DB_AVAILABLE", False),
            patch("routers.pipeline._trigger_enrichment", new_callable=AsyncMock) as mock_trigger,
        ):
            await _pipeline_sweep()
            await asyncio.sleep(0)

        triggered = {c.args[0] for c in mock_trigger.call_args_list}
        assert "new-session" in triggered
        assert "done-session" not in triggered
        assert "working-session" not in triggered

    async def test_sweep_skips_session_without_summary(
        self, tmp_path, temp_vault_dir
    ):
        for d in ("data/raw", "data/staged", "data/sorted"):
            (tmp_path / d).mkdir(parents=True)

        # Seed without a summary.md (Ollama still running)
        sid = "no-summary-session"
        raw_dir = tmp_path / "data" / "raw" / sid
        staged_dir = tmp_path / "data" / "staged" / sid
        raw_dir.mkdir(parents=True)
        staged_dir.mkdir(parents=True)
        shutil.copy(FIXTURE_TRANSCRIPT, raw_dir / "transcript.txt")
        meta = {"session_id": sid, "status": "staged"}
        (staged_dir / "metadata.json").write_text(json.dumps(meta))

        from routers.pipeline import _pipeline_sweep

        cfg = {**BASE_CONFIG, "vault_path": str(temp_vault_dir)}
        with (
            patch("routers.pipeline.BASE_DIR", tmp_path),
            patch("routers.pipeline.config", cfg),
            patch("routers.pipeline.DB_AVAILABLE", False),
            patch("routers.pipeline._trigger_enrichment", new_callable=AsyncMock) as mock_trigger,
        ):
            await _pipeline_sweep()
            await asyncio.sleep(0)

        assert mock_trigger.call_count == 0


# ---------------------------------------------------------------------------
# TestManualPromoteEndpoint
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pipeline_client():
    """TestClient for pipeline endpoint tests — startup patched, no auth overrides."""
    with (
        patch("server.get_live_model", return_value=MagicMock()),
        patch("server.db_module.init_db", new_callable=AsyncMock),
        patch("server.db_module.close_db", new_callable=AsyncMock),
        patch("server.DB_AVAILABLE", False),
    ):
        from server import app
        with TestClient(app, raise_server_exceptions=True) as client:
            yield client


class TestManualPromoteEndpoint:
    """POST /api/pipeline/staged/{id}/promote via HTTP."""

    def test_promote_returns_200_and_ok(self, tmp_path, temp_vault_dir, pipeline_client):
        for d in ("data/raw", "data/staged", "data/sorted"):
            (tmp_path / d).mkdir(parents=True, exist_ok=True)
        _seed_session(tmp_path, SESSION_ID)

        cfg = {**BASE_CONFIG, "vault_path": str(temp_vault_dir)}
        with (
            patch("routers.pipeline.BASE_DIR", tmp_path),
            patch("routers.pipeline.config", cfg),
            patch("routers.pipeline.DB_AVAILABLE", False),
        ):
            resp = pipeline_client.post(f"/api/pipeline/staged/{SESSION_ID}/promote")

        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_promoted_session_appears_in_staged_list(self, tmp_path, temp_vault_dir, pipeline_client):
        for d in ("data/raw", "data/staged", "data/sorted"):
            (tmp_path / d).mkdir(parents=True, exist_ok=True)
        _seed_session(tmp_path, SESSION_ID)

        cfg = {**BASE_CONFIG, "vault_path": str(temp_vault_dir)}
        with (
            patch("routers.pipeline.BASE_DIR", tmp_path),
            patch("routers.pipeline.config", cfg),
            patch("routers.pipeline.DB_AVAILABLE", False),
        ):
            pipeline_client.post(f"/api/pipeline/staged/{SESSION_ID}/promote")
            list_resp = pipeline_client.get("/api/pipeline/staged")

        assert list_resp.status_code == 200
        sessions = list_resp.json()["sessions"]
        matching = [s for s in sessions if s.get("session_id") == SESSION_ID]
        assert len(matching) == 1
        assert matching[0]["status"] == "promoted"


# ---------------------------------------------------------------------------
# TestOsintTriggerRouting
# ---------------------------------------------------------------------------

class TestOsintTriggerRouting:
    """Verify all agent types route to Pi after Phase 29 (Hermes retired)."""

    def test_trigger_osint_fires_to_pi_url(self, test_db_org_id):
        """osint must route to Pi."""
        from routers.agents import _get_service_url
        with patch("routers.agents.config") as mock_cfg:
            mock_cfg.get = lambda key, default=None: {
                "agent_service_url_pi": "http://pi:8001",
            }.get(key, default)
            url = _get_service_url("osint")
        assert url == "http://pi:8001", f"osint must route to Pi, got {url}"

    def test_trigger_research_fires_to_pi_url(self, test_db_org_id):
        """research must route to Pi."""
        from routers.agents import _get_service_url
        with patch("routers.agents.config") as mock_cfg:
            mock_cfg.get = lambda key, default=None: {
                "agent_service_url_pi": "http://pi:8001",
            }.get(key, default)
            url = _get_service_url("research")
        assert url == "http://pi:8001", f"research must route to Pi, got {url}"

    def test_monitor_routes_to_pi(self, test_db_org_id):
        """monitor must route to Pi after Phase 29 (Hermes retired)."""
        from routers.agents import _get_service_url
        with patch("routers.agents.config") as mock_cfg:
            mock_cfg.get = lambda key, default=None: {
                "agent_service_url_pi": "http://pi:8001",
            }.get(key, default)
            url = _get_service_url("monitor")
        assert url == "http://pi:8001", f"monitor must route to Pi, got {url}"

    def test_all_formerly_hermes_types_route_to_pi(self):
        """After Phase 29, monitor/product_research/pain_point_research/match_monitor all go to Pi."""
        from routers.agents import _get_service_url
        formerly_hermes = ["monitor", "product_research", "pain_point_research", "match_monitor"]
        with patch("routers.agents.config") as mock_cfg:
            mock_cfg.get = lambda key, default=None: {
                "agent_service_url_pi": "http://pi:8001",
            }.get(key, default)
            for agent_type in formerly_hermes:
                url = _get_service_url(agent_type)
                assert url == "http://pi:8001", f"{agent_type} must route to Pi, got {url}"
