"""
tests/test_fetch_fallback.py — Camofox fallback tier for the Python page
fetchers (WP11).

Covers routers.pipeline: _fetch_page_camofox (text + links_html, tab
lifecycle, timeout budget), _fetch_rendered_tier / _fetch_page_text's
three-tier fallback (plain GET -> browser-service -> Camofox) including the
normalized-length comparison fix (WP11 review blocker 1), _fetch_page_raw's
link harvesting off a Camofox snapshot (blocker 2 / nit 2), prefer_camofox
ordering, the _FETCH_TIER_LOG diagnostics ring, and the admin-only
GET /api/agents/fetch-log endpoint. Never makes a real HTTP call —
httpx.AsyncClient is patched throughout, following
tests/test_source_monitor.py::_patch_httpx's pattern.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

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
    camofox_delete_raises=False,
):
    """A single mocked httpx.AsyncClient standing in for every tier. Routes
    on the call (get/post/delete) and the URL, so one patch can drive plain
    GET, the browser-service POST, and the full Camofox tab lifecycle
    (POST /tabs, GET /tabs/{id}/snapshot, DELETE /tabs/{id}).

    camofox_tab_id=None simulates a 200 /tabs response with no tabId/id
    field in the body (distinct from a non-2xx /tabs response)."""
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
            resp.json.return_value = {"tabId": camofox_tab_id} if camofox_tab_id else {}
            return resp
        if browser_raises:
            raise RuntimeError("browser-service boom")
        resp = MagicMock(status_code=browser_status)
        resp.json.return_value = {"text": browser_text}
        return resp

    async def fake_delete(url, *args, **kwargs):
        calls["delete_urls"].append(url)
        if camofox_delete_raises:
            raise RuntimeError("camofox delete boom")
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


@pytest.fixture()
def _no_sleep(monkeypatch):
    """_fetch_page_camofox sleeps wait_ms before snapshotting — skip the real
    delay in tests. NOT autouse at module scope: pipeline.asyncio IS the
    process-wide asyncio module (there's only one), so patching its sleep
    reaches every coroutine running during the test, not just this one. The
    fetch-log endpoint tests below boot the real FastAPI app (TestClient),
    which can have its own background polling loops depending on
    asyncio.sleep for an actual delay — poisoning it to return instantly
    turns those into a tight busy-loop that hangs the test run. Applied only
    via the classes that actually call _fetch_page_camofox."""
    async def _instant_sleep(_secs):
        return None
    monkeypatch.setattr(pipeline.asyncio, "sleep", _instant_sleep)


# ---------------------------------------------------------------------------
# _fetch_page_camofox
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_no_sleep")
class TestFetchPageCamofox:
    @pytest.mark.asyncio
    async def test_returns_empty_and_makes_no_calls_when_camofox_url_unset(self, monkeypatch):
        monkeypatch.delenv("CAMOFOX_URL", raising=False)
        client_cls = MagicMock()
        with patch.object(pipeline.httpx, "AsyncClient", client_cls):
            text, links = await pipeline._fetch_page_camofox("https://example.com")
        assert text == "" and links == ""
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
            text, links = await pipeline._fetch_page_camofox("https://www.trumpf.com/de_DE/karriere/")
        assert "Karriere bei TRUMPF" in text
        assert len(text) > 500
        assert links == ""  # no "- link ... [eN]:" entries in this snapshot
        client.delete.assert_awaited_once()
        assert "tab-abc" in calls["delete_urls"][0]

    @pytest.mark.asyncio
    async def test_tab_deleted_even_when_snapshot_raises(self, monkeypatch):
        monkeypatch.setenv("CAMOFOX_URL", "http://camofox-test:9377")
        httpx_patch, client, calls = _patch_httpx(
            camofox_tab_id="tab-xyz",
            camofox_snapshot_raises=True,
        )
        with httpx_patch:
            text, links = await pipeline._fetch_page_camofox("https://miele.de/careers")
        assert text == "" and links == ""
        client.delete.assert_awaited_once()
        assert "tab-xyz" in calls["delete_urls"][0]

    @pytest.mark.asyncio
    async def test_text_still_returned_when_delete_raises(self, monkeypatch):
        """The tab-cleanup DELETE is best-effort — its failure must not lose
        the text/links already extracted from the snapshot."""
        monkeypatch.setenv("CAMOFOX_URL", "http://camofox-test:9377")
        snapshot = "Content survives a failed cleanup. " * 30
        httpx_patch, client, calls = _patch_httpx(
            camofox_tab_id="tab-cleanup-fail",
            camofox_snapshot=snapshot,
            camofox_delete_raises=True,
        )
        with httpx_patch:
            text, _links = await pipeline._fetch_page_camofox("https://example.com")
        assert "Content survives a failed cleanup" in text
        client.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tabs_non_2xx_skips_delete(self, monkeypatch):
        monkeypatch.setenv("CAMOFOX_URL", "http://camofox-test:9377")
        httpx_patch, client, _calls = _patch_httpx(camofox_tabs_status=500)
        with httpx_patch:
            text, links = await pipeline._fetch_page_camofox("https://example.com")
        assert text == "" and links == ""
        client.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tabs_200_without_tab_id_skips_delete(self, monkeypatch):
        """A 200 /tabs response missing tabId/id is a distinct failure mode
        from a non-2xx status — both must skip DELETE (no tab was ever
        opened), but only this one exercises the 'no tabId in response'
        branch."""
        monkeypatch.setenv("CAMOFOX_URL", "http://camofox-test:9377")
        httpx_patch, client, _calls = _patch_httpx(camofox_tabs_status=200, camofox_tab_id=None)
        with httpx_patch:
            text, links = await pipeline._fetch_page_camofox("https://example.com")
        assert text == "" and links == ""
        client.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_never_raises_when_tabs_call_errors(self, monkeypatch):
        monkeypatch.setenv("CAMOFOX_URL", "http://camofox-test:9377")
        httpx_patch, client, _calls = _patch_httpx(camofox_tabs_raises=True)
        with httpx_patch:
            text, links = await pipeline._fetch_page_camofox("https://example.com")
        assert text == "" and links == ""

    @pytest.mark.asyncio
    async def test_wait_ms_clamped_between_2000_and_4000(self, monkeypatch):
        seen = {}

        async def _spy_sleep(secs):
            seen["secs"] = secs

        monkeypatch.setattr(pipeline.asyncio, "sleep", _spy_sleep)
        monkeypatch.setenv("CAMOFOX_URL", "http://camofox-test:9377")
        httpx_patch, _client, _calls = _patch_httpx(camofox_tab_id="tab-1", camofox_snapshot="hi")

        with httpx_patch:
            await pipeline._fetch_page_camofox("https://example.com", wait_ms=100)
        assert seen["secs"] == pytest.approx(2.0)

        with httpx_patch:
            await pipeline._fetch_page_camofox("https://example.com", wait_ms=999_999)
        assert seen["secs"] == pytest.approx(4.0)

    def test_camofox_links_html_resolves_relative_hrefs(self):
        snapshot = (
            '- link "Vertrieb Jobs" [e3]:\n'
            '  - /url: /de/karriere/vertrieb\n'
            '- link "Impressum" [e9]:\n'
            '  - /url: /impressum\n'
        )
        html = pipeline._camofox_links_html(snapshot, "https://example.com/de/karriere/")
        assert '<a href="https://example.com/de/karriere/vertrieb">Vertrieb Jobs</a>' in html
        assert '<a href="https://example.com/impressum">Impressum</a>' in html

    def test_camofox_links_html_empty_when_no_link_entries(self):
        assert pipeline._camofox_links_html("just some accessibility text, no links", "https://x") == ""


# ---------------------------------------------------------------------------
# _fetch_page_text — three-tier fallback wiring
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_no_sleep")
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
    async def test_thin_nav_stub_does_not_beat_shorter_real_browser_text(self, monkeypatch):
        """WP11 review blocker 1: a plain-GET nav/footer stub's whitespace
        (turned into extra spaces by the tag-stripping regex) used to inflate
        its length past a shorter but genuine browser-service result, so the
        stub won the old `len(browser_text) >= len(text)` comparison and the
        function returned repeated "Impressum" boilerplate instead of the
        real listing. Once the plain tier is thin (<500 normalized chars) it
        must no longer compete on raw length against a later tier at all."""
        monkeypatch.setenv("CAMOFOX_URL", "http://camofox-test:9377")
        monkeypatch.setenv("BROWSER_SERVICE_URL", "http://browser-test:3000")

        # Lots of newlines/indentation between short nav items so the
        # tag-stripped-but-uncollapsed text is much longer than its
        # whitespace-collapsed content.
        nav_html = "<nav>" + ("<a>Impressum</a>\n\n    \n\t" * 6) + "</nav>"
        raw_len = len(pipeline._HTML_TAG_RE.sub(" ", nav_html).strip())
        collapsed_len = len(pipeline._ws_norm(pipeline._HTML_TAG_RE.sub(" ", nav_html)))
        assert collapsed_len < raw_len  # sanity: whitespace really is inflating it
        assert collapsed_len < 500  # sanity: still "thin" by the real gate

        browser_text = "Junior Sales Engineer (m/w/d), Stuttgart. Apply by 2026-10-01."
        assert len(browser_text) < raw_len  # the real content is shorter than the stub's RAW length
        camofox_snapshot = "Shorter Camofox result."

        httpx_patch, client, calls = _patch_httpx(
            get_status=200,
            get_text=nav_html,
            browser_status=200,
            browser_text=browser_text,
            camofox_tab_id="tab-blocker1",
            camofox_snapshot=camofox_snapshot,
        )
        with httpx_patch:
            text = await pipeline._fetch_page_text("https://example.de/karriere")

        assert text == browser_text
        assert "Impressum" not in text
        assert pipeline._FETCH_TIER_LOG[-1]["tier"] == "browser"

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
    async def test_ring_log_is_bounded_to_200_entries(self, monkeypatch):
        """D19: raised from 50 -> 200 once the path-probe and posting-title-
        fetch tiers started recording here too."""
        monkeypatch.setenv("CAMOFOX_URL", "http://camofox-test:9377")
        html = "<html>" + "content " * 100 + "</html>"
        httpx_patch, client, _calls = _patch_httpx(get_status=200, get_text=html)
        with httpx_patch:
            for i in range(205):
                await pipeline._fetch_page_text(f"https://example.com/{i}")
        assert len(pipeline._FETCH_TIER_LOG) == 200
        assert pipeline._FETCH_TIER_LOG[-1]["url"] == "https://example.com/204"


# ---------------------------------------------------------------------------
# _fetch_page_raw — link harvesting off a Camofox snapshot (nit 2)
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_no_sleep")
class TestFetchPageRawCamofoxLinks:
    @pytest.mark.asyncio
    async def test_harvest_links_finds_camofox_links_when_plain_html_is_empty(self, monkeypatch):
        monkeypatch.setenv("CAMOFOX_URL", "http://camofox-test:9377")
        monkeypatch.setenv("BROWSER_SERVICE_URL", "http://browser-test:3000")
        base_url = "https://example.com/de/karriere/"
        snapshot = (
            '- link "Vertrieb Jobs" [e3]:\n'
            '  - /url: /de/karriere/vertrieb\n'
            '- link "Technik Jobs" [e7]:\n'
            '  - /url: /de/karriere/technik\n'
        )
        httpx_patch, client, calls = _patch_httpx(
            get_status=403,  # plain GET blocked -> html stays ''
            browser_status=200,
            browser_text="tiny",  # thin -> escalate to Camofox
            camofox_tab_id="tab-links",
            camofox_snapshot=snapshot,
        )
        with httpx_patch:
            text, html = await pipeline._fetch_page_raw(base_url)

        assert html  # Camofox's links_html filled in for the empty plain-GET body
        found = pipeline._harvest_links(html, base_url, pipeline._CAREERS_KEYS)
        assert "https://example.com/de/karriere/vertrieb" in found
        assert "https://example.com/de/karriere/technik" in found
        assert pipeline._FETCH_TIER_LOG[-1]["url"] == base_url

    @pytest.mark.asyncio
    async def test_plain_html_kept_and_camofox_links_appended(self, monkeypatch):
        """A thin but real plain body (a JS shell, a nav-only page) keeps its
        own html AND gets Camofox's links appended, so _harvest_links sees
        both — a JS shell exposes its real links only through the rendered
        snapshot."""
        monkeypatch.setenv("CAMOFOX_URL", "http://camofox-test:9377")
        monkeypatch.setenv("BROWSER_SERVICE_URL", "http://browser-test:3000")
        base_url = "https://example.com/karriere"
        plain_html = '<a href="/karriere/jobs">Jobs</a>'  # thin text, but real html
        snapshot = '- link "Should not appear" [e1]:\n  - /url: /somewhere\n'
        httpx_patch, client, calls = _patch_httpx(
            get_status=200,
            get_text=plain_html,
            browser_status=200,
            browser_text="tiny",
            camofox_tab_id="tab-2",
            camofox_snapshot=snapshot,
        )
        with httpx_patch:
            _text, html = await pipeline._fetch_page_raw(base_url)
        assert html.startswith(plain_html)
        assert 'href="https://example.com/somewhere"' in html
        assert "Should not appear" in html


# ---------------------------------------------------------------------------
# GET /api/agents/fetch-log — admin-only diagnostics endpoint (blocker 2)
# ---------------------------------------------------------------------------

FAKE_ADMIN = {
    "id": 1, "org_id": 1, "username": "konrad", "display_name": "Konrad",
    "email": "k@test.com", "role": "admin", "org_name": "North", "org_slug": "north",
}
FAKE_MEMBER = {**FAKE_ADMIN, "id": 2, "username": "tester", "role": "member"}


@pytest.fixture()
def admin_client():
    with (
        patch("server.db_module.init_db", new_callable=AsyncMock),
        patch("server.db_module.close_db", new_callable=AsyncMock),
        patch("server.DB_AVAILABLE", True),
    ):
        from server import app
        from routers.auth import current_user

        async def _fake_admin():
            return FAKE_ADMIN

        saved = dict(app.dependency_overrides)
        app.dependency_overrides[current_user] = _fake_admin
        try:
            with TestClient(app, raise_server_exceptions=True) as client:
                yield client
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(saved)


@pytest.fixture()
def member_client():
    with (
        patch("server.db_module.init_db", new_callable=AsyncMock),
        patch("server.db_module.close_db", new_callable=AsyncMock),
        patch("server.DB_AVAILABLE", True),
    ):
        from server import app
        from routers.auth import current_user

        async def _fake_member():
            return FAKE_MEMBER

        saved = dict(app.dependency_overrides)
        app.dependency_overrides[current_user] = _fake_member
        try:
            with TestClient(app, raise_server_exceptions=True) as client:
                yield client
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(saved)


class TestFetchLogEndpoint:
    def test_member_forbidden(self, member_client):
        r = member_client.get("/api/agents/fetch-log")
        assert r.status_code == 403

    def test_admin_returns_bounded_entries_with_expected_fields(self, admin_client):
        pipeline._FETCH_TIER_LOG.clear()
        for i in range(205):
            pipeline._record_fetch_tier(f"https://example.com/{i}", "http", 600 + i)
        try:
            r = admin_client.get("/api/agents/fetch-log")
            assert r.status_code == 200
            body = r.json()
            assert body["count"] == 200
            assert len(body["entries"]) == 200
            entry = body["entries"][-1]
            assert set(entry.keys()) == {"url", "tier", "chars", "at"}
            assert entry["url"] == "https://example.com/204"
            assert entry["tier"] == "http"
        finally:
            pipeline._FETCH_TIER_LOG.clear()
