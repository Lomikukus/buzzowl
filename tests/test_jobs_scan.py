"""
tests/test_jobs_scan.py — jobs-scan coverage (WP0 bug sweep scaffold).

Minimal for now: pins the WP0 fix to routers.pipeline._sitemap_job_urls's
content-type guard. WP2 extends this file with the rest of the jobs-scan
coverage (career-URL discovery, junior-title filtering, failure stamping).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routers import pipeline


class _FakeResp:
    """Stand-in for an httpx.Response — a plain dict for headers so
    `.get("content-type", "")` behaves like the real thing (a MagicMock's
    `.get` does not honour the default)."""

    def __init__(self, status_code=200, text="", content_type=""):
        self.status_code = status_code
        self.text = text
        self.headers = {"content-type": content_type}


def _patch_get(resp):
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return patch.object(pipeline.httpx, "AsyncClient", return_value=ctx)


# ---------------------------------------------------------------------------
# _sitemap_job_urls content-type guard
# ---------------------------------------------------------------------------

class TestSitemapGuard:
    """Regression for the bug where the guard concatenated the content-type
    header and the body prefix before the substring test, so a real
    sitemap served as text/html (common on misconfigured sites) was
    silently dropped."""

    SITEMAP_BODY = (
        "<urlset><url><loc>https://acme.com/jobs/engineer</loc></url></urlset>"
    )

    @pytest.mark.asyncio
    async def test_text_html_content_type_with_urlset_body_is_accepted(self):
        resp = _FakeResp(status_code=200, text=self.SITEMAP_BODY, content_type="text/html; charset=utf-8")
        with _patch_get(resp):
            jobs = await pipeline._sitemap_job_urls("https://acme.com")
        assert jobs == [("engineer", "https://acme.com/jobs/engineer")]

    @pytest.mark.asyncio
    async def test_404_is_rejected_even_with_a_urlset_body(self):
        resp = _FakeResp(status_code=404, text=self.SITEMAP_BODY, content_type="text/html; charset=utf-8")
        with _patch_get(resp):
            jobs = await pipeline._sitemap_job_urls("https://acme.com")
        assert jobs == []
