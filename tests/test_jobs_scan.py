"""
tests/test_jobs_scan.py — jobs-at-intake / careers-page discovery (WP2).

Covers routers.pipeline: _sitemap_job_urls' content-type guard, _filter_positions
(junior-role filter), _careers_candidates / _discover_careers_url, and
_scan_client_jobs (success + failure stamping + playbook integration); and
routers.knowledge: the "## Hiring Signals" brief section and the [JOBS] block
in _build_brief_context.

Fake-db/config helpers mirror tests/test_source_monitor.py.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import playbook
from routers import knowledge, pipeline


def _client(name="Acme", **meta):
    return {"id": 1, "name": name, "metadata": {**meta}}


def _patch_db():
    db = MagicMock()
    db.update_client_metadata = AsyncMock()
    db.get_document = AsyncMock(return_value=None)
    db.update_document = AsyncMock(return_value=None)
    db.index_document = AsyncMock(return_value=1)
    db.link_document = AsyncMock()
    db.list_products = AsyncMock(return_value=[])
    return patch.object(pipeline, "db_module", db), db


def _patch_httpx_sitemap(text, status=200, headers=None):
    resp = MagicMock(status_code=status, text=text, headers=headers or {})
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return patch.object(pipeline.httpx, "AsyncClient", return_value=ctx)


# ---------------------------------------------------------------------------
# Sitemap content-type guard (string concatenation -> proper "or")
# ---------------------------------------------------------------------------

class TestSitemapGuard:
    @pytest.mark.asyncio
    async def test_accepts_urlset_body_without_xml_content_type(self):
        xml = ("<?xml version='1.0'?><urlset><url>"
               "<loc>https://acme.com/jobs/backend-engineer</loc></url></urlset>")
        with _patch_httpx_sitemap(xml, headers={"content-type": "text/plain"}):
            jobs = await pipeline._sitemap_job_urls("https://acme.com")
        urls = [u for _, u in jobs]
        assert "https://acme.com/jobs/backend-engineer" in urls

    @pytest.mark.asyncio
    async def test_rejects_non_sitemap_html_body(self):
        html = "<html><body>not a sitemap, just a page</body></html>"
        with _patch_httpx_sitemap(html, headers={"content-type": "text/html"}):
            jobs = await pipeline._sitemap_job_urls("https://acme.com")
        assert jobs == []

    @pytest.mark.asyncio
    async def test_rejects_non_200_even_with_xml_content_type(self):
        xml = "<urlset><url><loc>https://acme.com/jobs/x</loc></url></urlset>"
        with _patch_httpx_sitemap(xml, status=404, headers={"content-type": "application/xml"}):
            jobs = await pipeline._sitemap_job_urls("https://acme.com")
        assert jobs == []


# ---------------------------------------------------------------------------
# _filter_positions — junior-role filter, dedupe, cap
# ---------------------------------------------------------------------------

class TestFilterPositions:
    def test_ausbildung_fachinformatiker_kept(self):
        positions = [{"title": "Ausbildung Fachinformatiker (m/w/d)"}]
        assert pipeline._filter_positions(positions) == positions

    def test_praktikum_marketing_dropped(self):
        assert pipeline._filter_positions([{"title": "Praktikum Marketing"}]) == []

    def test_trainee_controlling_dropped(self):
        """'Controlling' alone is a common junior finance-trainee program, not a
        management role — it must not override the junior-title drop."""
        assert pipeline._filter_positions([{"title": "Trainee Controlling"}]) == []

    def test_regular_it_role_kept(self):
        positions = [{"title": "Senior Cloud Engineer (m/f/d)"}]
        assert pipeline._filter_positions(positions) == positions

    def test_dedupe_case_insensitive_and_cap_at_20(self):
        positions = [{"title": "Engineer"}, {"title": "engineer"}] + \
            [{"title": f"Manager {i}"} for i in range(25)]
        out = pipeline._filter_positions(positions)
        titles = [p["title"] for p in out]
        assert titles.count("Engineer") == 1
        assert len(out) == 20

    def test_it_mgmt_overbroad_words_no_longer_override_junior_drop(self):
        """Nit: 'system'/'digital'/'projekt' are too generic to serve as an
        IT/management override — they kept genuinely junior titles."""
        titles = [
            "Ausbildung Fachkraft für Systemgastronomie",
            "Ausbildung Mediengestalter Digital und Print",
            "Trainee Projektmanagement",
        ]
        for title in titles:
            assert pipeline._filter_positions([{"title": title}]) == [], title

    def test_german_junior_inflections_dropped(self):
        """Nit: 'Studentische'/'Bachelorand' etc. are junior inflections the
        original _JUNIOR_TITLE_RE (bare 'student'/'bachelor') missed."""
        titles = ["Studentische Hilfskraft Buchhaltung", "Bachelorand Maschinenbau"]
        for title in titles:
            assert pipeline._filter_positions([{"title": title}]) == [], title


# ---------------------------------------------------------------------------
# _ats_match — label-anchored, not a bare substring
# ---------------------------------------------------------------------------

class TestAtsHostAnchoring:
    def test_jobs_evil_com_is_not_an_ats_host(self):
        """BLOCKER 2 nit: 'jobs.' matched anywhere in the host as a bare
        substring, so an attacker's own 'jobs.evil.com' subdomain qualified."""
        assert pipeline._ats_match("jobs.evil.com") is False
        assert pipeline._ats_match("personio.evil.com") is False

    def test_legitimate_multi_tld_ats_hosts_still_match(self):
        """Guard against over-correcting: most _ATS_HOSTS entries (personio,
        workday, smartrecruiters, recruitee, ...) are bare labels with no
        fixed TLD because those vendors operate under several (personio.de,
        personio.com, ...); anchoring must still recognise those, not just
        the handful of entries that already carry a full domain."""
        for host in ("company.personio.de", "company.personio.com",
                     "company.workday.com", "jobs.smartrecruiters.com",
                     "company.recruitee.com", "boards.greenhouse.io"):
            assert pipeline._ats_match(host) is True, host

    def test_jobs_is_a_generic_word_not_an_ats_vendor(self):
        """Re-review BLOCKER: the second-from-last-label rule is right for the
        19 real vendor labels but inverted the meaning of a bare "jobs."
        entry — "jobs" is a generic subdomain word, not a vendor name, so
        anchoring it the same way accepted jobs.<anything>.<tld> wholesale:
        an unrelated job board (jobs.de, jobs.com) or an attacker's
        evil.jobs.com. "jobs." was removed from _ATS_HOSTS entirely; a
        client's own jobs.<domain> subdomain (e.g. jobs.miele.de,
        jobs.apleona.com) is still fine, but recognised via _own_or_ats's
        own-domain arm, not via _ats_match."""
        for host in ("jobs.com", "www.jobs.com", "jobs.de", "jobs.ch",
                     "x.jobs.de", "evil.jobs.com", "jobs.miele.de", "jobs.apleona.com"):
            assert pipeline._ats_match(host) is False, host


# ---------------------------------------------------------------------------
# _careers_candidates / _discover_careers_url
# ---------------------------------------------------------------------------

