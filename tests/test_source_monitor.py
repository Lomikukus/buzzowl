"""
tests/test_source_monitor.py — monitored-sources sweep (Session 86).

Covers routers.pipeline: _discover_client_sources, _fetch_source_fp,
_monitor_client, _maybe_escalate_match, and news_pending clearing.
"""

import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routers import pipeline


def _client(name="Acme", is_focus=False, **meta):
    return {"id": 1, "name": name, "metadata": {"is_focus": is_focus, **meta}}


def _patch_db():
    db = MagicMock()
    db.update_client_metadata = AsyncMock()
    db.create_agent_run = AsyncMock(return_value=42)
    db.update_agent_run = AsyncMock()
    return patch.object(pipeline, "db_module", db), db


def _patch_config(**overrides):
    cfg = {
        "searxng_url": "http://localhost:8080",
        "match_escalation_min_relevance": 4,
        "agent_service_brain": "openrouter",
        "agent_service_model": "test-model",
    }
    cfg.update(overrides)
    return patch.object(pipeline.context, "config", cfg)


def _searxng(results):
    return patch.object(pipeline, "_searxng_results", AsyncMock(return_value=results))


# ---------------------------------------------------------------------------
# _discover_client_sources
# ---------------------------------------------------------------------------

class TestDiscoverSources:
    RESULTS = [
        {"url": "https://acme.com/newsroom", "title": "Acme Newsroom"},
        {"url": "https://acme.com/about", "title": "About"},             # no keyword → dropped
        {"url": "https://techblog.example/acme-press", "title": "Acme press coverage"},
        {"url": "https://acme.com/newsroom/", "title": "Duplicate"},     # dupe of first
    ]

    @pytest.mark.asyncio
    async def test_keyword_filter_domain_preference_dedupe(self):
        db_patch, db = _patch_db()
        client = _client(website="https://www.acme.com")
        with db_patch, _patch_config(), _searxng(self.RESULTS):
            sources = await pipeline._discover_client_sources(1, client)
        urls = [s["url"] for s in sources]
        assert "https://acme.com/newsroom" in urls
        assert all("about" not in u for u in urls)
        assert len([u for u in urls if "newsroom" in u]) == 1       # deduped
        # own-domain result sorted before third-party
        assert urls[0] == "https://acme.com/newsroom"
        saved = db.update_client_metadata.await_args.args[2]
        assert "sources_discovered_at" in saved

    @pytest.mark.asyncio
    async def test_merge_preserves_user_entries_and_caps(self):
        db_patch, _ = _patch_db()
        user_sources = [{"url": f"https://manual{i}.example/news", "label": f"m{i}"} for i in range(5)]
        client = _client(monitored_sources=user_sources)
        many = [{"url": f"https://x{i}.example/press", "title": f"t{i}"} for i in range(10)]
        with db_patch, _patch_config(), _searxng(many):
            sources = await pipeline._discover_client_sources(1, client)
        assert sources[:5] == user_sources                           # user entries kept first
        assert len(sources) <= pipeline._MAX_MONITORED_SOURCES

    @pytest.mark.asyncio
    async def test_searxng_down_returns_existing(self):
        db_patch, _ = _patch_db()
        client = _client(monitored_sources=[{"url": "https://a.example/news"}])
        with db_patch, _patch_config(), \
             patch.object(pipeline, "_searxng_results", AsyncMock(side_effect=ConnectionError)):
            sources = await pipeline._discover_client_sources(1, client)
        assert len(sources) == 1


# ---------------------------------------------------------------------------
# _fetch_source_fp
# ---------------------------------------------------------------------------

