"""
tests/test_news_scan.py — news-scan search plumbing (WP1), the WP3 news
pipeline built on top of it (Python/textonly), and the WP9 degraded-backend
detection + own-newsroom tier layered on top of that.

WP1 covers routers.pipeline._searxng_query / _searxng_results: categories/
time_range/language are only added to the SearXNG request when the caller
passes them, publishedDate survives on the returned result dicts, and
`unresponsive_engines` in SearXNG's own JSON survives as _searxng_query's
"unresponsive" key.

WP3 covers routers.pipeline: _norm_news_url, _parse_published,
_existing_signal_urls, _client_news_scan, _market_news_scan.

WP9 covers the fix for a live test drive finding "news scan wrote 0-1
signals per client and reported error: null although nearly every SearXNG
engine was suspended and the results that did come back carried no
publishedDate": _news_candidates now uses _searxng_query (not
_searxng_results) so degraded-backend signals (unresponsive engines, an
all-zero-hit news-category query, or 100% undated results even after a
page-header probe) reach _client_news_scan/_market_news_scan, which set
result["error"] instead of silently reporting found=0/error=None. It also
covers the own-newsroom tier (_newsroom_candidates): harvesting dated items
straight off a client's own newsroom pages, backend-independent of SearXNG,
which can save a scan from failing (result["warning"] instead of
result["error"]) when it alone finds >=3 candidates.

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


def _patch_httpx(results=None, unresponsive_engines=None):
    """Mirrors tests/test_source_monitor.py::_patch_httpx, but for the JSON
    SearXNG endpoint: AsyncClient().get(...) resolves to a response whose
    .json() yields {"results": results, "unresponsive_engines": [...]}."""
    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        "results": results if results is not None else [],
        "unresponsive_engines": unresponsive_engines or [],
    }
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return patch.object(pipeline.httpx, "AsyncClient", return_value=ctx), client


def _qdata(results=None, unresponsive=None, engines_ok=0):
    """The dict shape _searxng_query returns — the standard way tests mock
    it at the function level (as opposed to _patch_httpx, which mocks the
    transport underneath the real _searxng_query implementation)."""
    return {"results": results or [], "unresponsive": unresponsive or [], "engines_ok": engines_ok}


def _news_data(candidates=None, unresponsive=None, undated_total=0, news_zero_all=False):
    """The dict shape _news_candidates returns, for mocking it at the
    function level in _client_news_scan tests."""
    return {
        "candidates": candidates or [],
        "unresponsive": unresponsive or [],
        "undated_total": undated_total,
        "news_zero_all": news_zero_all,
    }


def _no_newsroom():
    return patch.object(pipeline, "_newsroom_candidates", AsyncMock(return_value=([], [])))


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
        assert isinstance(out, list)
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
# _searxng_query — degraded-backend plumbing (WP9)
# ---------------------------------------------------------------------------

class TestSearxngQueryDegraded:
    @pytest.mark.asyncio
    async def test_unresponsive_engines_and_engines_ok_surface(self):
        results = [{"url": "https://a.example/1", "title": "A", "engine": "bing news"}]
        unresponsive_engines = [
            ["brave.news", "Suspended: too many requests"],
            ["startpage news", "Suspended: CAPTCHA"],
        ]
        httpx_patch, _ = _patch_httpx(results, unresponsive_engines=unresponsive_engines)
        with httpx_patch:
            data = await pipeline._searxng_query("acme news", limit=10, categories="news")
        assert data["results"] == results
        assert data["unresponsive"] == unresponsive_engines
        assert data["engines_ok"] == 1

    @pytest.mark.asyncio
    async def test_no_unresponsive_engines_key_defaults_empty(self):
        httpx_patch, _ = _patch_httpx([{"url": "https://a.example/1"}])
        with httpx_patch:
            data = await pipeline._searxng_query("q")
        assert data["unresponsive"] == []

    @pytest.mark.asyncio
    async def test_malformed_unresponsive_entries_are_dropped(self):
        httpx_patch, _ = _patch_httpx([], unresponsive_engines=[["only-one-field"], ["ok", "reason"], "not-a-pair"])
        with httpx_patch:
            data = await pipeline._searxng_query("q")
        assert data["unresponsive"] == [["ok", "reason"]]

    @pytest.mark.asyncio
    async def test_searxng_results_wrapper_still_returns_a_plain_list(self):
        results = [{"url": "https://a.example/1", "title": "A"}]
        httpx_patch, _ = _patch_httpx(results, unresponsive_engines=[["brave", "down"]])
        with httpx_patch:
            out = await pipeline._searxng_results("q")
        assert isinstance(out, list)
        assert out == results


class TestDedupeUnresponsive:
    def test_first_seen_wins_dedupe_by_engine(self):
        pairs = [["brave", "r1"], ["brave", "r2"], ["startpage", "r3"]]
        assert pipeline._dedupe_unresponsive(pairs) == [["brave", "r1"], ["startpage", "r3"]]

    def test_ignores_malformed_entries(self):
        assert pipeline._dedupe_unresponsive([["brave"], "oops", None, ["ok", "reason"]]) == [["ok", "reason"]]

    def test_empty_input(self):
        assert pipeline._dedupe_unresponsive(None) == []
        assert pipeline._dedupe_unresponsive([]) == []


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
# Date-recovery helpers (WP9 step 3: bounded page-header probe)
# ---------------------------------------------------------------------------

class TestCoerceIsoDate:
    def test_full_iso_datetime(self):
        assert pipeline._coerce_iso_date("2026-09-12T10:00:00Z") == "2026-09-12"

    def test_bare_iso_date(self):
        assert pipeline._coerce_iso_date("2026-09-12") == "2026-09-12"

    def test_embedded_url_style_date(self):
        assert pipeline._coerce_iso_date("published 2026/09/12 article") == "2026-09-12"

    def test_garbage_returns_none(self):
        assert pipeline._coerce_iso_date("not a date") is None

    def test_empty_returns_none(self):
        assert pipeline._coerce_iso_date("") is None


class TestExtractPublishedFromHtml:
    def test_meta_property_published_time(self):
        html = '<meta property="article:published_time" content="2026-09-12T08:00:00Z">'
        assert pipeline._extract_published_from_html(html) == "2026-09-12"

    def test_meta_name_date(self):
        html = '<meta name="date" content="2026-09-11">'
        assert pipeline._extract_published_from_html(html) == "2026-09-11"

    def test_time_tag_datetime_attr(self):
        html = '<time datetime="2026-09-10">10 Sept</time>'
        assert pipeline._extract_published_from_html(html) == "2026-09-10"

    def test_jsonld_date_published(self):
        html = '<script type="application/ld+json">{"@type":"NewsArticle","datePublished":"2026-09-09"}</script>'
        assert pipeline._extract_published_from_html(html) == "2026-09-09"

    def test_nothing_returns_none(self):
        assert pipeline._extract_published_from_html("<html><body>no dates here</body></html>") is None


class TestProbePublishedDate:
    @pytest.mark.asyncio
    async def test_plain_get_finds_meta_date(self):
        html = '<meta property="article:published_time" content="2026-09-12">'
        resp = MagicMock(status_code=200, text=html)
        client = MagicMock()
        client.get = AsyncMock(return_value=resp)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=client)
        ctx.__aexit__ = AsyncMock(return_value=False)
        with patch.object(pipeline.httpx, "AsyncClient", return_value=ctx):
            result = await pipeline._probe_published_date("https://acme.com/article")
        assert result == "2026-09-12"

    @pytest.mark.asyncio
    async def test_non_200_returns_none(self):
        resp = MagicMock(status_code=404, text="")
        client = MagicMock()
        client.get = AsyncMock(return_value=resp)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=client)
        ctx.__aexit__ = AsyncMock(return_value=False)
        with patch.object(pipeline.httpx, "AsyncClient", return_value=ctx):
            result = await pipeline._probe_published_date("https://acme.com/article")
        assert result is None

    @pytest.mark.asyncio
    async def test_transport_exception_returns_none(self):
        with patch.object(pipeline.httpx, "AsyncClient", side_effect=RuntimeError("boom")):
            result = await pipeline._probe_published_date("https://acme.com/article")
        assert result is None


# ---------------------------------------------------------------------------
# _extract_date_near (WP9 review BLOCKER 1)
# ---------------------------------------------------------------------------

def _anchor_dates(html: str) -> dict:
    """href -> _extract_date_near(...) for every <a> in html, exactly how
    _newsroom_candidates drives it (m.end() of the whole anchor match)."""
    out = {}
    for m in pipeline._ANCHOR_RE.finditer(html):
        out[m.group(1)] = pipeline._extract_date_near(html, m.end())
    return out


class TestExtractDateNear:
    def test_two_adjacent_compact_items_keep_their_own_dates(self):
        """The original bug: an unbounded +-300-char window let the FIRST
        <time> anywhere nearby win for every anchor. Three compact,
        back-to-back items (no padding between them) must each resolve
        their own trailing marker, in three different formats."""
        html = (
            '<a href="/a">A</a> 12.09.2026'
            '<a href="/b">B</a> 2026-09-10'
            '<a href="/c">C</a> <time datetime="2026-09-08"></time>'
        )
        dates = _anchor_dates(html)
        assert dates["/a"] == "2026-09-12"
        assert dates["/b"] == "2026-09-10"
        assert dates["/c"] == "2026-09-08"

    def test_undated_item_between_two_dated_ones_gets_no_date(self):
        html = (
            '<a href="/a">A</a> 12.09.2026'
            '<a href="/b">B has no date of its own</a>'
            '<a href="/c">C</a> 10.09.2026'
        )
        dates = _anchor_dates(html)
        assert dates["/a"] == "2026-09-12"
        assert dates["/b"] is None
        assert dates["/c"] == "2026-09-10"

    def test_neighbours_trailing_date_never_leaks_backward(self):
        """A lone anchor with a dated neighbour right before it, and
        nothing of its own after it, must not inherit the neighbour's
        date — this is the exact bleed the original window bug caused."""
        html = '<a href="/a">A</a> 12.09.2026<a href="/b">B</a>'
        dates = _anchor_dates(html)
        assert dates["/a"] == "2026-09-12"
        assert dates["/b"] is None

    def test_dated_200_days_ago_resolves_but_is_later_dropped_by_age(self):
        """_extract_date_near itself has no age opinion — the 90-day gate
        lives in _newsroom_candidates. This just confirms an old date is
        still read correctly (so the age filter has something to reject)."""
        html = '<a href="/old">Old</a> 01.01.2020'
        dates = _anchor_dates(html)
        assert dates["/old"] == "2020-01-01"

    def test_next_anchor_bounds_the_forward_search(self):
        """A date belonging to a THIRD item, two anchors away, must never
        be visible to the first, even though a flat 300-char window would
        easily have reached it (the original bug) — the forward search
        must stop at the very next `<a`, not merely somewhere within
        `window` chars."""
        html = '<a href="/a">A</a><a href="/b">B</a><a href="/c">C</a> 12.09.2026'
        dates = _anchor_dates(html)
        assert dates["/a"] is None
        assert dates["/b"] is None
        assert dates["/c"] == "2026-09-12"

    def test_no_next_anchor_still_bounded_by_window(self):
        html = '<a href="/a">A</a> 12.09.2026'
        assert _anchor_dates(html)["/a"] == "2026-09-12"


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

        async def fake_query(query, limit=10, *, categories=None, time_range=None, language=None):
            if query.startswith('"Acme GmbH"') and not query.startswith("site:"):
                # No publishedDate and no date in the URL → must be dropped
                # (the page probe below is mocked to also find nothing).
                return _qdata([{"url": "https://thirdparty.example/some-acme-news", "title": "Acme mentioned"}])
            if query.startswith("site:acme.com"):
                # Own-domain, no publishedDate, but a date embedded in the URL.
                return _qdata([{"url": kept_url, "title": "Acme deal"}])
            return _qdata([])

        with patch.object(pipeline, "_searxng_query", AsyncMock(side_effect=fake_query)), \
             patch.object(pipeline, "_probe_published_date", AsyncMock(return_value=None)):
            data = await pipeline._news_candidates(1, client)

        urls = [c["url"] for c in data["candidates"]]
        assert kept_url in urls
        assert all("thirdparty" not in u for u in urls)

    @pytest.mark.asyncio
    async def test_skip_host_result_dropped_even_if_dated(self):
        client = _client("Acme GmbH")

        async def fake_query(query, limit=10, *, categories=None, time_range=None, language=None):
            return _qdata([{"url": "https://www.linkedin.com/posts/acme-update",
                             "publishedDate": _RECENT_ISO, "title": "Acme on LinkedIn"}])

        with patch.object(pipeline, "_searxng_query", AsyncMock(side_effect=fake_query)):
            data = await pipeline._news_candidates(1, client)
        assert data["candidates"] == []

    @pytest.mark.asyncio
    async def test_dedupes_same_normalized_url_across_queries(self):
        client = _client("Acme GmbH", industry="Retail")
        same = {"url": "https://news.example/acme-story?utm_source=x", "publishedDate": _RECENT_ISO,
                "title": "Acme story"}

        with patch.object(pipeline, "_searxng_query", AsyncMock(return_value=_qdata([same]))):
            data = await pipeline._news_candidates(1, client)
        # Same normalized URL comes back from every query (name, name+industry) — kept once.
        assert len(data["candidates"]) == 1

    @pytest.mark.asyncio
    async def test_older_than_90_days_dropped(self):
        client = _client("Acme GmbH")
        old = [{"url": "https://news.example/old-story", "publishedDate": "2020-01-01", "title": "Old"}]
        with patch.object(pipeline, "_searxng_query", AsyncMock(return_value=_qdata(old))):
            data = await pipeline._news_candidates(1, client)
        assert data["candidates"] == []

    @pytest.mark.asyncio
    async def test_total_searxng_outage_raises(self):
        client = _client("Acme GmbH")
        with patch.object(pipeline, "_searxng_query", AsyncMock(side_effect=ConnectionError("down"))):
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
            return _qdata(good)

        with patch.object(pipeline, "_searxng_query", AsyncMock(side_effect=flaky)):
            data = await pipeline._news_candidates(1, client)
        assert len(data["candidates"]) == 1

    @pytest.mark.asyncio
    async def test_forwards_categories_and_time_range_to_searxng(self):
        """The exact-name query must ask SearXNG for the news category over
        the last month — that's what makes the pipeline see fresh, dated
        results at all once WP1's _searxng_query actually honors them."""
        client = _client("Acme GmbH")
        search_mock = AsyncMock(return_value=_qdata([]))
        with patch.object(pipeline, "_searxng_query", search_mock):
            await pipeline._news_candidates(1, client)
        first_call = search_mock.await_args_list[0]
        assert first_call.kwargs["categories"] == "news"
        assert first_call.kwargs["time_range"] == "month"

    @pytest.mark.asyncio
    async def test_all_news_queries_zero_results_with_unresponsive_flagged(self):
        """The WP7 pattern: brave/startpage/qwant suspended, every
        categories='news' query comes back empty — _news_candidates must
        surface this, not just quietly return []."""
        client = _client("Acme GmbH")
        unresponsive = [["brave", "Suspended: too many requests"], ["startpage", "Suspended: CAPTCHA"]]

        async def fake_query(query, limit=10, *, categories=None, time_range=None, language=None):
            return _qdata([], unresponsive=unresponsive)

        with patch.object(pipeline, "_searxng_query", AsyncMock(side_effect=fake_query)):
            data = await pipeline._news_candidates(1, client)
        assert data["candidates"] == []
        assert data["news_zero_all"] is True
        assert {tuple(p) for p in data["unresponsive"]} == {
            ("brave", "Suspended: too many requests"), ("startpage", "Suspended: CAPTCHA"),
        }

    @pytest.mark.asyncio
    async def test_one_news_query_has_hits_news_zero_all_is_false(self):
        client = _client("Acme GmbH", industry="Retail")
        hit = {"url": "https://news.example/acme-story", "publishedDate": _RECENT_ISO, "title": "Acme"}

        async def fake_query(query, limit=10, *, categories=None, time_range=None, language=None):
            if "Retail" in query:
                return _qdata([hit])
            return _qdata([], unresponsive=[["brave", "down"]])

        with patch.object(pipeline, "_searxng_query", AsyncMock(side_effect=fake_query)):
            data = await pipeline._news_candidates(1, client)
        assert data["news_zero_all"] is False

    @pytest.mark.asyncio
    async def test_undated_results_recovered_via_page_probe(self):
        client = _client("Acme GmbH")
        url = "https://news.example/acme-story"

        async def fake_query(query, limit=10, *, categories=None, time_range=None, language=None):
            return _qdata([{"url": url, "title": "Acme story"}])  # no publishedDate, no URL date

        with patch.object(pipeline, "_searxng_query", AsyncMock(side_effect=fake_query)), \
             patch.object(pipeline, "_probe_published_date", AsyncMock(return_value=_RECENT_ISO)):
            data = await pipeline._news_candidates(1, client)

        assert len(data["candidates"]) == 1
        assert data["candidates"][0]["_published"] == _RECENT_ISO
        assert data["undated_total"] == 1

    @pytest.mark.asyncio
    async def test_undated_results_page_probe_finds_nothing_dropped(self):
        client = _client("Acme GmbH")
        url = "https://news.example/acme-story"

        async def fake_query(query, limit=10, *, categories=None, time_range=None, language=None):
            return _qdata([{"url": url, "title": "Acme story"}])

        with patch.object(pipeline, "_searxng_query", AsyncMock(side_effect=fake_query)), \
             patch.object(pipeline, "_probe_published_date", AsyncMock(return_value=None)):
            data = await pipeline._news_candidates(1, client)

        assert data["candidates"] == []
        assert data["undated_total"] == 1

    @pytest.mark.asyncio
    async def test_page_probe_bounded_to_five_undated_candidates(self):
        client = _client("Acme GmbH", industry="Retail")
        urls = [f"https://news.example/story-{i}" for i in range(8)]

        async def fake_query(query, limit=10, *, categories=None, time_range=None, language=None):
            return _qdata([{"url": u, "title": u} for u in urls])

        probe_mock = AsyncMock(return_value=None)
        with patch.object(pipeline, "_searxng_query", AsyncMock(side_effect=fake_query)), \
             patch.object(pipeline, "_probe_published_date", probe_mock):
            data = await pipeline._news_candidates(1, client)
        assert data["undated_total"] == 8
        assert probe_mock.await_count == 5