class TestCareersDiscovery:
    @pytest.mark.asyncio
    async def test_playbook_url_short_circuits_discovery(self):
        client = _client(website="https://acme.com")
        pb = {"careers": {"url": "https://acme.com/karriere", "tier": "homepage",
                           "last_success_at": datetime.now(timezone.utc).isoformat()}}
        searx = AsyncMock()
        acomplete = AsyncMock()
        with patch.object(pipeline, "_searxng_results", searx), \
             patch.object(pipeline, "_fetch_page_raw", AsyncMock(return_value=("", ""))), \
             patch.object(pipeline, "_sitemap_job_urls", AsyncMock(return_value=[])), \
             patch.object(pipeline.llm, "acomplete", acomplete):
            url, tier = await pipeline._discover_careers_url(1, client, pb)
        assert (url, tier) == ("https://acme.com/karriere", "playbook")
        searx.assert_not_awaited()
        acomplete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stale_playbook_url_joins_the_pool_instead_of_short_circuiting(self):
        client = _client(website="https://acme.com")
        old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        pb = {"careers": {"url": "https://acme.com/old-careers", "tier": "homepage",
                           "last_success_at": old}}
        with patch.object(pipeline, "_searxng_results", AsyncMock(return_value=[])), \
             patch.object(pipeline, "_fetch_page_raw", AsyncMock(return_value=("", ""))), \
             patch.object(pipeline, "_sitemap_job_urls", AsyncMock(return_value=[])):
            url, tier = await pipeline._discover_careers_url(1, client, pb)
        # only candidate in the pool -> still used, but via the normal (stale) tier
        assert (url, tier) == ("https://acme.com/old-careers", "playbook")

    @pytest.mark.asyncio
    async def test_llm_offdomain_pick_is_rejected_heuristic_owndomain_wins(self):
        client = _client(website="https://acme.com")
        results = [
            {"url": "https://www.stepstone.de/acme-job", "title": "Acme job on Stepstone"},
            {"url": "https://acme.com/karriere", "title": "Acme Karriere"},
        ]
        with patch.object(pipeline, "_searxng_results", AsyncMock(return_value=results)), \
             patch.object(pipeline, "_fetch_page_raw", AsyncMock(return_value=("", ""))), \
             patch.object(pipeline, "_sitemap_job_urls", AsyncMock(return_value=[])), \
             patch.object(pipeline.llm, "acomplete",
                           AsyncMock(return_value="https://www.stepstone.de/acme-job")):
            url, tier = await pipeline._discover_careers_url(1, client)
        assert url == "https://acme.com/karriere"
        assert tier == "searxng"

    @pytest.mark.asyncio
    async def test_single_sitemap_hit_becomes_a_candidate(self):
        client = _client(website="https://acme.com")
        with patch.object(pipeline, "_fetch_page_raw", AsyncMock(return_value=("", ""))), \
             patch.object(pipeline, "_sitemap_job_urls",
                           AsyncMock(return_value=[("Backend Engineer",
                                                     "https://acme.com/jobs/backend-engineer-123")])), \
             patch.object(pipeline, "_searxng_results", AsyncMock(return_value=[])):
            url, tier = await pipeline._discover_careers_url(1, client)
        assert tier == "sitemap"
        assert url == "https://acme.com/jobs/"

    @pytest.mark.asyncio
    async def test_owndomain_homepage_and_sitemap_hit_never_calls_searxng(self):
        """BLOCKER 1: a homepage careers link AND a sitemap hit must short-circuit
        before the SearXNG loop is even entered — not merely go unused once
        fetched. Measured regression: this exact combination still produced
        three SearXNG calls before the fix."""
        client = _client(website="https://acme.com")
        html = '<html><body><a href="https://acme.com/karriere">Karriere</a></body></html>'
        searx = AsyncMock()
        with patch.object(pipeline, "_fetch_page_raw", AsyncMock(return_value=("", html))), \
             patch.object(pipeline, "_sitemap_job_urls",
                           AsyncMock(return_value=[("Backend Engineer",
                                                     "https://acme.com/jobs/backend-engineer-1")])), \
             patch.object(pipeline, "_searxng_results", searx):
            candidates = await pipeline._careers_candidates(1, client)
        assert candidates
        assert all(c["tier"] != "searxng" for c in candidates)
        searx.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lone_offdomain_candidate_rejected_not_returned_unchecked(self):
        """BLOCKER 2: the len(candidates) == 1 short-circuit must be
        constrained too — an off-domain lone SearXNG hit (e.g. a job-board
        listing, not the client's own careers page) must not be handed back
        unchecked, since it would then persist in clients.metadata.careers_url
        forever."""
        client = _client(website="https://acme.com")
        with patch.object(pipeline, "_careers_candidates",
                           AsyncMock(return_value=[{"url": "https://www.stepstone.de/jobs/acme",
                                                     "tier": "searxng",
                                                     "title": "Acme jobs on Stepstone"}])):
            url, tier = await pipeline._discover_careers_url(1, client)
        assert (url, tier) == ("", "")

    @pytest.mark.asyncio
    async def test_own_jobs_subdomain_accepted_via_owndomain_not_ats(self):
        """jobs.miele.de is miele.de's own subdomain — accepted through
        _own_or_ats's own-domain arm, independent of _ats_match no longer
        recognising the generic word "jobs" as an ATS vendor."""
        client = _client(website="https://miele.de")
        with patch.object(pipeline, "_careers_candidates",
                           AsyncMock(return_value=[{"url": "https://jobs.miele.de/stellenangebote",
                                                     "tier": "searxng", "title": ""}])):
            url, tier = await pipeline._discover_careers_url(1, client)
        assert url == "https://jobs.miele.de/stellenangebote"

    @pytest.mark.asyncio
    async def test_lone_jobs_de_or_evil_jobs_com_candidate_rejected(self):
        """Re-review BLOCKER, measured regression: before "jobs." was removed
        from _ATS_HOSTS, an unrelated jobs.de job board and an attacker's
        evil.jobs.com subdomain both passed _own_or_ats for a miele.de
        client and would have been persisted via the single-candidate
        short-circuit."""
        client = _client(website="https://miele.de")
        for bad_url in ("https://www.jobs.de/miele-stellenangebote", "https://evil.jobs.com/miele"):
            with patch.object(pipeline, "_careers_candidates",
                               AsyncMock(return_value=[{"url": bad_url, "tier": "searxng", "title": ""}])):
                url, tier = await pipeline._discover_careers_url(1, client)
            assert (url, tier) == ("", ""), bad_url

    @pytest.mark.asyncio
    async def test_heuristic_fallback_credits_ats_host_with_no_careers_keyword(self):
        """NIT: a bare ATS URL with no careers-ish keyword in it (e.g. just
        "https://company.personio.de/") scored 0 in the heuristic fallback
        and was dropped when the LLM is unavailable; _ats_match must count
        on its own, same weight as own-domain, so it survives."""
        client = _client(website="https://acme.com")
        candidates = [
            {"url": "https://company.personio.de/", "tier": "searxng", "title": "Company Personio"},
            {"url": "https://boards.greenhouse.io/other", "tier": "searxng", "title": "Other co"},
        ]
        with patch.object(pipeline, "_careers_candidates", AsyncMock(return_value=candidates)), \
             patch.object(pipeline.llm, "acomplete", AsyncMock(side_effect=RuntimeError("llm down"))):
            url, tier = await pipeline._discover_careers_url(1, client)
        assert url == "https://company.personio.de/"


