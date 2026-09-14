"""
tests/test_fetch_fallback.py — Camofox fallback tier for the Python page
fetchers (WP11).

Covers routers.pipeline: _fetch_page_camofox, _fetch_page_text's three-tier
fallback (plain GET -> browser-service -> Camofox), prefer_camofox ordering,
and the _FETCH_TIER_LOG diagnostics ring. Never makes a real HTTP call —
httpx.AsyncClient is patched throughout, following
tests/test_source_monitor.py::_patch_httpx's pattern.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routers import pipeline


def _patch_httpx(
    get_status=200,
    get_text="",
    browser_status=200,
    browser_text="",
    browser_raises=False,
    camofox_tabs_status=200,
    camofox_tab_id="tab-1",
    camofox_tabs_raises=False,
    camofox_snapshot_status=200,
    camofox_snapshot="",
    camofox_snapshot_raises=False,
):
    """A single mocked httpx.AsyncClient standing in for every tier. Routes
    on the call (get/post/delete) and the URL, so one patch can drive plain
    GET, the browser-service POST, and the full Camofox tab lifecycle
    (POST /tabs, GET /tabs/{id}/snapshot, DELETE /tabs/{id})."""
    calls = {"get_urls": [], "post_urls": [], "delete_urls": []}

    async def fake_get(url, *args, **kwargs):
        calls["get_urls"].append(url)
        if "/snapshot" in url:
            if camofox_snapshot_raises:
                raise RuntimeError("camofox snapshot boom")
            resp = MagicMock(status_code=camofox_snapshot_status)
            resp.json.return_value = {"snapshot": camofox_snapshot}
            return resp
        return MagicMock(status_code=get_status, text=get_text)

    async def fake_post(url, *args, **kwargs):
        calls["post_urls"].append(url)
        if url.endswith("/tabs"):
            if camofox_tabs_raises:
                raise RuntimeError("camofox tabs boom")
            resp = MagicMock(status_code=camofox_tabs_status)
            resp.json.return_value = {"tabId": camofox_tab_id}
            return resp
        if browser_raises:
            raise RuntimeError("browser-service boom")
        resp = MagicMock(status_code=browser_status)
        resp.json.return_value = {"text": browser_text}
        return resp

    async def fake_delete(url, *args, **kwargs):
        calls["delete_urls"].append(url)
        return MagicMock(status_code=200)

    client = MagicMock()
    client.get = AsyncMock(side_effect=fake_get)
    client.post = AsyncMock(side_effect=fake_post)
    client.delete = AsyncMock(side_effect=fake_delete)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)

    return patch.object(pipeline.httpx, "AsyncClient", return_value=ctx), client, calls


@pytest.fixture(autouse=True)
def _clear_fetch_tier_log():
    pipeline._FETCH_TIER_LOG.clear()
    yield
    pipeline._FETCH_TIER_LOG.clear()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """_fetch_page_camofox sleeps wait_ms before snapshotting — skip the real
    delay in tests."""
    async def _instant_sleep(_secs):
        return None
    monkeypatch.setattr(pipeline.asyncio, "sleep", _instant_sleep)


# ---------------------------------------------------------------------------
# _fetch_page_camofox
# ---------------------------------------------------------------------------

class TestFetchPageCamofox:
    @pytest.mark.asyncio
    async def test_returns_empty_and_makes_no_calls_when_camofox_url_unset(self, monkeypatch):
        monkeypatch.delenv("CAMOFOX_URL", raising=False)
        client_cls = MagicMock()
        with patch.object(pipeline.httpx, "AsyncClient", client_cls):
            text = await pipeline._fetch_page_camofox("https://example.com")
        assert text == ""
        client_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_snapshot_text_and_deletes_tab(self, monkeypatch):
        monkeypatch.setenv("CAMOFOX_URL", "http://camofox-test:9377")
        snapshot = "Karriere bei TRUMPF. " * 60
        httpx_patch, client, calls = _patch_httpx(
            camofox_tab_id="tab-abc",
            camofox_snapshot=snapshot,
        )
        with httpx_patch:
            text = await pipeline._fetch_page_camofox("https://www.trumpf.com/de_DE/karriere/")
        assert "Karriere bei TRUMPF" in text
        assert len(text) > 500
        client.delete.assert_awaited_once()
        deleted_url = calls["delete_urls"][0]
        assert "tab-abc" in deleted_url

    @pytest.mark.asyncio
    async def test_tab_deleted_even_when_snapshot_raises(self, monkeypatch):
        monkeypatch.setenv("CAMOFOX_URL", "http://camofox-test:9377")
        httpx_patch, client, calls = _patch_httpx(
            camofox_tab_id="tab-xyz",
            camofox_snapshot_raises=True,
        )
        with httpx_patch:
            text = await pipeline._fetch_page_camofox("https://miele.de/careers")
        assert text == ""
        client.delete.assert_awaited_once()
        assert "tab-xyz" in calls["delete_urls"][0]

    @pytest.mark.asyncio
    async def test_no_tab_id_in_response_skips_delete(self, monkeypatch):
        monkeypatch.setenv("CAMOFOX_URL", "http://camofox-test:9377")
        httpx_patch, client, calls = _patch_httpx(camofox_tabs_status=500)
        with httpx_patch:
            text = await pipeline._fetch_page_camofox("https://example.com")
        assert text == ""
        client.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_never_raises_when_tabs_call_errors(self, monkeypatch):
        monkeypatch.setenv("CAMOFOX_URL", "http://camofox-test:9377")
        httpx_patch, client, _calls = _patch_httpx(camofox_tabs_raises=True)
        with httpx_patch:
            text = await pipeline._fetch_page_camofox("https://example.com")
        assert text == ""


# ---------------------------------------------------------------------------
# _fetch_page_text — three-tier fallback wiring
# ---------------------------------------------------------------------------

class TestFetchPageTextTiers:
    @pytest.mark.asyncio
    async def test_plain_get_long_text_skips_browser_and_camofox(self, monkeypatch):
        monkeypatch.setenv("CAMOFOX_URL", "http://camofox-test:9377")
        html = "<html><body><h1>Careers</h1>" + "Real page content. " * 60 + "</body></html>"
        httpx_patch, client, calls = _patch_httpx(get_status=200, get_text=html)
        with httpx_patch:
            text = await pipeline._fetch_page_text("https://acme.example/careers")
        assert "Real page content." in text
        client.post.assert_not_awaited()
        assert calls["post_urls"] == []
        assert pipeline._FETCH_TIER_LOG[-1]["tier"] == "http"

    @pytest.mark.asyncio
    async def test_403_and_thin_browser_falls_to_camofox(self, monkeypatch):
        monkeypatch.setenv("CAMOFOX_URL", "http://camofox-test:9377")
        monkeypatch.setenv("BROWSER_SERVICE_URL", "http://browser-test:3000")
        camofox_snapshot = "Wir suchen Verstaerkung fuer unser Team. " * 40
        httpx_patch, client, calls = _patch_httpx(
            get_status=403,
            get_text="",
            browser_status=200,
            browser_text="tiny stub",
            camofox_tab_id="tab-99",
            camofox_snapshot=camofox_snapshot,
        )
        with httpx_patch:
            text = await pipeline._fetch_page_text("https://www.miele.de/karriere")
        assert "Wir suchen Verstaerkung" in text
        assert "tab-99" in calls["delete_urls"][0]
        assert pipeline._FETCH_TIER_LOG[-1]["tier"] == "camofox"
        assert pipeline._FETCH_TIER_LOG[-1]["url"] == "https://www.miele.de/karriere"
        assert pipeline._FETCH_TIER_LOG[-1]["chars"] == len(text)

    @pytest.mark.asyncio
    async def test_camofox_url_unset_never_called(self, monkeypatch):
        monkeypatch.delenv("CAMOFOX_URL", raising=False)
        monkeypatch.setenv("BROWSER_SERVICE_URL", "http://browser-test:3000")
        httpx_patch, client, calls = _patch_httpx(
            get_status=403,
            get_text="",
            browser_status=200,
            browser_text="still thin",
        )
        with httpx_patch:
            text = await pipeline._fetch_page_text("https://trumpf.example/karriere")
        # only the browser-service POST happened — no /tabs, no DELETE at all
        assert calls["post_urls"] == ["http://browser-test:3000/fetch"]
        assert calls["delete_urls"] == []
        assert text == "still thin"
        assert pipeline._FETCH_TIER_LOG[-1]["tier"] == "browser"

    @pytest.mark.asyncio
    async def test_browser_service_500_triggers_camofox(self, monkeypatch):
        monkeypatch.setenv("CAMOFOX_URL", "http://camofox-test:9377")
        monkeypatch.setenv("BROWSER_SERVICE_URL", "http://browser-test:3000")
        camofox_snapshot = "Rendered via Camofox after browser-service 500. " * 20
        httpx_patch, client, calls = _patch_httpx(
            get_status=200,
            get_text="<html>hi</html>",  # strips to a couple chars — thin
            browser_status=500,
            camofox_tab_id="tab-500",
            camofox_snapshot=camofox_snapshot,
        )
        with httpx_patch:
            text = await pipeline._fetch_page_text("https://blocked.example/page")
        assert "Rendered via Camofox" in text
        assert pipeline._FETCH_TIER_LOG[-1]["tier"] == "camofox"

    @pytest.mark.asyncio
    async def test_browser_service_timeout_triggers_camofox(self, monkeypatch):
        monkeypatch.setenv("CAMOFOX_URL", "http://camofox-test:9377")
        monkeypatch.setenv("BROWSER_SERVICE_URL", "http://browser-test:3000")
        camofox_snapshot = "Camofox saved the day after a browser timeout. " * 20
        httpx_patch, client, calls = _patch_httpx(
            get_status=403,
            get_text="",
            browser_raises=True,
            camofox_tab_id="tab-to",
            camofox_snapshot=camofox_snapshot,
        )
        with httpx_patch:
            text = await pipeline._fetch_page_text("https://timeout.example/page")
        assert "Camofox saved the day" in text
        assert pipeline._FETCH_TIER_LOG[-1]["tier"] == "camofox"

    @pytest.mark.asyncio
    async def test_prefer_camofox_skips_plain_get_and_tries_browser_first(self, monkeypatch):
        monkeypatch.setenv("CAMOFOX_URL", "http://camofox-test:9377")
        monkeypatch.setenv("BROWSER_SERVICE_URL", "http://browser-test:3000")
        browser_text = "Full careers listing rendered by the browser service. " * 20
        httpx_patch, client, calls = _patch_httpx(
            browser_status=200,
            browser_text=browser_text,
        )
        with httpx_patch:
            text = await pipeline._fetch_page_text(
                "https://cookie-wall.example/jobs", prefer_camofox=True,
            )
        # plain GET must never have been attempted
        assert calls["get_urls"] == []
        assert "Full careers listing" in text
        # browser-service alone was enough — camofox /tabs never called
        assert all("/tabs" not in u for u in calls["post_urls"])
        assert pipeline._FETCH_TIER_LOG[-1]["tier"] == "browser"

    @pytest.mark.asyncio
    async def test_prefer_camofox_falls_through_to_camofox_when_browser_thin(self, monkeypatch):
        monkeypatch.setenv("CAMOFOX_URL", "http://camofox-test:9377")
        monkeypatch.setenv("BROWSER_SERVICE_URL", "http://browser-test:3000")
        camofox_snapshot = "Only Camofox could render this cookie-walled page. " * 20
        httpx_patch, client, calls = _patch_httpx(
            browser_status=200,
            browser_text="tiny",
            camofox_tab_id="tab-pref",
            camofox_snapshot=camofox_snapshot,
        )
        with httpx_patch:
            text = await pipeline._fetch_page_text(
                "https://cookie-wall.example/jobs", prefer_camofox=True,
            )
        # camofox's own snapshot GET may appear, but the plain-GET tier
        # (a GET of the page URL itself) must never have run
        assert "https://cookie-wall.example/jobs" not in calls["get_urls"]
        assert "Only Camofox could render" in text
        assert pipeline._FETCH_TIER_LOG[-1]["tier"] == "camofox"

    @pytest.mark.asyncio
    async def test_ring_log_is_bounded_to_50_entries(self, monkeypatch):
        monkeypatch.setenv("CAMOFOX_URL", "http://camofox-test:9377")
        html = "<html>" + "content " * 100 + "</html>"
        httpx_patch, client, _calls = _patch_httpx(get_status=200, get_text=html)
        with httpx_patch:
            for i in range(55):
                await pipeline._fetch_page_text(f"https://example.com/{i}")
        assert len(pipeline._FETCH_TIER_LOG) == 50
        assert pipeline._FETCH_TIER_LOG[-1]["url"] == "https://example.com/54"
