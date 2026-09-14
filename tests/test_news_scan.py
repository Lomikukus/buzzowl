"""
tests/test_news_scan.py — news-scan search plumbing (WP1) and the WP3 news
pipeline built on top of it (Python/textonly).

WP1 covers routers.pipeline._searxng_results: categories/time_range/language
are only added to the SearXNG request when the caller passes them, and
publishedDate survives on the returned result dicts.

WP3 covers routers.pipeline: _norm_news_url, _parse_published, _news_candidates,
_existing_signal_urls, _client_news_scan, _market_news_scan — every call in
pipeline.py passes categories=/time_range= as keywords to _searxng_results, so
now that WP1 has merged those reach the real implementation; tests still patch
_searxng_results with an AsyncMock so none of this touches the network.
Source discovery (_discover_client_sources / _probe_newsroom_paths /
_harvest_links_news) is covered in
tests/test_source_monitor.py::TestDiscoverSources.
"""

import hashlib
import json
import sys
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import playbook
from routers import pipeline
from tests.test_source_monitor import _client, _patch_config, _patch_db

# A date comfortably inside the 90-day freshness window but not today, so
# tests don't silently start failing as today's date drifts forward.
_RECENT_DATE = date.today() - timedelta(days=10)
_RECENT_ISO = _RECENT_DATE.isoformat()
_RECENT_URL_DATE = _RECENT_DATE.strftime("%Y/%m/%d")


def _patch_httpx(results=None):
    """Mirrors tests/test_source_monitor.py::_patch_httpx, but for the JSON
    SearXNG endpoint: AsyncClient().get(...) resolves to a response whose
    .json() yields {"results": results}."""
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"results": results if results is not None else []}
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return patch.object(pipeline.httpx, "AsyncClient", return_value=ctx), client


class TestSearxngResultsForwardsParams:
    RESULTS = [
        {
            "url": "https://a.example/1", "title": "A", "content": "snippet",
            "engine": "bing_news", "publishedDate": "2026-09-01T00:00:00",
        },
    ]

    @pytest.mark.asyncio
    async def test_searxng_results_forwards_params(self):
        httpx_patch, client = _patch_httpx(self.RESULTS)
        with httpx_patch:
            out = await pipeline._searxng_results("acme news", limit=10)
        params = client.get.await_args.kwargs["params"]
        assert "categories" not in params
        assert "time_range" not in params
        assert "language" not in params
        assert out[0]["publishedDate"] == "2026-09-01T00:00:00"
        assert out[0]["engine"] == "bing_news"
        assert out[0]["content"] == "snippet"

        httpx_patch2, client2 = _patch_httpx(self.RESULTS)
        with httpx_patch2:
            await pipeline._searxng_results(
                "acme news", limit=10, categories="news", time_range="month", language="en",
            )
        params2 = client2.get.await_args.kwargs["params"]
        assert params2["categories"] == "news"
        assert params2["time_range"] == "month"
        assert params2["language"] == "en"


# ---------------------------------------------------------------------------
# _norm_news_url
# ---------------------------------------------------------------------------

class TestNormNewsUrl:
    def test_strips_www_tracking_params_fragment_and_trailing_slash(self):
        url = "https://WWW.Example.com/Article/?utm_source=x&utm_medium=y&fbclid=abc&gclid=def&id=1#frag"
        assert pipeline._norm_news_url(url) == "example.com/Article?id=1"

    def test_bare_url_lowercases_host_and_strips_trailing_slash(self):
        assert pipeline._norm_news_url("https://Example.com/a/") == "example.com/a"

    def test_path_case_is_preserved_only_host_is_lowercased(self):
        assert pipeline._norm_news_url("https://Example.com/A/B") == "example.com/A/B"

    def test_query_with_only_tracking_params_drops_question_mark(self):
        assert pipeline._norm_news_url("https://example.com/a?utm_source=x") == "example.com/a"

    def test_empty_url_returns_empty_string(self):
        assert pipeline._norm_news_url("") == ""

    def test_two_equivalent_urls_normalize_the_same(self):
        a = pipeline._norm_news_url("https://www.example.com/a/b?utm_campaign=z")
        b = pipeline._norm_news_url("http://example.com/a/b/")
        assert a == b


# ---------------------------------------------------------------------------
# _parse_published
# ---------------------------------------------------------------------------