# ---------------------------------------------------------------------------
# D1 — own-domain path-probe tier
# ---------------------------------------------------------------------------

def _patch_httpx_dynamic(resolver, calls=None):
    """Like _patch_httpx_sitemap but per-URL: `resolver(url) -> MagicMock`
    (a fake httpx.Response). `calls`, if given, is a list every requested URL
    is appended to."""
    async def _get(url, *_a, **_kw):
        if calls is not None:
            calls.append(url)
        return resolver(url)

    client = MagicMock()
    client.get = AsyncMock(side_effect=_get)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return patch.object(pipeline.httpx, "AsyncClient", return_value=ctx)


def _fake_response(status_code=200, text="", url=None):
    resp = MagicMock(status_code=status_code, text=text)
    resp.url = url if url is not None else ""
    return resp


class TestPathProbeTier:
    @pytest.mark.asyncio
    async def test_finds_karriere_path_when_homepage_503_and_sitemap_empty(self):
        """D1, measured regression (WP7 D1): trumpf.com's homepage 503s and
        its root sitemap is empty, but /de_DE/karriere/ is a real page — the
        path-probe tier must find it without ever calling SearXNG."""
        client = _client(website="https://trumpf.com")
        hit_url = "https://trumpf.com/de_DE/karriere/"
        hit_html = ("<html><head><title>Karriere bei TRUMPF</title></head><body>"
                    + ("Aktuelle Stellenangebote und Karrieremöglichkeiten. " * 20)
                    + "</body></html>")

        def _resolver(url):
            if url == hit_url:
                return _fake_response(200, hit_html, url=hit_url)
            return _fake_response(404, "", url=url)

        searx = AsyncMock()
        with patch.object(pipeline, "_fetch_page_raw", AsyncMock(return_value=("", ""))), \
             patch.object(pipeline, "_sitemap_job_urls", AsyncMock(return_value=[])), \
             patch.object(pipeline, "_searxng_results", searx), \
             _patch_httpx_dynamic(_resolver):
            candidates = await pipeline._careers_candidates(1, client)

        hit = next((c for c in candidates if c["url"] == hit_url), None)
        assert hit is not None
        assert hit["tier"] == "path-probe"
        searx.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_redirect_to_ats_host_accepted(self):
        """A path probe that redirects to a known ATS host is accepted on
        that basis alone — no content/keyword check needed."""
        client = _client(website="https://acme.com")
        probed_url = "https://acme.com/karriere"  # _CAREERS_PATHS[0]
        final_url = "https://acme.wd3.myworkdayjobs.com/en-US/Acme"

        def _resolver(url):
            if url == probed_url:
                return _fake_response(200, "", url=final_url)
            return _fake_response(404, "", url=url)

        with patch.object(pipeline, "_fetch_page_raw", AsyncMock(return_value=("", ""))), \
             patch.object(pipeline, "_sitemap_job_urls", AsyncMock(return_value=[])), \
             _patch_httpx_dynamic(_resolver):
            candidates = await pipeline._careers_candidates(1, client)

        hit = next((c for c in candidates if c["url"] == final_url), None)
        assert hit is not None
        assert hit["tier"] == "path-probe"

    @pytest.mark.asyncio
    async def test_caps_total_probes_at_ten(self):
        client = _client(website="https://acme.com")
        calls: list = []
        with patch.object(pipeline, "_fetch_page_raw", AsyncMock(return_value=("", ""))), \
             patch.object(pipeline, "_sitemap_job_urls", AsyncMock(return_value=[])), \
             patch.object(pipeline, "_searxng_results", AsyncMock(return_value=[])), \
             _patch_httpx_dynamic(lambda u: _fake_response(404, "", url=u), calls=calls):
            await pipeline._careers_candidates(1, client)
        assert len(calls) <= 10

    @pytest.mark.asyncio
    async def test_recently_blocked_probe_path_is_skipped(self):
        """D7: a path already recorded as blocked within the last 14 days is
        never re-probed."""
        client = _client(website="https://acme.com")
        blocked_url = "https://acme.com/karriere"
        pb = {"blocked_urls": [{"url": blocked_url, "kind": "403",
                                 "at": datetime.now(timezone.utc).isoformat()}]}
        calls: list = []
        with patch.object(pipeline, "_fetch_page_raw", AsyncMock(return_value=("", ""))), \
             patch.object(pipeline, "_sitemap_job_urls", AsyncMock(return_value=[])), \
             patch.object(pipeline, "_searxng_results", AsyncMock(return_value=[])), \
             _patch_httpx_dynamic(lambda u: _fake_response(404, "", url=u), calls=calls):
            await pipeline._careers_candidates(1, client, pb)
        assert blocked_url not in calls

    @pytest.mark.asyncio
    async def test_stale_blocked_probe_path_is_retried(self):
        """A block older than 14 days no longer suppresses the probe."""
        client = _client(website="https://acme.com")
        blocked_url = "https://acme.com/karriere"
        old_at = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        pb = {"blocked_urls": [{"url": blocked_url, "kind": "403", "at": old_at}]}
        calls: list = []
        with patch.object(pipeline, "_fetch_page_raw", AsyncMock(return_value=("", ""))), \
             patch.object(pipeline, "_sitemap_job_urls", AsyncMock(return_value=[])), \
             patch.object(pipeline, "_searxng_results", AsyncMock(return_value=[])), \
             _patch_httpx_dynamic(lambda u: _fake_response(404, "", url=u), calls=calls):
            await pipeline._careers_candidates(1, client, pb)
        assert blocked_url in calls


# ---------------------------------------------------------------------------
# D2 — a Pi-run careers/ATS candidate ranks between playbook and metadata
# ---------------------------------------------------------------------------