def _patch_httpx(get_text=None, get_status=200, post_text=None):
    client = MagicMock()
    get_resp = MagicMock(status_code=get_status, text=get_text or "")
    client.get = AsyncMock(return_value=get_resp)
    post_resp = MagicMock(status_code=200)
    post_resp.json.return_value = {"text": post_text or ""}
    client.post = AsyncMock(return_value=post_resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return patch.object(pipeline.httpx, "AsyncClient", return_value=ctx), client


class TestFetchSourceFp:
    @pytest.mark.asyncio
    async def test_plain_get_fingerprint(self):
        html = "<html><body><h1>News</h1>" + "Acme wins major contract. " * 50 + "</body></html>"
        httpx_patch, _ = _patch_httpx(get_text=html)
        with httpx_patch:
            fp = await pipeline._fetch_source_fp("https://acme.com/news")
        assert fp is not None and len(fp) == 64

    @pytest.mark.asyncio
    async def test_same_content_same_fp_different_content_differs(self):
        a = "<p>" + "alpha " * 100 + "</p>"
        b = "<p>" + "beta " * 100 + "</p>"
        p1, _ = _patch_httpx(get_text=a)
        with p1:
            fp1 = await pipeline._fetch_source_fp("https://x/news")
            fp1b = await pipeline._fetch_source_fp("https://x/news")
        p2, _ = _patch_httpx(get_text=b)
        with p2:
            fp2 = await pipeline._fetch_source_fp("https://x/news")
        assert fp1 == fp1b and fp1 != fp2

    @pytest.mark.asyncio
    async def test_falls_back_to_browser_service(self):
        httpx_patch, client = _patch_httpx(get_text="tiny", post_text="rendered " * 100)
        with httpx_patch:
            fp = await pipeline._fetch_source_fp("https://js-heavy.example/news")
        assert fp is not None
        client.post.assert_awaited()

    @pytest.mark.asyncio
    async def test_unreadable_returns_none(self):
        httpx_patch, _ = _patch_httpx(get_text="", get_status=403, post_text="")
        with httpx_patch:
            assert await pipeline._fetch_source_fp("https://blocked.example") is None


# ---------------------------------------------------------------------------
# _monitor_client
# ---------------------------------------------------------------------------

class TestMonitorClient:
    def _common_patches(self, news_changed=False, fp="newfp"):
        return [
            patch.object(pipeline, "_client_news_changed", AsyncMock(return_value=news_changed)),
            patch.object(pipeline, "_fetch_source_fp", AsyncMock(return_value=fp)),
            patch.object(pipeline, "_fire_news_research", AsyncMock(return_value=42)),
            patch.object(pipeline, "_maybe_escalate_match", AsyncMock(return_value=False)),
        ]

    @pytest.mark.asyncio
    async def test_first_fp_is_baseline_not_change(self):
        db_patch, db = _patch_db()
        client = _client(monitored_sources=[{"url": "https://a/news"}], sources_discovered_at="x")
        with db_patch, _patch_config():
            ps = self._common_patches()
            with ps[0], ps[1], ps[2] as fire, ps[3]:
                summary = await pipeline._monitor_client(1, client)
        assert summary["changed"] == []
        fire.assert_not_awaited()
        saved = db.update_client_metadata.await_args.args[2]
        assert saved["monitored_sources"][0]["last_fp"] == "newfp"

    @pytest.mark.asyncio
    async def test_focus_change_fires_research_and_clears_flag(self):
        db_patch, db = _patch_db()
        client = _client(is_focus=True, sources_discovered_at="x",
                         monitored_sources=[{"url": "https://a/news", "label": "Newsroom", "last_fp": "old"}])
        with db_patch, _patch_config():
            ps = self._common_patches()
            with ps[0], ps[1], ps[2] as fire, ps[3] as esc:
                summary = await pipeline._monitor_client(1, client)
        assert summary["changed"] == ["Newsroom"]
        assert summary["researched"] is True
        fire.assert_awaited_once()
        esc.assert_awaited_once_with(1, "Acme", 42)
        saved = db.update_client_metadata.await_args.args[2]
        assert saved["news_pending"] is False

    @pytest.mark.asyncio
    async def test_nonfocus_change_sets_pending_no_research(self):
        db_patch, db = _patch_db()
        client = _client(is_focus=False, sources_discovered_at="x",
                         monitored_sources=[{"url": "https://a/news", "label": "Newsroom", "last_fp": "old"}])
        with db_patch, _patch_config():
            ps = self._common_patches()
            with ps[0], ps[1], ps[2] as fire, ps[3]:
                summary = await pipeline._monitor_client(1, client)
        assert summary["flagged"] is True
        fire.assert_not_awaited()
        saved = db.update_client_metadata.await_args.args[2]
        assert saved["news_pending"] is True
        assert saved["news_pending_reason"] == ["Newsroom"]

    @pytest.mark.asyncio
    async def test_news_search_change_needs_existing_baseline(self):
        db_patch, _ = _patch_db()
        # news gate says "changed" but there was no stored news_fp → baseline, not change
        client = _client(sources_discovered_at="x")
        with db_patch, _patch_config():
            ps = self._common_patches(news_changed=True)
            with ps[0], ps[1], ps[2], ps[3]:
                summary = await pipeline._monitor_client(1, client)
        assert summary["changed"] == []

        client2 = _client(sources_discovered_at="x", news_fp="had-one")
        db_patch2, _ = _patch_db()
        with db_patch2, _patch_config():
            ps = self._common_patches(news_changed=True)
            with ps[0], ps[1], ps[2], ps[3]:
                summary2 = await pipeline._monitor_client(1, client2)
        assert "news search" in summary2["changed"]


# ---------------------------------------------------------------------------
# _maybe_escalate_match
# ---------------------------------------------------------------------------

class TestEscalation:
    def _patch_signals(self, max_rel):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value={"max_rel": max_rel})
        acquire = MagicMock()
        acquire.__aenter__ = AsyncMock(return_value=conn)
        acquire.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=acquire)
        db = MagicMock()
        db._pool = pool
        return patch.object(pipeline, "db_module", db)

    @pytest.mark.asyncio
    async def test_escalates_at_threshold(self):
        trigger = AsyncMock()
        with self._patch_signals(4.0), _patch_config(), \
             patch("routers.agents._maybe_trigger_pain_point_research", trigger):
            assert await pipeline._maybe_escalate_match(1, "Acme", 42) is True
        trigger.assert_awaited_once_with(1, "Acme")

    @pytest.mark.asyncio
    async def test_no_escalation_below_threshold_or_no_signals(self):
        trigger = AsyncMock()
        with self._patch_signals(3.0), _patch_config(), \
             patch("routers.agents._maybe_trigger_pain_point_research", trigger):
            assert await pipeline._maybe_escalate_match(1, "Acme", 42) is False
        with self._patch_signals(None), _patch_config(), \
             patch("routers.agents._maybe_trigger_pain_point_research", trigger):
            assert await pipeline._maybe_escalate_match(1, "Acme", 42) is False
        trigger.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_run_id_no_escalation(self):
        with _patch_config():
            assert await pipeline._maybe_escalate_match(1, "Acme", None) is False