class TestParsePublished:
    def test_iso_published_date(self):
        assert pipeline._parse_published({"publishedDate": "2026-08-01T10:00:00Z"}) == "2026-08-01"

    def test_date_only_published_date(self):
        assert pipeline._parse_published({"publishedDate": "2026-08-01"}) == "2026-08-01"

    def test_falls_back_to_date_embedded_in_url(self):
        r = {"url": "https://acme.com/news/2026/08/01/acme-deal"}
        assert pipeline._parse_published(r) == "2026-08-01"

    def test_dash_separated_date_in_url(self):
        r = {"url": "https://acme.com/news/2026-08-01-acme-deal"}
        assert pipeline._parse_published(r) == "2026-08-01"

    def test_no_date_anywhere_returns_none(self):
        r = {"url": "https://acme.com/news/acme-deal"}
        assert pipeline._parse_published(r) is None

    def test_garbage_published_date_falls_back_to_url(self):
        r = {"publishedDate": "not-a-date", "url": "https://acme.com/news/2026/08/01/x"}
        assert pipeline._parse_published(r) == "2026-08-01"


# ---------------------------------------------------------------------------
# _existing_signal_urls
# ---------------------------------------------------------------------------

class TestExistingSignalUrls:
    @pytest.mark.asyncio
    async def test_only_signal_docs_with_a_source_url_are_returned_normalized(self):
        docs = [
            {"type": "signal", "metadata": {"source_url": "https://acme.com/news/1?utm_source=x"}},
            {"type": "signal", "metadata": {}},                                        # no source_url
            {"type": "finding", "metadata": {"source_url": "https://acme.com/other"}},  # wrong type
        ]
        db = MagicMock()
        db.list_documents = AsyncMock(return_value=docs)
        with patch.object(pipeline, "db_module", db):
            urls = await pipeline._existing_signal_urls(1, 42)
        assert urls == {"acme.com/news/1"}
        db.list_documents.assert_awaited_once_with(1, client_id=42)

    @pytest.mark.asyncio
    async def test_db_failure_returns_empty_set_not_raise(self):
        db = MagicMock()
        db.list_documents = AsyncMock(side_effect=RuntimeError("db down"))
        with patch.object(pipeline, "db_module", db):
            urls = await pipeline._existing_signal_urls(1, 42)
        assert urls == set()


# ---------------------------------------------------------------------------
# _news_candidates
# ---------------------------------------------------------------------------