class TestPiRunCandidateTier:
    @pytest.mark.asyncio
    async def test_pi_run_candidate_used_when_nothing_fresher(self):
        client = _client(website="https://acme.com")
        pb = {"careers": {"candidate_url": "https://acme.wd3.myworkdayjobs.com/en-US/Acme",
                           "candidate_source": "pi-run"}}
        with patch.object(pipeline, "_fetch_page_raw", AsyncMock(return_value=("", ""))), \
             patch.object(pipeline, "_sitemap_job_urls", AsyncMock(return_value=[])), \
             patch.object(pipeline, "_searxng_results", AsyncMock(return_value=[])):
            candidates = await pipeline._careers_candidates(1, client, pb)
        assert candidates[0]["url"] == "https://acme.wd3.myworkdayjobs.com/en-US/Acme"
        assert candidates[0]["tier"] == "pi-run"

    @pytest.mark.asyncio
    async def test_pi_run_candidate_ranks_above_metadata(self):
        client = _client(website="https://acme.com", careers_url="https://acme.com/some-old-page")
        pb = {"careers": {"candidate_url": "https://acme.wd3.myworkdayjobs.com/en-US/Acme"}}
        with patch.object(pipeline, "_fetch_page_raw", AsyncMock(return_value=("", ""))), \
             patch.object(pipeline, "_sitemap_job_urls", AsyncMock(return_value=[])), \
             patch.object(pipeline, "_searxng_results", AsyncMock(return_value=[])):
            candidates = await pipeline._careers_candidates(1, client, pb)
        tiers = [c["tier"] for c in candidates]
        assert tiers.index("pi-run") < tiers.index("metadata")

    @pytest.mark.asyncio
    async def test_fresh_playbook_url_still_short_circuits_before_pi_run(self):
        client = _client(website="https://acme.com")
        pb = {"careers": {"url": "https://acme.com/karriere", "tier": "homepage",
                           "candidate_url": "https://acme.wd3.myworkdayjobs.com/en-US/Acme",
                           "last_success_at": datetime.now(timezone.utc).isoformat()}}
        with patch.object(pipeline, "_fetch_page_raw", AsyncMock(return_value=("", ""))), \
             patch.object(pipeline, "_sitemap_job_urls", AsyncMock(return_value=[])):
            candidates = await pipeline._careers_candidates(1, client, pb)
        assert candidates == [{"url": "https://acme.com/karriere", "tier": "playbook", "title": ""}]


# ---------------------------------------------------------------------------
# WP4: approved cross-site lessons (scope='jobs') inlined into jobs prompts
# ---------------------------------------------------------------------------

def _fake_playbook_with_lessons(lessons):
    """A fake `playbook` module for sys.modules — lessons_load returns the
    fixture, lessons_block is the REAL function so scope/status filtering is
    exercised end to end, not just the plumbing that calls it."""
    fake = MagicMock()
    fake.lessons_load = AsyncMock(return_value=lessons)
    fake.lessons_block = playbook.lessons_block
    return fake


_MULTI_CANDIDATES = [
    {"url": "https://acme.com/karriere", "tier": "searxng", "title": "Acme Karriere"},
    {"url": "https://boards.greenhouse.io/acme", "tier": "searxng", "title": "Acme on Greenhouse"},
]


class TestLessonsInCareersSelectionPrompt:
    @pytest.mark.asyncio
    async def test_approved_jobs_lesson_appears_proposed_does_not(self, monkeypatch):
        lessons = [
            {"text": "Prefer the ATS link over a homepage careers page", "scope": "jobs", "status": "approved"},
            {"text": "A merely proposed jobs lesson", "scope": "jobs", "status": "proposed"},
            {"text": "An approved but news-scoped lesson", "scope": "news", "status": "approved"},
        ]
        monkeypatch.setitem(sys.modules, "playbook", _fake_playbook_with_lessons(lessons))
        client = _client(website="https://acme.com")
        acomplete = AsyncMock(return_value="none")
        with patch.object(pipeline, "_careers_candidates", AsyncMock(return_value=_MULTI_CANDIDATES)), \
             patch.object(pipeline.llm, "acomplete", acomplete):
            await pipeline._discover_careers_url(1, client)
        acomplete.assert_awaited_once()
        prompt = acomplete.await_args.args[0]
        assert "Prefer the ATS link over a homepage careers page" in prompt
        assert "A merely proposed jobs lesson" not in prompt
        assert "An approved but news-scoped lesson" not in prompt
        # WP4 re-review nit: rules must land before the final instruction and
        # before the (untrusted, search-result-derived) candidate listing —
        # never after, where a prior version appended them post-format.
        assert prompt.index("Prefer the ATS link over a homepage careers page") \
            < prompt.index("Reply with ONLY the single best URL") \
            < prompt.index("Acme on Greenhouse")

    @pytest.mark.asyncio
    async def test_unchanged_when_org_has_no_lessons_document(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "playbook", _fake_playbook_with_lessons([]))
        client = _client(website="https://acme.com")
        acomplete = AsyncMock(return_value="none")
        with patch.object(pipeline, "_careers_candidates", AsyncMock(return_value=_MULTI_CANDIDATES)), \
             patch.object(pipeline.llm, "acomplete", acomplete):
            await pipeline._discover_careers_url(1, client)
        prompt = acomplete.await_args.args[0]
        assert "Learned rules" not in prompt
        # no dangling text appended: the prompt ends exactly on the candidate listing
        assert prompt.endswith("2. Acme on Greenhouse — https://boards.greenhouse.io/acme")


class TestLessonsInJobsExtractPrompt:
    @pytest.mark.asyncio
    async def test_approved_jobs_lesson_appears_proposed_does_not(self, monkeypatch):
        lessons = [
            {"text": "Skip listings with no location field", "scope": "jobs", "status": "approved"},
            {"text": "A merely proposed jobs lesson", "scope": "jobs", "status": "proposed"},
        ]
        monkeypatch.setitem(sys.modules, "playbook", _fake_playbook_with_lessons(lessons))
        acomplete = AsyncMock(return_value=json.dumps({"positions": [], "inferred_needs": []}))
        with patch.object(pipeline.llm, "acomplete", acomplete):
            await pipeline._extract_jobs("Acme", "x" * 250, 1)
        prompt = acomplete.await_args.args[0]
        assert "Skip listings with no location field" in prompt
        assert "A merely proposed jobs lesson" not in prompt
        # WP4 re-review nit: rules must land before "Return STRICT JSON ONLY"
        # and before {page} (untrusted page text) — never after, where a
        # prior version appended them post-format, landing them inside the
        # untrusted-page region and pushing the JSON contract out of place.
        assert prompt.index("Skip listings with no location field") < prompt.index("Return STRICT JSON")
        assert prompt.index("Return STRICT JSON") < prompt.index("x" * 250)

    @pytest.mark.asyncio
    async def test_unchanged_when_org_has_no_lessons_document(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "playbook", _fake_playbook_with_lessons([]))
        text = "x" * 250
        acomplete = AsyncMock(return_value=json.dumps({"positions": [], "inferred_needs": []}))
        with patch.object(pipeline.llm, "acomplete", acomplete):
            await pipeline._extract_jobs("Acme", text, 1)
        prompt = acomplete.await_args.args[0]
        assert prompt == pipeline._JOBS_EXTRACT_PROMPT.format(
            client="Acme", page=text[:16000], rules=pipeline._rules_block(""),
        )


# ---------------------------------------------------------------------------
# _scan_client_jobs — success path (sitemap ≥1 threshold) + playbook write
# ---------------------------------------------------------------------------