# ---------------------------------------------------------------------------
# _client_newsroom_urls (pure helper)
# ---------------------------------------------------------------------------

class TestClientNewsroomUrls:
    def test_playbook_first_then_monitored_sources_deduped(self):
        client = {"metadata": {"monitored_sources": [
            {"url": "https://acme.com/press"}, {"url": "https://acme.com/news"},
        ]}}
        pb = {"newsroom": {"urls": ["https://acme.com/news", "https://acme.com/media"]}}
        assert pipeline._client_newsroom_urls(client, pb) == [
            "https://acme.com/news", "https://acme.com/media", "https://acme.com/press",
        ]

    def test_no_playbook_falls_back_to_monitored_sources(self):
        client = {"metadata": {"monitored_sources": [{"url": "https://acme.com/press"}]}}
        assert pipeline._client_newsroom_urls(client, None) == ["https://acme.com/press"]

    def test_neither_returns_empty(self):
        assert pipeline._client_newsroom_urls({"metadata": {}}, None) == []


# ---------------------------------------------------------------------------
# _newsroom_candidates — own-newsroom tier (WP9)
# ---------------------------------------------------------------------------

def _patch_page_httpx(text=None, status=200, get_side_effect=None):
    client = MagicMock()
    if get_side_effect is not None:
        client.get = AsyncMock(side_effect=get_side_effect)
    else:
        client.get = AsyncMock(return_value=MagicMock(status_code=status, text=text or ""))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return patch.object(pipeline.httpx, "AsyncClient", return_value=ctx), client