class TestNewsCandidates:
    @pytest.mark.asyncio
    async def test_undated_third_party_dropped_own_domain_url_date_kept(self):
        client = _client("Acme GmbH", website="https://www.acme.com")
        kept_url = f"https://acme.com/news/{_RECENT_URL_DATE}/acme-deal"

        async def fake_searxng(query, limit=10, *, categories=None, time_range=None, language=None):
            if query.startswith('"Acme GmbH"') and not query.startswith("site:"):
                # No publishedDate and no date in the URL → must be dropped.
                return [{"url": "https://thirdparty.example/some-acme-news", "title": "Acme mentioned"}]
            if query.startswith("site:acme.com"):
                # Own-domain, no publishedDate, but a date embedded in the URL.
                return [{"url": kept_url, "title": "Acme deal"}]
            return []

        with patch.object(pipeline, "_searxng_results", AsyncMock(side_effect=fake_searxng)):
            candidates = await pipeline._news_candidates(1, client)

        urls = [c["url"] for c in candidates]
        assert kept_url in urls
        assert all("thirdparty" not in u for u in urls)

    @pytest.mark.asyncio
    async def test_skip_host_result_dropped_even_if_dated(self):
        client = _client("Acme GmbH")

        async def fake_searxng(query, limit=10, *, categories=None, time_range=None, language=None):
            return [{"url": "https://www.linkedin.com/posts/acme-update",
                     "publishedDate": _RECENT_ISO, "title": "Acme on LinkedIn"}]

        with patch.object(pipeline, "_searxng_results", AsyncMock(side_effect=fake_searxng)):
            candidates = await pipeline._news_candidates(1, client)
        assert candidates == []

    @pytest.mark.asyncio
    async def test_dedupes_same_normalized_url_across_queries(self):
        client = _client("Acme GmbH", industry="Retail")
        same = {"url": "https://news.example/acme-story?utm_source=x", "publishedDate": _RECENT_ISO,
                "title": "Acme story"}

        with patch.object(pipeline, "_searxng_results", AsyncMock(return_value=[same])):
            candidates = await pipeline._news_candidates(1, client)
        # Same normalized URL comes back from every query (name, name+industry) — kept once.
        assert len(candidates) == 1

    @pytest.mark.asyncio
    async def test_older_than_90_days_dropped(self):
        client = _client("Acme GmbH")
        old = [{"url": "https://news.example/old-story", "publishedDate": "2020-01-01", "title": "Old"}]
        with patch.object(pipeline, "_searxng_results", AsyncMock(return_value=old)):
            candidates = await pipeline._news_candidates(1, client)
        assert candidates == []

    @pytest.mark.asyncio
    async def test_total_searxng_outage_raises(self):
        client = _client("Acme GmbH")
        with patch.object(pipeline, "_searxng_results", AsyncMock(side_effect=ConnectionError("down"))):
            with pytest.raises(Exception):
                await pipeline._news_candidates(1, client)

    @pytest.mark.asyncio
    async def test_partial_searxng_failure_does_not_raise(self):
        client = _client("Acme GmbH", industry="Retail")
        good = [{"url": "https://news.example/acme-story", "publishedDate": _RECENT_ISO, "title": "Acme"}]

        calls = {"n": 0}

        async def flaky(query, limit=10, *, categories=None, time_range=None, language=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("down")
            return good

        with patch.object(pipeline, "_searxng_results", AsyncMock(side_effect=flaky)):
            candidates = await pipeline._news_candidates(1, client)
        assert len(candidates) == 1

    @pytest.mark.asyncio
    async def test_forwards_categories_and_time_range_to_searxng(self):
        """The exact-name query must ask SearXNG for the news category over
        the last month — that's what makes the pipeline see fresh, dated
        results at all once WP1's _searxng_results actually honors them."""
        client = _client("Acme GmbH")
        search_mock = AsyncMock(return_value=[])
        with patch.object(pipeline, "_searxng_results", search_mock):
            await pipeline._news_candidates(1, client)
        first_call = search_mock.await_args_list[0]
        assert first_call.kwargs["categories"] == "news"
        assert first_call.kwargs["time_range"] == "month"


# ---------------------------------------------------------------------------
# _client_news_scan
# ---------------------------------------------------------------------------

class TestClientNewsScan:
    @pytest.mark.asyncio
    async def test_existing_signal_urls_are_not_rescored(self):
        client = _client("Acme GmbH", website="https://www.acme.com")
        cand = [{"url": "https://acme.com/news/1", "title": "t", "content": "c",
                 "_norm_url": "acme.com/news/1", "_published": _RECENT_ISO, "query": "q"}]

        db = MagicMock()
        db.list_documents = AsyncMock(return_value=[
            {"type": "signal", "metadata": {"source_url": "https://acme.com/news/1"}},
        ])
        llm_mock = AsyncMock()
        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "_news_candidates", AsyncMock(return_value=cand)), \
             patch.object(pipeline.llm, "acomplete", llm_mock):
            result = await pipeline._client_news_scan(1, client)

        assert result == {"found": 0, "scored": 0, "written": 0, "max_relevance": 0, "error": None}
        llm_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_writes_signal_with_deterministic_doc_id_and_metadata(self):
        client = _client("Acme GmbH", website="https://www.acme.com")
        norm = "acme.com/news/1"
        cand = [{"url": "https://acme.com/news/1", "title": "Acme wins deal", "content": "c",
                 "_norm_url": norm, "_published": _RECENT_ISO, "query": '"Acme GmbH"'}]
        expected_doc_id = f"news-1-{hashlib.sha1(norm.encode()).hexdigest()[:10]}"
        reply = json.dumps([{"i": 0, "relevance": 4, "signal_type": "opportunity",
                              "headline": "Acme wins deal", "why": "Confirmed contract win"}])

        db = MagicMock()
        db.list_documents = AsyncMock(return_value=[])
        db.index_document = AsyncMock(return_value=101)
        db.link_document = AsyncMock()
        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "_news_candidates", AsyncMock(return_value=cand)), \
             patch.object(pipeline.llm, "acomplete", AsyncMock(return_value=reply)):
            result = await pipeline._client_news_scan(1, client, run_id=55)

        assert result["found"] == 1
        assert result["written"] == 1
        assert result["max_relevance"] == 4
        assert result["error"] is None

        db.index_document.assert_awaited_once()
        kwargs = db.index_document.await_args.kwargs
        assert kwargs["doc_id"] == expected_doc_id
        assert kwargs["doc_type"] == "signal"
        assert kwargs["agent_run_id"] == 55
        assert kwargs["source"] == "agent"
        meta = kwargs["metadata"]
        assert meta["source_url"] == "https://acme.com/news/1"
        assert meta["published_at"] == _RECENT_ISO
        assert meta["signal_type"] == "opportunity"
        assert meta["relevance_score"] == 4
        assert meta["subject"] == "Acme GmbH"
        assert meta["from_news_scan"] is True
        assert meta["service"] == "python"
        db.link_document.assert_awaited_once_with(101, "client", 1)

    @pytest.mark.asyncio
    async def test_low_relevance_not_written(self):
        client = _client("Acme GmbH")
        cand = [{"url": "https://acme.com/news/1", "title": "t", "content": "c",
                 "_norm_url": "acme.com/news/1", "_published": _RECENT_ISO, "query": "q"}]
        reply = json.dumps([{"i": 0, "relevance": 1, "signal_type": "news",
                              "headline": "h", "why": "namesake, not this Acme"}])

        db = MagicMock()
        db.list_documents = AsyncMock(return_value=[])
        db.index_document = AsyncMock()
        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "_news_candidates", AsyncMock(return_value=cand)), \
             patch.object(pipeline.llm, "acomplete", AsyncMock(return_value=reply)):
            result = await pipeline._client_news_scan(1, client)

        assert result["written"] == 0
        db.index_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_max_write_caps_signals_written(self):
        client = _client("Acme GmbH")
        cand = [
            {"url": f"https://acme.com/news/{i}", "title": f"t{i}", "content": "c",
             "_norm_url": f"acme.com/news/{i}", "_published": _RECENT_ISO, "query": "q"}
            for i in range(5)
        ]
        reply = json.dumps([
            {"i": i, "relevance": 4, "signal_type": "news", "headline": f"h{i}", "why": "w"}
            for i in range(5)
        ])
        db = MagicMock()
        db.list_documents = AsyncMock(return_value=[])
        db.index_document = AsyncMock(return_value=101)
        db.link_document = AsyncMock()
        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "_news_candidates", AsyncMock(return_value=cand)), \
             patch.object(pipeline.llm, "acomplete", AsyncMock(return_value=reply)):
            result = await pipeline._client_news_scan(1, client, max_write=2)
        assert result["written"] == 2
        assert db.index_document.await_count == 2

    @pytest.mark.asyncio
    async def test_searxng_down_retries_once_then_errors_nothing_written(self):
        client = _client("Acme GmbH")
        db = MagicMock()
        db.index_document = AsyncMock()
        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "_news_candidates",
                          AsyncMock(side_effect=ConnectionError("down"))) as candidates_mock, \
             patch.object(pipeline.asyncio, "sleep", AsyncMock()) as sleep_mock:
            result = await pipeline._client_news_scan(1, client)

        assert result["error"] == "searxng unreachable"
        assert result["written"] == 0
        db.index_document.assert_not_awaited()
        sleep_mock.assert_awaited_once_with(20)
        assert candidates_mock.await_count == 2   # one retry, per the plan

    @pytest.mark.asyncio
    async def test_llm_failure_writes_nothing_and_sets_error(self):
        client = _client("Acme GmbH")
        cand = [{"url": "https://acme.com/news/1", "title": "t", "content": "c",
                 "_norm_url": "acme.com/news/1", "_published": _RECENT_ISO, "query": "q"}]
        db = MagicMock()
        db.list_documents = AsyncMock(return_value=[])
        db.index_document = AsyncMock()
        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "_news_candidates", AsyncMock(return_value=cand)), \
             patch.object(pipeline.llm, "acomplete", AsyncMock(side_effect=RuntimeError("boom"))):
            result = await pipeline._client_news_scan(1, client)

        assert result["written"] == 0
        assert result["error"]
        db.index_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_fresh_candidates_short_circuits_without_llm_call(self):
        client = _client("Acme GmbH")
        db = MagicMock()
        db.list_documents = AsyncMock(return_value=[])
        llm_mock = AsyncMock()
        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "_news_candidates", AsyncMock(return_value=[])), \
             patch.object(pipeline.llm, "acomplete", llm_mock):
            result = await pipeline._client_news_scan(1, client)
        assert result["found"] == 0
        llm_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# WP4: approved cross-site lessons (scope='news') inlined into