class TestScanClientJobsSuccess:
    @pytest.mark.asyncio
    async def test_single_sitemap_hit_is_trusted_for_extraction(self):
        db_patch, db = _patch_db()
        client = _client(website="https://acme.com", careers_url="https://acme.com/jobs/")
        reply = json.dumps({"positions": [{"title": "Backend Engineer"}],
                             "inferred_needs": ["Scaling backend infrastructure"]})
        with db_patch, \
             patch.object(pipeline, "_sitemap_job_urls",
                           AsyncMock(return_value=[("Backend Engineer",
                                                     "https://acme.com/jobs/backend-engineer-1")])), \
             patch.object(pipeline, "_fetch_posting_title", AsyncMock(return_value="")), \
             patch.object(pipeline.llm, "acomplete", AsyncMock(return_value=reply)):
            summary = await pipeline._scan_client_jobs(1, client)
        assert summary["found"] is True
        assert summary["positions"] == 1
        assert summary["needs"] == 1
        assert summary["tier"] == "metadata"
        assert summary["error"] is None
        # jobs doc written with agent_run_id and the new bookkeeping fields
        _, kwargs = db.index_document.await_args_list[0]
        assert kwargs["metadata"]["attempts"] == 0
        assert kwargs["metadata"]["tier"] == "metadata"

    @pytest.mark.asyncio
    async def test_run_id_passed_to_every_index_document_call(self):
        db_patch, db = _patch_db()
        client = _client(website="https://acme.com", careers_url="https://acme.com/jobs/")
        reply = json.dumps({"positions": [{"title": "Data Engineer"}],
                             "inferred_needs": ["Building a data platform"]})
        with db_patch, \
             patch.object(pipeline, "_sitemap_job_urls",
                           AsyncMock(return_value=[("Data Engineer", "https://acme.com/jobs/data-1")])), \
             patch.object(pipeline, "_fetch_posting_title", AsyncMock(return_value="")), \
             patch.object(pipeline.llm, "acomplete", AsyncMock(return_value=reply)):
            await pipeline._scan_client_jobs(1, client, run_id=99)
        for call in db.index_document.await_args_list:
            assert call.kwargs["agent_run_id"] == 99

    @pytest.mark.asyncio
    async def test_junior_positions_are_filtered_before_storage(self):
        db_patch, db = _patch_db()
        client = _client(website="https://acme.com", careers_url="https://acme.com/jobs/")
        reply = json.dumps({
            "positions": [{"title": "Praktikum Marketing"}, {"title": "Senior Cloud Engineer"}],
            "inferred_needs": [],
        })
        with db_patch, \
             patch.object(pipeline, "_sitemap_job_urls",
                           AsyncMock(return_value=[("x", "https://acme.com/jobs/x-1")])), \
             patch.object(pipeline.llm, "acomplete", AsyncMock(return_value=reply)):
            summary = await pipeline._scan_client_jobs(1, client)
        assert summary["positions"] == 1
        _, kwargs = db.index_document.await_args_list[0]
        titles = [p["title"] for p in kwargs["metadata"]["positions"]]
        assert titles == ["Senior Cloud Engineer"]
        assert kwargs["metadata"]["filtered_out"] == 1


class TestPlaybookIntegration:
    @pytest.mark.asyncio
    async def test_scan_records_success_to_playbook(self, monkeypatch):
        fake = MagicMock()
        fake.load = AsyncMock(return_value=None)
        fake.record = AsyncMock()
        monkeypatch.setitem(sys.modules, "playbook", fake)

        db_patch, db = _patch_db()
        client = _client(website="https://acme.com", careers_url="https://acme.com/jobs/")
        reply = json.dumps({"positions": [{"title": "Cloud Engineer"}], "inferred_needs": []})
        with db_patch, \
             patch.object(pipeline, "_sitemap_job_urls",
                           AsyncMock(return_value=[("Cloud Engineer", "https://acme.com/jobs/cloud-1")])), \
             patch.object(pipeline, "_fetch_posting_title", AsyncMock(return_value="")), \
             patch.object(pipeline.llm, "acomplete", AsyncMock(return_value=reply)):
            summary = await pipeline._scan_client_jobs(1, client, run_id=7)
        assert summary["found"] is True
        fake.record.assert_awaited_once()
        args, kwargs = fake.record.await_args
        assert args[0] == 1
        assert args[1] == "acme.com"
        assert args[2]["careers"]["url"] == "https://acme.com/jobs/"
        assert args[2]["careers"]["last_success_at"]
        # D5: tier == "metadata" here (no playbook on file yet) must never be
        # written back — it would permanently hide any real discovery tier a
        # later scan finds behind an un-downgradeable "metadata".
        assert "tier" not in args[2]["careers"]
        assert kwargs.get("run_id") == 7

    @pytest.mark.asyncio
    async def test_scan_records_failure_to_playbook(self, monkeypatch):
        fake = MagicMock()
        fake.load = AsyncMock(return_value=None)
        fake.record = AsyncMock()
        monkeypatch.setitem(sys.modules, "playbook", fake)

        db_patch, db = _patch_db()
        client = _client(website="https://acme.com")
        with db_patch, \
             patch.object(pipeline, "_discover_careers_url", AsyncMock(return_value=("", ""))):
            summary = await pipeline._scan_client_jobs(1, client)
        assert summary["found"] is False
        fake.record.assert_awaited_once()
        args, _kwargs = fake.record.await_args
        assert "last_failure_at" in args[2]["careers"]


# ---------------------------------------------------------------------------
# D5 — tier precedence, and _record_playbook never downgrades the tier
# ---------------------------------------------------------------------------