class TestNewsroomCandidates:
    NEWSROOM_HTML = (
        '<html><body><ul>'
        '<li><a href="/presse/produkt-a">Acme launches Produkt A</a> <span class="date">12.09.2026</span></li>'
        '<li><a href="/presse/produkt-b">Acme launches Produkt B</a> <time datetime="2026-09-11"></time></li>'
        '<li><a href="/presse/produkt-c">Acme launches Produkt C</a> <span class="date">10.09.2026</span></li>'
        '<li><a href="https://otherdomain.example/press/x">Off-domain item</a> <span class="date">12.09.2026</span></li>'
        '</ul></body></html>'
    )

    def _client_with_playbook(self, monkeypatch, newsroom_urls=None):
        fake_pb = MagicMock()
        fake_pb.load = AsyncMock(return_value=None)
        monkeypatch.setitem(sys.modules, "playbook", fake_pb)
        urls = newsroom_urls if newsroom_urls is not None else ["https://acme.com/presse"]
        return _client("Acme GmbH", website="https://www.acme.com",
                        monitored_sources=[{"url": u} for u in urls])

    @pytest.mark.asyncio
    async def test_german_dated_items_harvested_same_domain_only(self, monkeypatch):
        client = self._client_with_playbook(monkeypatch)
        httpx_patch, _ = _patch_page_httpx(self.NEWSROOM_HTML)
        with httpx_patch:
            candidates, blocked = await pipeline._newsroom_candidates(1, client)
        assert len(candidates) == 3
        assert all(c["engine"] == "newsroom" for c in candidates)
        assert all("otherdomain" not in c["url"] for c in candidates)
        assert blocked == []
        # Each item keeps its OWN date, not a neighbour's (WP9 review BLOCKER 1) —
        # this used to yield 2026-09-11 (item B's <time>) for every item.
        by_url = {c["url"]: c["_published"] for c in candidates}
        assert by_url["https://acme.com/presse/produkt-a"] == "2026-09-12"
        assert by_url["https://acme.com/presse/produkt-b"] == "2026-09-11"
        assert by_url["https://acme.com/presse/produkt-c"] == "2026-09-10"

    @pytest.mark.asyncio
    async def test_no_date_nearby_dropped(self, monkeypatch):
        client = self._client_with_playbook(monkeypatch)
        html = '<html><body><a href="/presse/no-date">No date here</a></body></html>'
        httpx_patch, _ = _patch_page_httpx(html)
        with httpx_patch:
            candidates, blocked = await pipeline._newsroom_candidates(1, client)
        assert candidates == []
        assert blocked == []

    @pytest.mark.asyncio
    async def test_cap_at_ten_candidates(self, monkeypatch):
        client = self._client_with_playbook(monkeypatch)
        items = "".join(
            f'<li><a href="/presse/item-{i}">Item {i}</a> <span class="date">{i + 1:02d}.09.2026</span></li>'
            for i in range(12)
        )
        html = f"<html><body><ul>{items}</ul></body></html>"
        httpx_patch, _ = _patch_page_httpx(html)
        with httpx_patch:
            candidates, blocked = await pipeline._newsroom_candidates(1, client)
        assert len(candidates) == 10

    @pytest.mark.asyncio
    async def test_no_newsroom_urls_returns_empty_without_http_call(self, monkeypatch):
        client = self._client_with_playbook(monkeypatch, newsroom_urls=[])
        client["metadata"]["monitored_sources"] = []
        ac_mock = MagicMock()
        with patch.object(pipeline.httpx, "AsyncClient", ac_mock):
            candidates, blocked = await pipeline._newsroom_candidates(1, client)
        assert candidates == [] and blocked == []
        ac_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_blocked_urls_kinds_fetch_error_403_4xx(self, monkeypatch):
        urls = ["https://acme.com/a", "https://acme.com/b", "https://acme.com/c"]
        client = self._client_with_playbook(monkeypatch, newsroom_urls=urls)

        async def get_side_effect(url, *a, **k):
            if url == urls[0]:
                raise ConnectionError("boom")
            if url == urls[1]:
                return MagicMock(status_code=403, text="")
            return MagicMock(status_code=404, text="")

        httpx_patch, _ = _patch_page_httpx(get_side_effect=get_side_effect)
        with httpx_patch, patch.object(pipeline, "_fetch_rendered_tier",
                                        AsyncMock(return_value=("", "none", ""))) as rendered_mock:
            candidates, blocked = await pipeline._newsroom_candidates(1, client)

        assert candidates == []
        kinds = {b["url"]: b["kind"] for b in blocked}
        assert kinds[urls[0]] == "fetch_error"
        assert kinds[urls[1]] == "403"   # i=1 — the rendered-tier fallback is only for the FIRST page
        assert kinds[urls[2]] == "4xx"
        rendered_mock.assert_not_awaited()   # url[0] raised (not a 403/503); url[1]/url[2] aren't i==0

    @pytest.mark.asyncio
    async def test_rendered_tier_camofox_links_rescue_first_page_403(self, monkeypatch):
        """WP11 rebase: the newsroom tier's 403/503 fallback now goes
        through the shared _fetch_rendered_tier (browser-service ->
        Camofox), not a private re-GET-free helper — so newsroom pages get
        Camofox on 403/503 too. Only links_html (Camofox's <a href>
        reconstruction) is usable for anchor harvesting; _fetch_rendered_tier
        itself already guarantees no re-GET (text_so_far="" tells it the
        plain tier already failed)."""
        url = "https://acme.com/presse"
        client = self._client_with_playbook(monkeypatch, newsroom_urls=[url])
        rescued_links_html = '<a href="/presse/item">Item</a> <span class="date">12.09.2026</span>'
        httpx_patch, get_client = _patch_page_httpx(status=403, text="")
        with httpx_patch, patch.object(
            pipeline, "_fetch_rendered_tier",
            AsyncMock(return_value=("Rendered snapshot text", "camofox", rescued_links_html)),
        ) as rendered_mock:
            candidates, blocked = await pipeline._newsroom_candidates(1, client)
        assert blocked == []
        assert len(candidates) == 1
        assert candidates[0]["url"] == "https://acme.com/presse/item"
        rendered_mock.assert_awaited_once_with(url, "", 18000, 1500)
        get_client.get.assert_awaited_once()   # the 403 GET only, never re-requested

    @pytest.mark.asyncio
    async def test_browser_tier_rescue_with_no_links_html_yields_no_candidates(self, monkeypatch):
        """The browser-service tier alone (no Camofox escalation) only ever
        returns plain innerText — no <a href> markup — so it cannot rescue
        the newsroom tier's anchor harvesting even though the fetch itself
        "succeeded"; this must not be misclassified as blocked either,
        since content genuinely was returned."""
        url = "https://acme.com/presse"
        client = self._client_with_playbook(monkeypatch, newsroom_urls=[url])
        httpx_patch, _ = _patch_page_httpx(status=403, text="")
        with httpx_patch, patch.object(
            pipeline, "_fetch_rendered_tier",
            AsyncMock(return_value=("Some plain rendered text, no markup", "browser", "")),
        ):
            candidates, blocked = await pipeline._newsroom_candidates(1, client)
        assert candidates == []
        # links_html was '' so `html` stayed '' — same as any other
        # no-content outcome for this URL's original 403.
        assert blocked == [{"url": url, "kind": "403", "at": blocked[0]["at"]}]

    @pytest.mark.asyncio
    async def test_older_than_90_days_dropped(self, monkeypatch):
        client = self._client_with_playbook(monkeypatch)
        html = '<a href="/presse/old">Old item</a> <span class="date">01.01.2020</span>'
        httpx_patch, _ = _patch_page_httpx(html)
        with httpx_patch:
            candidates, blocked = await pipeline._newsroom_candidates(1, client)
        assert candidates == []

    @pytest.mark.asyncio
    async def test_no_domain_returns_empty(self, monkeypatch):
        client = _client("Acme GmbH")  # no website → no domain
        candidates, blocked = await pipeline._newsroom_candidates(1, client)
        assert candidates == [] and blocked == []


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
             patch.object(pipeline, "_news_candidates", AsyncMock(return_value=_news_data(cand))), \
             _no_newsroom(), \
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
             patch.object(pipeline, "_news_candidates", AsyncMock(return_value=_news_data(cand))), \
             _no_newsroom(), \
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
             patch.object(pipeline, "_news_candidates", AsyncMock(return_value=_news_data(cand))), \
             _no_newsroom(), \
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
             patch.object(pipeline, "_news_candidates", AsyncMock(return_value=_news_data(cand))), \
             _no_newsroom(), \
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
             patch.object(pipeline, "_news_candidates", AsyncMock(return_value=_news_data(cand))), \
             _no_newsroom(), \
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
             patch.object(pipeline, "_news_candidates", AsyncMock(return_value=_news_data([]))), \
             _no_newsroom(), \
             patch.object(pipeline.llm, "acomplete", llm_mock):
            result = await pipeline._client_news_scan(1, client)
        assert result["found"] == 0
        llm_mock.assert_not_awaited()

    # -- WP9: degraded-backend detection -----------------------------------

    @pytest.mark.asyncio
    async def test_degraded_all_unresponsive_zero_results_sets_error_nothing_written(self):
        client = _client("Acme GmbH")
        unresponsive = [["brave", "Suspended: too many requests"], ["startpage", "Suspended: CAPTCHA"]]
        db = MagicMock()
        db.index_document = AsyncMock()
        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "_news_candidates",
                          AsyncMock(return_value=_news_data([], unresponsive=unresponsive, news_zero_all=True))), \
             _no_newsroom(), \
             patch.object(pipeline.llm, "acomplete", AsyncMock()) as llm_mock:
            result = await pipeline._client_news_scan(1, client)
        assert result["error"] is not None
        assert "search degraded" in result["error"]
        assert "2 engines unresponsive" in result["error"]
        assert result["found"] == 0
        assert result["written"] == 0
        db.index_document.assert_not_awaited()
        llm_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_all_undated_after_probe_sets_error_nothing_written(self):
        client = _client("Acme GmbH")
        db = MagicMock()
        db.index_document = AsyncMock()
        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "_news_candidates", AsyncMock(return_value=_news_data([], undated_total=4))), \
             _no_newsroom(), \
             patch.object(pipeline.llm, "acomplete", AsyncMock()) as llm_mock:
            result = await pipeline._client_news_scan(1, client)
        assert result["error"] == "search results undated"
        assert result["written"] == 0
        db.index_document.assert_not_awaited()
        llm_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_newsroom_saves_degraded_scan_sets_warning_and_writes(self):
        client = _client("Acme GmbH", website="https://www.acme.com")
        unresponsive = [["brave", "Suspended: too many requests"]]
        newsroom_cand = [
            {"url": f"https://acme.com/press/{i}", "title": f"Press {i}", "content": "",
             "_norm_url": f"acme.com/press/{i}", "_published": _RECENT_ISO, "query": "", "engine": "newsroom"}
            for i in range(3)
        ]
        reply = json.dumps([
            {"i": i, "relevance": 3, "signal_type": "news", "headline": f"h{i}", "why": "w"}
            for i in range(3)
        ])
        db = MagicMock()
        db.list_documents = AsyncMock(return_value=[])
        db.index_document = AsyncMock(return_value=101)
        db.link_document = AsyncMock()
        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "_news_candidates",
                          AsyncMock(return_value=_news_data([], unresponsive=unresponsive, news_zero_all=True))), \
             patch.object(pipeline, "_newsroom_candidates", AsyncMock(return_value=(newsroom_cand, []))), \
             patch.object(pipeline.llm, "acomplete", AsyncMock(return_value=reply)):
            result = await pipeline._client_news_scan(1, client)
        assert result["error"] is None
        assert "warning" in result and "search degraded" in result["warning"]
        assert result["newsroom_found"] == 3
        assert result["found"] == 3
        assert result["written"] == 3
        # WP9 review nit 2: the newsroom-rescue path must not drop `unresponsive`.
        assert result["unresponsive"] == unresponsive

    @pytest.mark.asyncio
    async def test_newsroom_under_three_does_not_save_a_degraded_scan(self):
        client = _client("Acme GmbH", website="https://www.acme.com")
        unresponsive = [["brave", "Suspended: too many requests"]]
        newsroom_cand = [
            {"url": "https://acme.com/press/1", "title": "Press 1", "content": "",
             "_norm_url": "acme.com/press/1", "_published": _RECENT_ISO, "query": "", "engine": "newsroom"},
        ]
        db = MagicMock()
        db.index_document = AsyncMock()
        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "_news_candidates",
                          AsyncMock(return_value=_news_data([], unresponsive=unresponsive, news_zero_all=True))), \
             patch.object(pipeline, "_newsroom_candidates", AsyncMock(return_value=(newsroom_cand, []))), \
             patch.object(pipeline.llm, "acomplete", AsyncMock()) as llm_mock:
            result = await pipeline._client_news_scan(1, client)
        assert result["error"] is not None
        assert "warning" not in result
        db.index_document.assert_not_awaited()
        llm_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_newsroom_blocked_urls_recorded_to_playbook(self, monkeypatch):
        client = _client("Acme GmbH", website="https://www.acme.com")
        blocked = [{"url": "https://acme.com/press", "kind": "403", "at": "2026-09-14T00:00:00+00:00"}]
        fake_pb = MagicMock()
        fake_pb.record = AsyncMock()
        monkeypatch.setitem(sys.modules, "playbook", fake_pb)
        db = MagicMock()
        db.list_documents = AsyncMock(return_value=[])
        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "_news_candidates", AsyncMock(return_value=_news_data([]))), \
             patch.object(pipeline, "_newsroom_candidates", AsyncMock(return_value=([], blocked))), \
             patch.object(pipeline.llm, "acomplete", AsyncMock()):
            await pipeline._client_news_scan(1, client)
        fake_pb.record.assert_awaited_once()
        call = fake_pb.record.await_args
        assert call.args[1] == "acme.com"
        assert call.args[2] == {"blocked_urls": blocked}

    @pytest.mark.asyncio
    async def test_unresponsive_but_not_degraded_still_reported_alongside_found(self):
        """Some engines down but real dated candidates still came back — not
        degraded, so it proceeds normally, but the caller still learns which
        engines were unresponsive."""
        client = _client("Acme GmbH")
        unresponsive = [["brave", "Suspended: too many requests"]]
        cand = [{"url": "https://acme.com/news/1", "title": "t", "content": "c",
                 "_norm_url": "acme.com/news/1", "_published": _RECENT_ISO, "query": "q"}]
        db = MagicMock()
        db.list_documents = AsyncMock(return_value=[])
        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "_news_candidates",
                          AsyncMock(return_value=_news_data(cand, unresponsive=unresponsive))), \
             _no_newsroom(), \
             patch.object(pipeline.llm, "acomplete", AsyncMock(return_value=json.dumps([]))):
            result = await pipeline._client_news_scan(1, client)
        assert result["error"] is None
        assert result["unresponsive"] == unresponsive

    @pytest.mark.asyncio
    async def test_candidates_exist_but_news_queries_degraded_sets_warning(self):
        """WP9 review nit 1: both original degraded branches required
        `not candidates`, so "every news-category query dead, but the
        site: domain query still returned something" read as healthy. It
        should warn (not error — there IS a candidate) since the news
        search itself was degraded."""
        client = _client("Acme GmbH", website="https://www.acme.com")
        unresponsive = [["brave", "Suspended: too many requests"]]
        # A candidate that came from the site:domain (general-category)
        # query, not from a news-category one — every news query was zero.
        cand = [{"url": "https://acme.com/press-release", "title": "t", "content": "c",
                 "_norm_url": "acme.com/press-release", "_published": _RECENT_ISO, "query": "site:acme.com"}]
        db = MagicMock()
        db.list_documents = AsyncMock(return_value=[])
        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "_news_candidates",
                          AsyncMock(return_value=_news_data(cand, unresponsive=unresponsive, news_zero_all=True))), \
             _no_newsroom(), \
             patch.object(pipeline.llm, "acomplete", AsyncMock(return_value=json.dumps([]))):
            result = await pipeline._client_news_scan(1, client)
        assert result["error"] is None
        assert "warning" in result and "search degraded" in result["warning"]
        assert result["unresponsive"] == unresponsive
        assert result["found"] == 1   # the candidate is still scored, nothing is dropped


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
    fake.load = AsyncMock(return_value=None)
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
             patch.object(pipeline, "_news_candidates", AsyncMock(return_value=_news_data(cand))), \
             _no_newsroom(), \
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
             patch.object(pipeline, "_news_candidates", AsyncMock(return_value=_news_data(cand))), \
             _no_newsroom(), \
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
             patch.object(pipeline, "_searxng_query", AsyncMock(return_value=_qdata(results))), \
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
             patch.object(pipeline, "_searxng_query", AsyncMock(return_value=_qdata(results))), \
             patch.object(pipeline.llm, "acomplete", llm_mock):
            result = await pipeline._market_news_scan(1, "Automotive", "the Automotive sector")

        assert result["found"] == 0
        llm_mock.assert_not_awaited()
        db.index_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_falls_back_to_focus_text_when_industry_blank(self):
        """Source-change-triggered scans pass industry='' — the free-text focus
        description drives the search terms instead."""
        search_mock = AsyncMock(return_value=_qdata([]))
        db = MagicMock()
        db.list_signals = AsyncMock(return_value=[])
        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "_searxng_query", search_mock):
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
             patch.object(pipeline, "_searxng_query", AsyncMock(return_value=_qdata(results))), \
             patch.object(pipeline.llm, "acomplete", AsyncMock(return_value=reply)):
            result = await pipeline._market_news_scan(1, "Automotive", "the Automotive sector", max_write=2)
        assert result["written"] == 2
        assert db.index_document.await_count == 2

    @pytest.mark.asyncio
    async def test_degraded_backend_sets_error_writes_nothing(self):
        unresponsive = [["brave", "Suspended: too many requests"], ["qwant", "Suspended: access denied"]]
        db = MagicMock()
        db.list_signals = AsyncMock(return_value=[])
        db.index_document = AsyncMock()
        llm_mock = AsyncMock()
        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "_searxng_query", AsyncMock(return_value=_qdata([], unresponsive=unresponsive))), \
             patch.object(pipeline.llm, "acomplete", llm_mock):
            result = await pipeline._market_news_scan(1, "Automotive", "the Automotive sector")
        assert result["error"] is not None
        assert "search degraded" in result["error"]
        assert result["written"] == 0
        llm_mock.assert_not_awaited()
        db.index_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_some_results_despite_unresponsive_not_degraded(self):
        results = [{"url": "https://reuters.com/industry-update", "title": "Industry update", "content": "c",
                    "publishedDate": _RECENT_ISO}]
        unresponsive = [["brave", "Suspended: too many requests"]]
        db = MagicMock()
        db.list_signals = AsyncMock(return_value=[])
        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "_searxng_query",
                          AsyncMock(return_value=_qdata(results, unresponsive=unresponsive))), \
             patch.object(pipeline.llm, "acomplete", AsyncMock(return_value=json.dumps([]))):
            result = await pipeline._market_news_scan(1, "Automotive", "the Automotive sector")
        assert result["error"] is None
        assert result["unresponsive"] == unresponsive


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
             patch.object(pipeline, "_searxng_query", AsyncMock(return_value=_qdata(results))), \
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
             patch.object(pipeline, "_searxng_query", AsyncMock(return_value=_qdata(results))), \
             patch.object(pipeline.llm, "acomplete", acomplete):
            await pipeline._market_news_scan(1, "Automotive", "the Automotive sector")
        prompt = acomplete.await_args.args[0]
        assert "Learned rules" not in prompt


