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
             patch.object(pipeline.llm, "acomplete", AsyncMock(return_value=reply)):
            summary = await pipeline._scan_client_jobs(1, client, run_id=7)
        assert summary["found"] is True
        fake.record.assert_awaited_once()
        args, kwargs = fake.record.await_args
        assert args[0] == 1
        assert args[1] == "acme.com"
        assert args[2]["careers"]["url"] == "https://acme.com/jobs/"
        assert args[2]["careers"]["last_success_at"]
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