class TestTierPrecedenceAndNoDowngrade:
    @pytest.mark.asyncio
    async def test_second_scan_uses_playbook_tier_and_skips_discovery(self, monkeypatch):
        fresh = datetime.now(timezone.utc).isoformat()
        pb_state = {"careers": {"url": "https://acme.com/karriere", "tier": "searxng",
                                 "last_success_at": fresh}}
        fake = MagicMock()
        fake.load = AsyncMock(return_value=pb_state)
        fake.record = AsyncMock()
        monkeypatch.setitem(sys.modules, "playbook", fake)

        db_patch, db = _patch_db()
        # metadata also carries a (stale/irrelevant) careers_url — the fresh
        # playbook URL must win over it, per the D5 precedence order.
        client = _client(website="https://acme.com", careers_url="https://acme.com/karriere")
        reply = json.dumps({"positions": [{"title": "Cloud Engineer"}], "inferred_needs": []})
        discover = AsyncMock(side_effect=AssertionError("discovery must be skipped"))
        with db_patch, \
             patch.object(pipeline, "_discover_careers_url", discover), \
             patch.object(pipeline, "_sitemap_job_urls",
                           AsyncMock(return_value=[("Cloud Engineer", "https://acme.com/karriere/cloud-1")])), \
             patch.object(pipeline, "_fetch_posting_title", AsyncMock(return_value="")), \
             patch.object(pipeline.llm, "acomplete", AsyncMock(return_value=reply)):
            summary = await pipeline._scan_client_jobs(1, client)

        assert summary["tier"] == "playbook"
        discover.assert_not_awaited()
        fake.record.assert_awaited_once()
        args, _ = fake.record.await_args
        careers_patch = args[2]["careers"]
        # The stored tier ("searxng" — the real, original discovery) must not
        # be downgraded to "playbook".
        assert "tier" not in careers_patch
        assert careers_patch["last_success_at"]

    @pytest.mark.asyncio
    async def test_argument_url_beats_metadata_and_is_recorded_as_argument_tier(self, monkeypatch):
        fake = MagicMock()
        fake.load = AsyncMock(return_value=None)
        fake.record = AsyncMock()
        monkeypatch.setitem(sys.modules, "playbook", fake)

        db_patch, db = _patch_db()
        client = _client(website="https://acme.com", careers_url="https://acme.com/old-metadata-url")
        reply = json.dumps({"positions": [{"title": "Cloud Engineer"}], "inferred_needs": []})
        with db_patch, \
             patch.object(pipeline, "_sitemap_job_urls",
                           AsyncMock(return_value=[("Cloud Engineer", "https://acme.com/karriere/cloud-1")])), \
             patch.object(pipeline, "_fetch_posting_title", AsyncMock(return_value="")), \
             patch.object(pipeline.llm, "acomplete", AsyncMock(return_value=reply)):
            summary = await pipeline._scan_client_jobs(1, client, careers_url="https://acme.com/karriere")

        assert summary["tier"] == "argument"
        args, _ = fake.record.await_args
        assert args[2]["careers"]["tier"] == "argument"

    @pytest.mark.asyncio
    async def test_genuine_discovery_tier_written_and_discovered_tier_set_once(self, monkeypatch):
        fake = MagicMock()
        fake.load = AsyncMock(return_value=None)  # no playbook yet
        fake.record = AsyncMock()
        monkeypatch.setitem(sys.modules, "playbook", fake)

        db_patch, db = _patch_db()
        client = _client(website="https://acme.com")  # no careers_url anywhere -> real discovery
        reply = json.dumps({"positions": [{"title": "Cloud Engineer"}], "inferred_needs": []})
        with db_patch, \
             patch.object(pipeline, "_discover_careers_url",
                           AsyncMock(return_value=("https://acme.com/jobs/", "sitemap"))), \
             patch.object(pipeline, "_sitemap_job_urls",
                           AsyncMock(return_value=[("Cloud Engineer", "https://acme.com/jobs/cloud-1")])), \
             patch.object(pipeline, "_fetch_posting_title", AsyncMock(return_value="")), \
             patch.object(pipeline.llm, "acomplete", AsyncMock(return_value=reply)):
            summary = await pipeline._scan_client_jobs(1, client)

        assert summary["tier"] == "sitemap"
        args, _ = fake.record.await_args
        careers_patch = args[2]["careers"]
        assert careers_patch["tier"] == "sitemap"
        assert careers_patch["discovered_tier"] == "sitemap"

    @pytest.mark.asyncio
    async def test_discovered_tier_never_overwritten_once_set(self, monkeypatch):
        pb_state = {"careers": {"discovered_tier": "searxng"}}
        fake = MagicMock()
        fake.load = AsyncMock(return_value=pb_state)
        fake.record = AsyncMock()
        monkeypatch.setitem(sys.modules, "playbook", fake)

        db_patch, db = _patch_db()
        client = _client(website="https://acme.com")
        reply = json.dumps({"positions": [{"title": "Cloud Engineer"}], "inferred_needs": []})
        with db_patch, \
             patch.object(pipeline, "_discover_careers_url",
                           AsyncMock(return_value=("https://acme.com/jobs/", "sitemap"))), \
             patch.object(pipeline, "_sitemap_job_urls",
                           AsyncMock(return_value=[("Cloud Engineer", "https://acme.com/jobs/cloud-1")])), \
             patch.object(pipeline, "_fetch_posting_title", AsyncMock(return_value="")), \
             patch.object(pipeline.llm, "acomplete", AsyncMock(return_value=reply)):
            await pipeline._scan_client_jobs(1, client)

        args, _ = fake.record.await_args
        careers_patch = args[2]["careers"]
        assert careers_patch["tier"] == "sitemap"  # tier itself DOES update
        assert "discovered_tier" not in careers_patch  # already set — left alone


# ---------------------------------------------------------------------------
# D7 — own-domain path-probe failures feed playbook.blocked_urls
# ---------------------------------------------------------------------------

class TestBlockedUrlsFeedback:
    @pytest.mark.asyncio
    async def test_own_domain_403_recorded_once_to_playbook(self, monkeypatch):
        fake = MagicMock()
        fake.load = AsyncMock(return_value=None)
        fake.record = AsyncMock()
        monkeypatch.setitem(sys.modules, "playbook", fake)

        db_patch, db = _patch_db()
        client = _client(website="https://acme.com")
        blocked_url = "https://acme.com/karriere"  # _CAREERS_PATHS[0]

        def _resolver(url):
            if url == blocked_url:
                return _fake_response(403, "", url=url)
            return _fake_response(404, "", url=url)

        with db_patch, \
             patch.object(pipeline, "_fetch_page_raw", AsyncMock(return_value=("", ""))), \
             patch.object(pipeline, "_sitemap_job_urls", AsyncMock(return_value=[])), \
             patch.object(pipeline, "_searxng_results", AsyncMock(return_value=[])), \
             _patch_httpx_dynamic(_resolver):
            summary = await pipeline._scan_client_jobs(1, client)

        assert summary["found"] is False
        fake.record.assert_awaited_once()
        args, _ = fake.record.await_args
        blocked = args[2].get("blocked_urls") or []
        matches = [b for b in blocked if b["url"] == blocked_url]
        assert len(matches) == 1
        assert matches[0]["kind"] == "403"


# ---------------------------------------------------------------------------
# _scan_client_jobs — failure stamping never clobbers a good scan
# ---------------------------------------------------------------------------

class TestFailureStamping:
    @pytest.mark.asyncio
    async def test_failure_stamps_existing_doc_without_overwriting(self):
        db_patch, db = _patch_db()
        prior_meta = {"positions": [{"title": "Existing Good Role"}],
                      "careers_url": "https://acme.com/karriere", "attempts": 2}
        db.get_document = AsyncMock(return_value={"metadata": prior_meta})
        client = _client(website="https://acme.com")
        with db_patch, \
             patch.object(pipeline, "_discover_careers_url", AsyncMock(return_value=("", ""))):
            summary = await pipeline._scan_client_jobs(1, client)
        assert summary["found"] is False
        assert summary["error"]
        db.update_document.assert_awaited_once()
        args = db.update_document.await_args.args
        assert args[0] == 1 and args[1] == "jobs-1"
        patch_meta = args[2]["metadata"]
        assert patch_meta["attempts"] == 3
        assert patch_meta["last_error"]
        db.index_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failure_with_no_prior_doc_creates_empty_placeholder(self):
        db_patch, db = _patch_db()
        db.get_document = AsyncMock(return_value=None)
        client = _client(website="https://acme.com")
        with db_patch, \
             patch.object(pipeline, "_discover_careers_url", AsyncMock(return_value=("", ""))):
            summary = await pipeline._scan_client_jobs(1, client)
        assert summary["found"] is False
        db.update_document.assert_not_awaited()
        db.index_document.assert_awaited_once()
        _, kwargs = db.index_document.await_args
        assert kwargs["metadata"]["positions"] == []
        assert kwargs["metadata"]["attempts"] == 1

    @pytest.mark.asyncio
    async def test_failure_placeholder_is_linked_to_client(self):
        """BLOCKER 3: _run_jobs_monitor's rotation query orders by
        MAX(d.updated_at) over documents JOIN document_links (entity_type=
        'client') — an unlinked placeholder is invisible to it, so a
        never-successful client would keep heading the retry queue every run."""
        db_patch, db = _patch_db()
        db.get_document = AsyncMock(return_value=None)
        db.index_document = AsyncMock(return_value=42)
        client = _client(website="https://acme.com")
        with db_patch, \
             patch.object(pipeline, "_discover_careers_url", AsyncMock(return_value=("", ""))):
            summary = await pipeline._scan_client_jobs(1, client)
        assert summary["found"] is False
        db.link_document.assert_awaited_once_with(42, "client", client["id"])

    @pytest.mark.asyncio
    async def test_no_positions_after_fetch_also_stamps_not_overwrites(self):
        db_patch, db = _patch_db()
        prior_meta = {"positions": [{"title": "Existing Good Role"}],
                      "careers_url": "https://acme.com/karriere", "attempts": 0}
        db.get_document = AsyncMock(return_value={"metadata": prior_meta})
        client = _client(website="https://acme.com", careers_url="https://acme.com/karriere")
        with db_patch, \
             patch.object(pipeline, "_sitemap_job_urls", AsyncMock(return_value=[])), \
             patch.object(pipeline, "_fetch_page_raw", AsyncMock(return_value=("", ""))), \
             patch.object(pipeline.llm, "acomplete", AsyncMock(return_value="{}")):
            summary = await pipeline._scan_client_jobs(1, client)
        assert summary["found"] is False
        db.index_document.assert_not_awaited()
        db.update_document.assert_awaited_once()