# ---------------------------------------------------------------------------
# routers.knowledge: POST /api/clients/{name}/news/scan (WP9 review nit 3)
# ---------------------------------------------------------------------------

class TestNewsScanEndpointOkFlag:
    """A degraded scan (result["error"] set) must not report ok: True —
    HTTP 200 is fine, but the body's own ok flag has to say it failed, the
    same way the rest of this API distinguishes a successful call from one
    that ran but found a problem."""

    @pytest.mark.asyncio
    async def test_ok_false_when_scan_degraded(self):
        from routers import knowledge as knowledge_router
        client = _client("Acme GmbH")
        degraded = {"found": 0, "scored": 0, "written": 0, "max_relevance": 0,
                    "error": "search degraded: 2 engines unresponsive (brave: down, startpage: down)"}
        db = MagicMock()
        db.get_client = AsyncMock(return_value=client)
        with patch.object(knowledge_router, "db_module", db), \
             patch.object(knowledge_router, "DB_AVAILABLE", True), \
             patch.object(knowledge_router, "_client_news_scan", AsyncMock(return_value=degraded)):
            result = await knowledge_router.scan_client_news_endpoint("Acme GmbH", user={"org_id": 1})
        assert result["ok"] is False
        assert result["error"] == degraded["error"]

    @pytest.mark.asyncio
    async def test_ok_true_when_scan_succeeds(self):
        from routers import knowledge as knowledge_router
        client = _client("Acme GmbH")
        ok_result = {"found": 1, "scored": 1, "written": 1, "max_relevance": 3, "error": None}
        db = MagicMock()
        db.get_client = AsyncMock(return_value=client)
        with patch.object(knowledge_router, "db_module", db), \
             patch.object(knowledge_router, "DB_AVAILABLE", True), \
             patch.object(knowledge_router, "_client_news_scan", AsyncMock(return_value=ok_result)):
            result = await knowledge_router.scan_client_news_endpoint("Acme GmbH", user={"org_id": 1})
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_ok_true_when_only_a_warning_is_set(self):
        """A warning (newsroom rescued a degraded backend) is not a failure."""
        from routers import knowledge as knowledge_router
        client = _client("Acme GmbH")
        warned = {"found": 3, "scored": 3, "written": 3, "max_relevance": 3, "error": None,
                  "warning": "search degraded: 1 engines unresponsive (brave: down)"}
        db = MagicMock()
        db.get_client = AsyncMock(return_value=client)
        with patch.object(knowledge_router, "db_module", db), \
             patch.object(knowledge_router, "DB_AVAILABLE", True), \
             patch.object(knowledge_router, "_client_news_scan", AsyncMock(return_value=warned)):
            result = await knowledge_router.scan_client_news_endpoint("Acme GmbH", user={"org_id": 1})
        assert result["ok"] is True
        assert result["warning"] == warned["warning"]