# _NEWS_SCORE_PROMPT via _rules_block, for both scan flavors.
# ---------------------------------------------------------------------------

def _fake_playbook_with_lessons(lessons):
    """A fake `playbook` module for sys.modules — lessons_load returns the
    fixture, lessons_block is the REAL function so scope/status filtering is
    exercised end to end, not just the plumbing that calls it."""
    fake = MagicMock()
    fake.lessons_load = AsyncMock(return_value=lessons)
    fake.lessons_block = playbook.lessons_block
    return fake


class TestLessonsInClientNewsScan:
    @pytest.mark.asyncio
    async def test_approved_news_lesson_appears_proposed_does_not(self, monkeypatch):
        lessons = [
            {"text": "Discount press-release wire copy with no named source", "scope": "news", "status": "approved"},
            {"text": "A merely proposed news lesson", "scope": "news", "status": "proposed"},
            {"text": "An approved but jobs-scoped lesson", "scope": "jobs", "status": "approved"},
        ]
        monkeypatch.setitem(sys.modules, "playbook", _fake_playbook_with_lessons(lessons))
        client = _client("Acme GmbH")
        cand = [{"url": "https://acme.com/news/1", "title": "t", "content": "c",
                 "_norm_url": "acme.com/news/1", "_published": _RECENT_ISO, "query": "q"}]
        db = MagicMock()
        db.list_documents = AsyncMock(return_value=[])
        acomplete = AsyncMock(return_value=json.dumps([]))
        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "_news_candidates", AsyncMock(return_value=cand)), \
             patch.object(pipeline.llm, "acomplete", acomplete):
            await pipeline._client_news_scan(1, client)
        acomplete.assert_awaited_once()
        prompt = acomplete.await_args.args[0]
        assert "Discount press-release wire copy with no named source" in prompt
        assert "A merely proposed news lesson" not in prompt
        assert "An approved but jobs-scoped lesson" not in prompt

    @pytest.mark.asyncio
    async def test_unchanged_when_org_has_no_lessons_document(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "playbook", _fake_playbook_with_lessons([]))
        client = _client("Acme GmbH")
        cand = [{"url": "https://acme.com/news/1", "title": "t", "content": "c",
                 "_norm_url": "acme.com/news/1", "_published": _RECENT_ISO, "query": "q"}]
        db = MagicMock()
        db.list_documents = AsyncMock(return_value=[])
        acomplete = AsyncMock(return_value=json.dumps([]))
        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "_news_candidates", AsyncMock(return_value=cand)), \
             patch.object(pipeline.llm, "acomplete", acomplete):
            await pipeline._client_news_scan(1, client)
        prompt = acomplete.await_args.args[0]
        assert "Learned rules" not in prompt
        assert prompt == pipeline._NEWS_SCORE_PROMPT.format(
            subject="Acme GmbH", n=1, listing=pipeline._news_listing(cand),
            rules=pipeline._rules_block(""),
        )