# ---------------------------------------------------------------------------
# D10 — sitemap-slug titles are replaced by the real posting page's title
# ---------------------------------------------------------------------------

class TestFetchPostingTitle:
    @pytest.mark.asyncio
    async def test_strips_company_suffix_after_dash(self):
        html = "<html><head><title>Senior Backend Engineer - Acme GmbH</title></head></html>"
        with _patch_httpx_dynamic(lambda u: _fake_response(200, html, url=u)):
            title = await pipeline._fetch_posting_title("https://acme.com/jobs/x")
        assert title == "Senior Backend Engineer"

    @pytest.mark.asyncio
    async def test_strips_company_suffix_after_pipe(self):
        html = "<html><head><title>Data Engineer | Acme</title></head></html>"
        with _patch_httpx_dynamic(lambda u: _fake_response(200, html, url=u)):
            title = await pipeline._fetch_posting_title("https://acme.com/jobs/y")
        assert title == "Data Engineer"

    @pytest.mark.asyncio
    async def test_falls_back_to_h1_when_title_missing(self):
        html = "<html><body><h1>Platform Engineer (w/m/d)</h1></body></html>"
        with _patch_httpx_dynamic(lambda u: _fake_response(200, html, url=u)):
            title = await pipeline._fetch_posting_title("https://acme.com/jobs/z")
        assert title == "Platform Engineer (w/m/d)"

    @pytest.mark.asyncio
    async def test_returns_empty_on_non_200(self):
        with _patch_httpx_dynamic(lambda u: _fake_response(404, "", url=u)):
            title = await pipeline._fetch_posting_title("https://acme.com/jobs/gone")
        assert title == ""

    @pytest.mark.asyncio
    async def test_returns_empty_on_connection_error(self):
        client = MagicMock()
        client.get = AsyncMock(side_effect=RuntimeError("boom"))
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=client)
        ctx.__aexit__ = AsyncMock(return_value=False)
        with patch.object(pipeline.httpx, "AsyncClient", return_value=ctx):
            title = await pipeline._fetch_posting_title("https://acme.com/jobs/down")
        assert title == ""


class TestSlugTitleReplacedByPageTitle:
    @pytest.mark.asyncio
    async def test_slug_title_replaced_by_real_posting_page_title(self):
        db_patch, db = _patch_db()
        client = _client(website="https://acme.com", careers_url="https://acme.com/jobs/")
        reply = json.dumps({"positions": [{"title": "IT Solution Architect Customer Serv"}],
                             "inferred_needs": []})
        posting_url = "https://acme.com/jobs/it-solution-architect-customer-service-123"
        with db_patch, \
             patch.object(pipeline, "_sitemap_job_urls",
                           AsyncMock(return_value=[("IT Solution Architect Customer Serv", posting_url)])), \
             patch.object(pipeline.llm, "acomplete", AsyncMock(return_value=reply)), \
             patch.object(pipeline, "_fetch_posting_title",
                           AsyncMock(return_value="IT Solution Architect Customer Service")):
            summary = await pipeline._scan_client_jobs(1, client)

        assert summary["positions"] == 1
        _, kwargs = db.index_document.await_args_list[0]
        position = kwargs["metadata"]["positions"][0]
        assert position["title"] == "IT Solution Architect Customer Service"
        assert position["title_source"] == "page"
        assert position["url"] == posting_url

    @pytest.mark.asyncio
    async def test_slug_title_kept_when_posting_page_fetch_finds_nothing(self):
        db_patch, db = _patch_db()
        client = _client(website="https://acme.com", careers_url="https://acme.com/jobs/")
        reply = json.dumps({"positions": [{"title": "Backend Engineer"}], "inferred_needs": []})
        with db_patch, \
             patch.object(pipeline, "_sitemap_job_urls",
                           AsyncMock(return_value=[("Backend Engineer", "https://acme.com/jobs/backend-1")])), \
             patch.object(pipeline.llm, "acomplete", AsyncMock(return_value=reply)), \
             patch.object(pipeline, "_fetch_posting_title", AsyncMock(return_value="")):
            summary = await pipeline._scan_client_jobs(1, client)

        assert summary["positions"] == 1
        _, kwargs = db.index_document.await_args_list[0]
        position = kwargs["metadata"]["positions"][0]
        assert position["title"] == "Backend Engineer"
        assert position["title_source"] == "slug"

    @pytest.mark.asyncio
    async def test_at_most_eight_posting_pages_fetched(self):
        db_patch, db = _patch_db()
        client = _client(website="https://acme.com", careers_url="https://acme.com/jobs/")
        titles = [f"Engineer Number {i}" for i in range(12)]
        sitemap = [(t, f"https://acme.com/jobs/eng-{i}") for i, t in enumerate(titles)]
        reply = json.dumps({"positions": [{"title": t} for t in titles], "inferred_needs": []})
        fetch_title = AsyncMock(return_value="")
        with db_patch, \
             patch.object(pipeline, "_sitemap_job_urls", AsyncMock(return_value=sitemap)), \
             patch.object(pipeline.llm, "acomplete", AsyncMock(return_value=reply)), \
             patch.object(pipeline, "_fetch_posting_title", fetch_title):
            summary = await pipeline._scan_client_jobs(1, client)

        assert summary["positions"] == 12
        assert fetch_title.await_count <= 8


# ---------------------------------------------------------------------------
# D2 — _run_jobs_monitor's rotation prefers a fresh Pi-run candidate
# ---------------------------------------------------------------------------

