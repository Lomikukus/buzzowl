"""
tests/test_news_scan.py — news-scan search plumbing (WP1).

Covers routers.pipeline._searxng_results: categories/time_range/language are
only added to the SearXNG request when the caller passes them, and
publishedDate survives on the returned result dicts. (WP3 extends this file
with the full news-scan pipeline; keep this class scoped to the plumbing.)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routers import pipeline


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