# ---------------------------------------------------------------------------
# _market_news_scan
# ---------------------------------------------------------------------------

class TestMarketNewsScan:
    @pytest.mark.asyncio
    async def test_writes_market_scoped_signal_without_linking(self):
        cand_url = "https://reuters.com/industry-update"
        norm = pipeline._norm_news_url(cand_url)
        results = [{"url": cand_url, "title": "Industry update", "content": "c",
                    "publishedDate": _RECENT_ISO}]
        reply = json.dumps([{"i": 0, "relevance": 3, "signal_type": "risk",
                              "headline": "Industry update", "why": "New regulation announced"}])

        db = MagicMock()
        db.list_signals = AsyncMock(return_value=[])
        db.index_document = AsyncMock(return_value=202)
        db.link_document = AsyncMock()
        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "_searxng_results", AsyncMock(return_value=results)), \
             patch.object(pipeline.llm, "acomplete", AsyncMock(return_value=reply)):
            result = await pipeline._market_news_scan(1, "Automotive", "the Automotive sector", run_id=9)

        assert result["written"] == 1
        db.index_document.assert_awaited_once()
        kwargs = db.index_document.await_args.kwargs
        assert kwargs["metadata"]["scope"] == "market"
        assert kwargs["metadata"]["industry"] == "Automotive"
        assert kwargs["agent_run_id"] == 9
        assert kwargs["doc_id"] == f"market-news-{hashlib.sha1(norm.encode()).hexdigest()[:10]}"
        db.link_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dedupes_against_existing_market_signals(self):
        cand_url = "https://reuters.com/industry-update"
        results = [{"url": cand_url, "title": "Industry update", "content": "c",
                    "publishedDate": _RECENT_ISO}]

        db = MagicMock()
        db.list_signals = AsyncMock(return_value=[{"metadata": {"source_url": cand_url}}])
        db.index_document = AsyncMock()
        llm_mock = AsyncMock()
        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "_searxng_results", AsyncMock(return_value=results)), \
             patch.object(pipeline.llm, "acomplete", llm_mock):
            result = await pipeline._market_news_scan(1, "Automotive", "the Automotive sector")

        assert result["found"] == 0
        llm_mock.assert_not_awaited()
        db.index_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_falls_back_to_focus_text_when_industry_blank(self):
        """Source-change-triggered scans pass industry='' — the free-text focus
        description drives the search terms instead."""
        results = []
        search_mock = AsyncMock(return_value=results)
        db = MagicMock()
        db.list_signals = AsyncMock(return_value=[])
        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "_searxng_results", search_mock):
            result = await pipeline._market_news_scan(
                1, "", "general business and economics news (triggered by an update on X)",
            )
        assert result["found"] == 0
        queried = search_mock.await_args_list[0].args[0]
        assert "general business and economics news" in queried

    @pytest.mark.asyncio
    async def test_max_write_caps_signals_written(self):
        results = [
            {"url": f"https://reuters.com/story-{i}", "title": f"Story {i}", "content": "c",
             "publishedDate": _RECENT_ISO}
            for i in range(5)
        ]
        reply = json.dumps([
            {"i": i, "relevance": 4, "signal_type": "news", "headline": f"h{i}", "why": "w"}
            for i in range(5)
        ])
        db = MagicMock()
        db.list_signals = AsyncMock(return_value=[])
        db.index_document = AsyncMock(return_value=1)
        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "_searxng_results", AsyncMock(return_value=results)), \
             patch.object(pipeline.llm, "acomplete", AsyncMock(return_value=reply)):
            result = await pipeline._market_news_scan(1, "Automotive", "the Automotive sector", max_write=2)
        assert result["written"] == 2
        assert db.index_document.await_count == 2