class TestJobsMonitorRotationPrefersPiCandidates:
    @pytest.mark.asyncio
    async def test_client_with_fresh_pi_candidate_jumps_the_lru_queue(self, monkeypatch):
        clients = [
            {"id": 1, "name": "Stale Co", "metadata": {"website": "https://stale.com"}},
            {"id": 2, "name": "Candidate Co", "metadata": {"website": "https://candidate.com"}},
            {"id": 3, "name": "Other Co", "metadata": {"website": "https://other.com"}},
        ]
        db = MagicMock()
        db.list_clients = AsyncMock(return_value=clients)
        db._pool = None  # no LRU timestamps -> original (id) order absent prioritization

        fake_playbook = MagicMock()
        pb_by_domain = {
            "candidate.com": {"careers": {"candidate_at": datetime.now(timezone.utc).isoformat()}},
        }

        async def _load(_org_id, domain):
            return pb_by_domain.get(domain)

        fake_playbook.load = AsyncMock(side_effect=_load)
        monkeypatch.setitem(sys.modules, "playbook", fake_playbook)

        scanned_order: list = []

        async def _fake_scan(_org_id, c, **_kw):
            scanned_order.append(c["name"])
            return {"client": c["name"], "found": False, "positions": 0, "needs": 0}

        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "_scan_client_jobs", AsyncMock(side_effect=_fake_scan)), \
             patch.object(pipeline.context, "config", {"jobs_max_per_run": 1}):
            await pipeline._run_jobs_monitor(1)

        # Only 1 "other" slot (jobs_max_per_run=1) and no focus clients — the
        # client with the fresh pi-run candidate must win it over "Stale Co",
        # which would otherwise be first (stable sort, no LRU data at all).
        assert scanned_order == ["Candidate Co"]

    @pytest.mark.asyncio
    async def test_candidate_with_confirmed_success_does_not_jump_queue(self, monkeypatch):
        clients = [
            {"id": 1, "name": "Stale Co", "metadata": {"website": "https://stale.com"}},
            {"id": 2, "name": "Already Working Co", "metadata": {"website": "https://working.com"}},
        ]
        db = MagicMock()
        db.list_clients = AsyncMock(return_value=clients)
        db._pool = None

        fake_playbook = MagicMock()
        pb_by_domain = {
            # candidate_at is fresh, but last_success_at is already set -> not
            # a rotation-jumping candidate, it's already confirmed working.
            "working.com": {"careers": {"candidate_at": datetime.now(timezone.utc).isoformat(),
                                          "last_success_at": datetime.now(timezone.utc).isoformat()}},
        }

        async def _load(_org_id, domain):
            return pb_by_domain.get(domain)

        fake_playbook.load = AsyncMock(side_effect=_load)
        monkeypatch.setitem(sys.modules, "playbook", fake_playbook)

        scanned_order: list = []

        async def _fake_scan(_org_id, c, **_kw):
            scanned_order.append(c["name"])
            return {"client": c["name"], "found": False, "positions": 0, "needs": 0}

        with patch.object(pipeline, "db_module", db), \
             patch.object(pipeline, "_scan_client_jobs", AsyncMock(side_effect=_fake_scan)), \
             patch.object(pipeline.context, "config", {"jobs_max_per_run": 1}):
            await pipeline._run_jobs_monitor(1)

        assert scanned_order == ["Stale Co"]


# ---------------------------------------------------------------------------
# Guard: no bare _call_brain_sync in the jobs block
# ---------------------------------------------------------------------------

class TestNoBareBrainCallInJobsBlock:
    def test_no_call_brain_sync_between_careers_keys_and_run_jobs_monitor(self):
        import pathlib
        src = pathlib.Path(pipeline.__file__).read_text()
        start = src.index("_CAREERS_KEYS = (")
        end = src.index("async def _run_jobs_monitor")
        block = src[start:end]
        assert "_call_brain_sync(" not in block


# ---------------------------------------------------------------------------
# knowledge.py — Hiring Signals section + [JOBS] context block
# ---------------------------------------------------------------------------

def _fake_pool(contact_rows, doc_rows, industry_rows=None):
    fetch_results = [contact_rows, doc_rows]
    if industry_rows is not None:
        fetch_results.append(industry_rows)
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=fetch_results)
    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_ctx)
    return pool


def test_brief_prompt_has_hiring_section():
    assert "## Hiring Signals" in knowledge._BRIEF_PROMPT
    assert "(no data yet)" in knowledge._BRIEF_PROMPT
    assert "## Active Signals" in knowledge._BRIEF_PROMPT


class TestBriefContextJobs:
    @pytest.mark.asyncio
    async def test_brief_context_has_jobs_block(self):
        client = {"id": 5, "name": "Acme", "metadata": {}, "session_count": 3}
        jobs_row = {
            "type": "jobs", "title": "Open positions — Acme", "content": "ignored",
            "metadata": {
                "careers_url": "https://acme.com/karriere",
                "positions": [{"title": "Head of IT", "team": "IT", "location": "Berlin"}],
                "inferred_needs": ["Scaling cloud infrastructure"],
                "last_scanned": "2026-09-01T00:00:00+00:00",
            },
            "created_at": "2026-09-01",
        }
        pool = _fake_pool(contact_rows=[], doc_rows=[jobs_row])
        with patch.object(knowledge.db_module, "_pool", pool):
            context_str = await knowledge._build_brief_context(1, client)
        assert context_str.count("[JOBS]") == 1
        assert "https://acme.com/karriere" in context_str
        assert "Head of IT" in context_str
        assert "Scaling cloud infrastructure" in context_str
        assert "2026-09-01T00:00:00+00:00" in context_str

    @pytest.mark.asyncio
    async def test_jobs_doc_not_duplicated_in_generic_loop(self):
        client = {"id": 5, "name": "Acme", "metadata": {}}
        jobs_row = {"type": "jobs", "title": "Open positions — Acme", "content": "raw content",
                    "metadata": {"careers_url": "https://acme.com/karriere", "positions": []},
                    "created_at": "2026-09-01"}
        pool = _fake_pool(contact_rows=[], doc_rows=[jobs_row])
        with patch.object(knowledge.db_module, "_pool", pool):
            context_str = await knowledge._build_brief_context(1, client)
        assert "[JOBS]" not in context_str.replace("[JOBS]", "", 1)  # exactly one occurrence
        assert "raw content" not in context_str  # generic loop skipped this doc entirely

    @pytest.mark.asyncio
    async def test_signal_header_includes_published_date(self):
        client = {"id": 5, "name": "Acme", "metadata": {}}
        signal_row = {"type": "signal", "title": "CEO change", "content": "Acme names a new CEO.",
                      "metadata": {"published_at": "2026-08-01", "source_url": "https://news.example/x"},
                      "created_at": "2026-08-02"}
        pool = _fake_pool(contact_rows=[], doc_rows=[signal_row])
        with patch.object(knowledge.db_module, "_pool", pool):
            context_str = await knowledge._build_brief_context(1, client)
        assert "Published: 2026-08-01" in context_str
        assert "Source: https://news.example/x" in context_str