# ---------------------------------------------------------------------------
# news_pending clearing on research trigger
# ---------------------------------------------------------------------------

class TestFlagClearing:
    @pytest.mark.asyncio
    async def test_trigger_research_clears_news_pending(self):
        db = MagicMock()
        db.update_client_metadata = AsyncMock()
        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "DB_AVAILABLE", True), \
             patch.object(pipeline, "config", {"agent_service_backend": "python"}), \
             patch.object(db, "enqueue_research_task", AsyncMock(return_value=1), create=True), \
             patch.object(db, "create_agent_run", AsyncMock(return_value=1), create=True), \
             patch.object(db, "update_agent_run", AsyncMock(), create=True), \
             patch.object(db, "get_first_org", AsyncMock(return_value={"id": 1}), create=True):
            await pipeline._trigger_research("Acme", 1)
        patch_arg = db.update_client_metadata.await_args_list[0].args[2]
        assert patch_arg["news_pending"] is False


# ---------------------------------------------------------------------------
# _resolve_client_website (heuristic + LLM fallback)
# ---------------------------------------------------------------------------

def _patch_openrouter(answer):
    """Patch the LLM fallback (llm.acomplete) the website resolver consults.

    Returns (patcher, call) — call is the AsyncMock; keep a .post alias so the
    older wire-level assertions (or_client.post.assert_*) keep reading naturally.
    """
    call = AsyncMock(return_value=answer)
    holder = MagicMock()
    holder.post = call
    return patch.object(pipeline.llm, "acomplete", call), holder


class TestResolveWebsite:
    @pytest.mark.asyncio
    async def test_heuristic_match_no_llm(self):
        db_patch, db = _patch_db()
        results = [
            {"url": "https://www.linkedin.com/company/bettzeit", "title": "Bettzeit | LinkedIn"},
            {"url": "https://www.bettzeit.de/ueber-uns", "title": "Bettzeit GmbH"},
        ]
        or_patch, or_client = _patch_openrouter("should-not-be-called")
        with db_patch, _patch_config(), _searxng(results), or_patch, \
             patch.dict("os.environ", {"OPENROUTER_API_KEY": "k"}):
            website = await pipeline._resolve_client_website(1, _client("Bettzeit GmbH"))
        assert website == "https://bettzeit.de"
        or_client.post.assert_not_awaited()
        saved = db.update_client_metadata.await_args.args[2]
        assert saved == {"website": "https://bettzeit.de", "website_source": "heuristic"}

    @pytest.mark.asyncio
    async def test_llm_fallback_validated_against_candidates(self):
        db_patch, db = _patch_db()
        results = [
            {"url": "https://www.dormando.de/", "title": "Dormando — Matratzen Online Shop"},
            {"url": "https://www.kununu.com/de/bettzeit", "title": "Bettzeit als Arbeitgeber"},
        ]
        or_patch, or_client = _patch_openrouter("dormando.de")
        with db_patch, _patch_config(), _searxng(results), or_patch, \
             patch.dict("os.environ", {"OPENROUTER_API_KEY": "k"}):
            website = await pipeline._resolve_client_website(1, _client("Bettzeit GmbH"))
        assert website == "https://dormando.de"
        or_client.post.assert_awaited_once()
        saved = db.update_client_metadata.await_args.args[2]
        assert saved["website_source"] == "llm"

    @pytest.mark.asyncio
    async def test_llm_hallucinated_domain_rejected(self):
        db_patch, db = _patch_db()
        results = [{"url": "https://random-blog.example/post", "title": "irrelevant"}]
        or_patch, _ = _patch_openrouter("totally-invented.com")
        with db_patch, _patch_config(), _searxng(results), or_patch, \
             patch.dict("os.environ", {"OPENROUTER_API_KEY": "k"}):
            website = await pipeline._resolve_client_website(1, _client("Obscure GmbH"))
        assert website is None
        db.update_client_metadata.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_existing_website_untouched(self):
        db_patch, db = _patch_db()
        searx = AsyncMock()
        with db_patch, _patch_config(), patch.object(pipeline, "_searxng_results", searx):
            website = await pipeline._resolve_client_website(1, _client("Acme", website="https://acme.com"))
        assert website == "https://acme.com"
        searx.assert_not_awaited()
        db.update_client_metadata.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_discovery_resolves_missing_website_first(self):
        db_patch, _ = _patch_db()
        resolver = AsyncMock(return_value="https://acme.com")
        with db_patch, _patch_config(), _searxng([]), \
             patch.object(pipeline, "_resolve_client_website", resolver):
            await pipeline._discover_client_sources(1, _client("Acme"))
        resolver.assert_awaited_once()