class TestLessonsInMarketNewsScan:
    @pytest.mark.asyncio
    async def test_approved_news_lesson_appears_proposed_does_not(self, monkeypatch):
        lessons = [
            {"text": "Discount press-release wire copy with no named source", "scope": "news", "status": "approved"},
            {"text": "A merely proposed news lesson", "scope": "news", "status": "proposed"},
            {"text": "An approved but jobs-scoped lesson", "scope": "jobs", "status": "approved"},
        ]
        monkeypatch.setitem(sys.modules, "playbook", _fake_playbook_with_lessons(lessons))
        results = [{"url": "https://reuters.com/industry-update", "title": "Industry update", "content": "c",
                    "publishedDate": _RECENT_ISO}]
        db = MagicMock()
        db.list_signals = AsyncMock(return_value=[])
        acomplete = AsyncMock(return_value=json.dumps([]))
        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "_searxng_results", AsyncMock(return_value=results)), \
             patch.object(pipeline.llm, "acomplete", acomplete):
            await pipeline._market_news_scan(1, "Automotive", "the Automotive sector")
        acomplete.assert_awaited_once()
        prompt = acomplete.await_args.args[0]
        assert "Discount press-release wire copy with no named source" in prompt
        assert "A merely proposed news lesson" not in prompt
        assert "An approved but jobs-scoped lesson" not in prompt

    @pytest.mark.asyncio
    async def test_unchanged_when_org_has_no_lessons_document(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "playbook", _fake_playbook_with_lessons([]))
        results = [{"url": "https://reuters.com/industry-update", "title": "Industry update", "content": "c",
                    "publishedDate": _RECENT_ISO}]
        db = MagicMock()
        db.list_signals = AsyncMock(return_value=[])
        acomplete = AsyncMock(return_value=json.dumps([]))
        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "_searxng_results", AsyncMock(return_value=results)), \
             patch.object(pipeline.llm, "acomplete", acomplete):
            await pipeline._market_news_scan(1, "Automotive", "the Automotive sector")
        prompt = acomplete.await_args.args[0]
        assert "Learned rules" not in prompt