class TestSimplifyCompanyName:
    def test_legal_suffixes_and_parens_stripped(self):
        simple, acr = pipeline._simplify_company_name("Deutscher Fußball-Bund e.V. (DFB)")
        assert simple == "Deutscher Fußball-Bund"
        assert acr == "dfb"

    def test_kg_chain_stripped(self):
        simple, acr = pipeline._simplify_company_name("ENTEGA Privatkunden GmbH & Co. KG")
        assert simple == "ENTEGA Privatkunden"
        assert acr == ""

    @pytest.mark.asyncio
    async def test_acronym_exact_domain_match(self):
        db_patch, db = _patch_db()
        results = [
            {"url": "https://www.fussball.de/irgendwas", "title": "Fußball"},
            {"url": "https://www.dfb.de/impressum", "title": "DFB Impressum"},
        ]
        with db_patch, _patch_config(), _searxng(results):
            website = await pipeline._resolve_client_website(
                1, _client("Deutscher Fußball-Bund e.V. (DFB)"))
        assert website == "https://dfb.de"
        saved = db.update_client_metadata.await_args.args[2]
        assert saved["website_source"] == "heuristic"


class TestResolverFalsePositives:
    @pytest.mark.asyncio
    async def test_short_domain_substring_not_auto_accepted(self):
        """dal.ca regression: 3-char domain inside a long name must not match."""
        db_patch, db = _patch_db()
        results = [{"url": "https://www.dal.ca/some-page", "title": "Dalhousie University"}]
        or_patch, or_client = _patch_openrouter("NONE")
        with db_patch, _patch_config(), _searxng(results), or_patch, \
             patch.dict("os.environ", {"OPENROUTER_API_KEY": "k"}):
            website = await pipeline._resolve_client_website(
                1, _client("DAL Deutsche Anlagen-Leasing GmbH & Co. KG"))
        assert website is None                       # LLM said NONE, heuristic stayed out
        or_client.post.assert_awaited_once()         # ...but the LLM was consulted
        db.update_client_metadata.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_long_substring_match_confirmed_by_llm(self):
        """bilfinger.com for 'Bilfinger Construction GmbH' — substring goes to LLM."""
        db_patch, db = _patch_db()
        results = [{"url": "https://www.bilfinger.com/", "title": "Bilfinger SE"}]
        or_patch, or_client = _patch_openrouter("bilfinger.com")
        with db_patch, _patch_config(), _searxng(results), or_patch, \
             patch.dict("os.environ", {"OPENROUTER_API_KEY": "k"}):
            website = await pipeline._resolve_client_website(
                1, _client("Bilfinger Construction GmbH"))
        assert website == "https://bilfinger.com"
        saved = db.update_client_metadata.await_args.args[2]
        assert saved["website_source"] == "llm"
        or_client.post.assert_awaited_once()


class TestDiscoveryMarkerStale:
    def test_no_marker_is_stale(self):
        assert pipeline._discovery_marker_stale({}) is True

    def test_fresh_marker_not_stale(self):
        from datetime import datetime, timezone
        with _patch_config(source_rediscover_days=7):
            assert pipeline._discovery_marker_stale(
                {"sources_discovered_at": datetime.now(timezone.utc).isoformat()}) is False

    def test_old_marker_is_stale(self):
        from datetime import datetime, timedelta, timezone
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        with _patch_config(source_rediscover_days=7):
            assert pipeline._discovery_marker_stale({"sources_discovered_at": old}) is True

    def test_garbage_marker_is_stale(self):
        with _patch_config(source_rediscover_days=7):
            assert pipeline._discovery_marker_stale({"sources_discovered_at": "not-a-date"}) is True
