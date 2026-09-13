"""
tests/test_playbook.py — WP4: site playbook (self-improving agent) + human-
approved cross-site lessons.

Covers playbook.py (record/merge/bounds, render_block, classify_tool_calls,
reflect_on_run, enrich_task, resolve_client_exact, lessons propose/decide/
block), the routers/agents.py `_fire_agent_service` reader, and
routers/lessons.py's admin-only decision endpoint.

DB is faked with MagicMock/AsyncMock (pattern: tests/test_source_monitor.py);
the agent-service POST is captured with a fake httpx client (pattern:
tests/test_llm_subscription.py ~:246); the FastAPI TestClient + dependency
override pattern for auth follows tests/test_pi_agents.py.
"""

import json
import pathlib
import re
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

import playbook


# ---------------------------------------------------------------------------
# record() — merge, bounds, deep-merge
# ---------------------------------------------------------------------------

def _db(get_document=None, list_clients=None):
    db = MagicMock()
    db.get_document = AsyncMock(return_value=get_document)
    db.index_document = AsyncMock(return_value=1)
    db.list_clients = AsyncMock(return_value=list_clients or [])
    db.update_client_metadata = AsyncMock()
    return db


async def test_record_bounds_blocked_urls_to_20_newest():
    urls = [{"url": f"https://acme.com/{i}", "kind": "403", "at": "t"} for i in range(21)]
    db = _db()
    with patch.object(playbook, "db_module", db):
        merged = await playbook.record(1, "acme.com", {"blocked_urls": urls})
    assert len(merged["blocked_urls"]) == 20
    # oldest (index 0) dropped, newest (index 20) kept
    assert merged["blocked_urls"][0]["url"] == "https://acme.com/1"
    assert merged["blocked_urls"][-1]["url"] == "https://acme.com/20"
    db.index_document.assert_awaited_once()


async def test_record_scalar_overwrite():
    existing = {"metadata": {"domain": "acme.com", "needs_js": False}}
    db = _db(get_document=existing)
    with patch.object(playbook, "db_module", db):
        merged = await playbook.record(1, "acme.com", {"needs_js": True})
    assert merged["needs_js"] is True


async def test_record_deep_merge_keeps_careers_url_on_failure_only_patch():
    existing = {"metadata": {
        "domain": "acme.com",
        "careers": {"url": "https://acme.com/jobs", "tier": "homepage"},
    }}
    db = _db(get_document=existing)
    with patch.object(playbook, "db_module", db):
        merged = await playbook.record(1, "acme.com", {
            "careers": {"last_failure_at": "2026-01-01T00:00:00Z", "error": "timeout"},
        })
    assert merged["careers"]["url"] == "https://acme.com/jobs"
    assert merged["careers"]["tier"] == "homepage"
    assert merged["careers"]["last_failure_at"] == "2026-01-01T00:00:00Z"
    assert merged["careers"]["error"] == "timeout"


async def test_record_appends_run_id_to_sources_of_truth():
    db = _db()
    with patch.object(playbook, "db_module", db):
        merged = await playbook.record(1, "acme.com", {"needs_js": True}, run_id=42)
    assert merged["sources_of_truth"] == [42]


async def test_record_dedupes_blocked_urls_by_url_keeping_newest_at():
    # A repeated block of the same URL must update its `at` in place, not
    # re-append a duplicate entry (the 20-cap would otherwise fill up with
    # copies of one URL — the WP4 review nit).
    existing = {"metadata": {"domain": "acme.com", "blocked_urls": [
        {"url": "https://acme.com/x", "kind": "403", "at": "t0"},
        {"url": "https://acme.com/y", "kind": "403", "at": "t0"},
    ]}}
    db = _db(get_document=existing)
    with patch.object(playbook, "db_module", db):
        merged = await playbook.record(1, "acme.com", {"blocked_urls": [
            {"url": "https://acme.com/x", "kind": "403", "at": "t1"},
        ]})
    assert len(merged["blocked_urls"]) == 2
    by_url = {b["url"]: b for b in merged["blocked_urls"]}
    assert by_url["https://acme.com/x"]["at"] == "t1"
    assert by_url["https://acme.com/y"]["at"] == "t0"


# ---------------------------------------------------------------------------
# _mirror_summary
# ---------------------------------------------------------------------------

async def test_mirror_summary_skips_when_no_client_has_that_domain():
    db = MagicMock()
    db.list_clients = AsyncMock(return_value=[
        {"id": 1, "name": "Other Co", "metadata": {"website": "https://other.com"}},
    ])
    db.update_client_metadata = AsyncMock()
    with patch.object(playbook, "db_module", db):
        await playbook._mirror_summary(1, "acme.com", {"careers": {"url": "https://acme.com/jobs"}})
    db.update_client_metadata.assert_not_called()


# ---------------------------------------------------------------------------
# render_block
# ---------------------------------------------------------------------------

def test_render_block_empty_playbook_returns_empty_string():
    assert playbook.render_block(None) == ""
    assert playbook.render_block({}) == ""


def test_render_block_omits_empty_fields():
    pb = {"domain": "acme.com", "careers": {"url": "https://acme.com/jobs", "tier": "homepage"}}
    block = playbook.render_block(pb)
    assert block.startswith("## Site playbook (learned)")
    assert "Careers page" in block
    assert "Newsroom" not in block
    assert "needs a JS-rendering fetch" not in block
    assert "Cookie wall" not in block


def test_render_block_caps_at_1200_chars():
    pb = {
        "careers": {"url": "https://acme.com/jobs", "tier": "homepage"},
        "good_queries": ["x" * 300] * 5,
        "failed_queries": ["y" * 300] * 5,
        "notes": ["z" * 300] * 4,
    }
    block = playbook.render_block(pb)
    assert len(block) <= 1200


# ---------------------------------------------------------------------------
# classify_tool_calls — pure
# ---------------------------------------------------------------------------

def test_classify_tool_calls_fixture():
    domain = "acme.com"
    tool_calls = [
        {"tool": "web_search", "args": {"query": "acme careers"}, "result": "Title\nurl\nsnippet", "ts": "t0"},
        {"tool": "fetch_page", "args": {"url": "https://acme.com/careers"}, "result": "Open roles: SRE, AE", "ts": "t1"},
        {"tool": "fetch_page", "args": {"url": "https://acme.com/blocked"}, "result": "Error: HTTP 403", "ts": "t2"},
        {"tool": "fetch_page", "args": {"url": "https://acme.com/a"}, "result": "(no readable content)", "ts": "t3"},
        {"tool": "fetch_page", "args": {"url": "https://acme.com/b"}, "result": "(no readable content)", "ts": "t4"},
        {"tool": "fetch_page", "args": {"url": "https://other.example/x"}, "result": "(no readable content)", "ts": "t5"},
        {"tool": "web_search", "args": {"query": "acme executives 2026"},
         "result": "(no results — SearXNG and DDG both returned empty)", "ts": "t6"},
    ]
    result = playbook.classify_tool_calls(tool_calls, domain)

    blocked_kinds = {(b["url"], b["kind"]) for b in result["blocked_urls"]}
    assert ("https://acme.com/blocked", "403") in blocked_kinds
    assert ("https://acme.com/a", "no_content") in blocked_kinds
    assert ("https://acme.com/b", "no_content") in blocked_kinds

    # 2 own-domain no_content fetches -> needs_js; the third no_content is a
    # different (non-own) domain and must not count towards it.
    assert result["needs_js"] is True

    assert "acme executives 2026" in result["failed_queries"]
    # the search at t0 is followed within 3 calls by a clean fetch (t1)
    assert "acme careers" in result["good_queries"]


def test_classify_tool_calls_binary_and_fetch_error_kinds():
    domain = "acme.com"
    tool_calls = [
        {"tool": "fetch_page", "args": {"url": "https://acme.com/x.pdf"},
         "result": "(skipped binary content: application/pdf)", "ts": "t0"},
        {"tool": "fetch_page", "args": {"url": "https://acme.com/down"},
         "result": "Error fetching page: timeout", "ts": "t1"},
        {"tool": "fetch_page", "args": {"url": "https://acme.com/notfound"},
         "result": "Error: HTTP 404", "ts": "t2"},
    ]
    result = playbook.classify_tool_calls(tool_calls, domain)
    kinds = {b["url"]: b["kind"] for b in result["blocked_urls"]}
    assert kinds["https://acme.com/x.pdf"] == "binary"
    assert kinds["https://acme.com/down"] == "fetch_error"
    assert kinds["https://acme.com/notfound"] == "4xx"


# ---------------------------------------------------------------------------
# reflect_on_run
# ---------------------------------------------------------------------------

async def test_reflect_on_run_idempotent_skips_second_record():
    db = MagicMock()
    db.get_agent_run = AsyncMock(return_value={
        "id": 5, "org_id": 1, "status": "done", "output": {"reflected": True}, "tool_calls": [],
    })
    db.update_agent_run = AsyncMock()
    record_mock = AsyncMock()
    with patch.object(playbook, "db_module", db), patch.object(playbook, "record", record_mock):
        await playbook.reflect_on_run(1, 5)
    record_mock.assert_not_called()
    db.update_agent_run.assert_not_called()


async def test_reflect_on_run_first_pass_classifies_and_stamps_reflected():
    tool_calls = [
        {"tool": "fetch_page", "args": {"url": "https://acme.com/careers"}, "result": "Roles: SRE", "ts": "t0"},
        {"tool": "fetch_page", "args": {"url": "https://acme.com/blocked"}, "result": "Error: HTTP 403", "ts": "t1"},
    ]
    db = MagicMock()
    db.get_agent_run = AsyncMock(return_value={
        "id": 7, "org_id": 1, "status": "done", "output": {}, "tool_calls": tool_calls,
    })
    db.update_agent_run = AsyncMock()
    record_mock = AsyncMock(return_value={})
    with patch.object(playbook, "db_module", db), patch.object(playbook, "record", record_mock):
        await playbook.reflect_on_run(1, 7)

    record_mock.assert_awaited_once()
    call_args, call_kwargs = record_mock.await_args
    assert call_args[0] == 1
    assert call_args[1] == "acme.com"
    assert call_kwargs["run_id"] == 7

    db.update_agent_run.assert_awaited_once()
    ua_args, ua_kwargs = db.update_agent_run.await_args
    assert ua_args == (7, "done")
    assert ua_kwargs["output"]["reflected"] is True


async def test_reflect_on_run_majority_third_party_writes_to_client_domain():
    # WP4 review blocker: a run whose fetches are mostly non-aggregator
    # third-party sites (e.g. news coverage of the client) must still write
    # the playbook to the CLIENT's own resolved domain — never to whichever
    # host happened to get fetched most.
    tool_calls = [
        {"tool": "fetch_page", "args": {"url": "https://heise.de/a"}, "result": "content", "ts": "t0"},
        {"tool": "fetch_page", "args": {"url": "https://heise.de/b"}, "result": "content", "ts": "t1"},
        {"tool": "fetch_page", "args": {"url": "https://faz.net/c"}, "result": "content", "ts": "t2"},
        {"tool": "fetch_page", "args": {"url": "https://acme.com/careers"}, "result": "Roles: SRE", "ts": "t3"},
    ]
    db = MagicMock()
    db.get_agent_run = AsyncMock(return_value={
        "id": 10, "org_id": 1, "status": "done", "output": {}, "tool_calls": tool_calls,
    })
    db.get_client = AsyncMock(return_value={
        "id": 1, "name": "Acme GmbH", "metadata": {"website": "https://www.acme.com"},
    })
    db.update_agent_run = AsyncMock()
    record_mock = AsyncMock(return_value={})
    with patch.object(playbook, "db_module", db), patch.object(playbook, "record", record_mock):
        await playbook.reflect_on_run(1, 10, subject="Acme GmbH")

    record_mock.assert_awaited_once()
    call_args, _ = record_mock.await_args
    assert call_args[1] == "acme.com"  # the CLIENT's domain, not heise.de (the majority host)
    db.get_client.assert_awaited_once()


async def test_reflect_on_run_inferred_domain_colliding_with_other_client_rejected():
    # The run's subject doesn't resolve to an exact client, and the
    # most-fetched host happens to be a DIFFERENT client's own domain (e.g. a
    # parent/subsidiary pair). Must be rejected outright, never misattributed
    # onto that other client's playbook (and, via _mirror_summary, its
    # site_playbook_summary).
    tool_calls = [
        {"tool": "fetch_page", "args": {"url": "https://lidl.de/a"}, "result": "content", "ts": "t0"},
        {"tool": "fetch_page", "args": {"url": "https://lidl.de/b"}, "result": "content", "ts": "t1"},
    ]
    db = MagicMock()
    db.get_agent_run = AsyncMock(return_value={
        "id": 11, "org_id": 1, "status": "done", "output": {}, "tool_calls": tool_calls,
    })
    db.get_client = AsyncMock(return_value=None)  # subject does not resolve exactly
    db.list_clients = AsyncMock(return_value=[
        {"id": 2, "name": "Lidl Stiftung", "metadata": {"website": "https://www.lidl.de"}},
    ])
    db.update_agent_run = AsyncMock()
    record_mock = AsyncMock(return_value={})
    with patch.object(playbook, "db_module", db), patch.object(playbook, "record", record_mock):
        await playbook.reflect_on_run(1, 11, subject="Schwarz IT")

    record_mock.assert_not_called()
    db.update_agent_run.assert_not_called()  # no determinable domain -> `reflected` left unset


async def test_reflect_on_run_no_fetch_run_records_nothing_and_leaves_unreflected():
    db = MagicMock()
    db.get_agent_run = AsyncMock(return_value={
        "id": 12, "org_id": 1, "status": "done", "output": {}, "tool_calls": [],
    })
    db.get_client = AsyncMock(return_value={
        "id": 1, "name": "Acme GmbH", "metadata": {"website": "https://www.acme.com"},
    })
    db.update_agent_run = AsyncMock()
    record_mock = AsyncMock()
    with patch.object(playbook, "db_module", db), patch.object(playbook, "record", record_mock):
        await playbook.reflect_on_run(1, 12, subject="Acme GmbH")
    record_mock.assert_not_called()
    db.update_agent_run.assert_not_called()


async def test_reflect_on_run_failed_status_still_records():
    tool_calls = [
        {"tool": "fetch_page", "args": {"url": "https://acme.com/a"}, "result": "Error: HTTP 403", "ts": "t0"},
        {"tool": "fetch_page", "args": {"url": "https://acme.com/b"}, "result": "Error: HTTP 403", "ts": "t1"},
    ]
    db = MagicMock()
    db.get_agent_run = AsyncMock(return_value={
        "id": 13, "org_id": 1, "status": "failed", "output": {}, "tool_calls": tool_calls,
    })
    db.get_client = AsyncMock(return_value={
        "id": 1, "name": "Acme GmbH", "metadata": {"website": "https://www.acme.com"},
    })
    db.update_agent_run = AsyncMock()
    record_mock = AsyncMock(return_value={})
    with patch.object(playbook, "db_module", db), patch.object(playbook, "record", record_mock):
        await playbook.reflect_on_run(1, 13, subject="Acme GmbH")

    record_mock.assert_awaited_once()
    db.update_agent_run.assert_awaited_once()
    ua_args, ua_kwargs = db.update_agent_run.await_args
    assert ua_args == (13, "failed")
    assert ua_kwargs["output"]["reflected"] is True


async def test_reflect_on_run_llm_threshold_seven_no_call_eight_exactly_one():
    def _fixture(n):
        return [
            {"tool": "fetch_page", "args": {"url": f"https://acme.com/{i}"}, "result": "content", "ts": f"t{i}"}
            for i in range(n)
        ]

    db = MagicMock()
    db.get_client = AsyncMock(return_value={
        "id": 1, "name": "Acme GmbH", "metadata": {"website": "https://www.acme.com"},
    })
    db.update_agent_run = AsyncMock()
    record_mock = AsyncMock(return_value={})
    llm_mock = AsyncMock(return_value="[]")

    db.get_agent_run = AsyncMock(return_value={
        "id": 20, "org_id": 1, "status": "done", "output": {}, "tool_calls": _fixture(7),
    })
    with patch.object(playbook, "db_module", db), patch.object(playbook, "record", record_mock), \
         patch.object(playbook.llm, "acomplete", llm_mock):
        await playbook.reflect_on_run(1, 20, subject="Acme GmbH")
    llm_mock.assert_not_called()

    db.get_agent_run = AsyncMock(return_value={
        "id": 21, "org_id": 1, "status": "done", "output": {}, "tool_calls": _fixture(8),
    })
    with patch.object(playbook, "db_module", db), patch.object(playbook, "record", record_mock), \
         patch.object(playbook.llm, "acomplete", llm_mock):
        await playbook.reflect_on_run(1, 21, subject="Acme GmbH")
    llm_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# resolve_client_exact / enrich_task
# ---------------------------------------------------------------------------

async def test_resolve_client_exact_rejects_a_fuzzy_only_match():
    # db.get_client is fuzzy (similarity > 0.6) and would happily return
    # "Acme GmbH" for the query "org" if the trigram score clears the bar.
    db = MagicMock()
    db.get_client = AsyncMock(return_value={"id": 1, "name": "Acme GmbH"})
    with patch.object(playbook, "db_module", db):
        exact = await playbook.resolve_client_exact(1, "Acme GmbH")
        fuzzy = await playbook.resolve_client_exact(1, "org")
    assert exact is not None and exact["name"] == "Acme GmbH"
    assert fuzzy is None


def _pb_and_lessons_db(client, pb_meta, lessons):
    db = MagicMock()
    db.get_client = AsyncMock(return_value=client)

    async def _get_document(org_id, doc_id):
        if doc_id == f"site-playbook-acme.com":
            return {"metadata": pb_meta} if pb_meta else None
        if doc_id == playbook.LESSONS_DOC_ID:
            return {"metadata": {"lessons": lessons}} if lessons else None
        return None

    db.get_document = AsyncMock(side_effect=_get_document)
    return db


async def test_enrich_task_appends_both_blocks_for_exact_match():
    client = {"id": 1, "name": "Acme GmbH", "metadata": {"website": "https://www.acme.com"}}
    pb_meta = {"domain": "acme.com", "needs_js": True,
               "careers": {"url": "https://acme.com/jobs", "tier": "homepage"}}
    lessons = [{"id": "l-1", "text": "Prefer sitemap.xml when blocked", "scope": "research", "status": "approved"}]
    db = _pb_and_lessons_db(client, pb_meta, lessons)
    with patch.object(playbook, "db_module", db):
        task, needs_js = await playbook.enrich_task(1, "Acme GmbH", "Research Acme GmbH", "research")
    assert needs_js is True
    assert "## Site playbook (learned)" in task
    assert "## Learned rules (approved)" in task
    assert "Prefer sitemap.xml when blocked" in task


async def test_enrich_task_untouched_on_fuzzy_only_match():
    client = {"id": 1, "name": "Acme GmbH", "metadata": {"website": "https://www.acme.com"}}
    pb_meta = {"domain": "acme.com", "needs_js": True}
    db = _pb_and_lessons_db(client, pb_meta, [])
    with patch.object(playbook, "db_module", db):
        task, needs_js = await playbook.enrich_task(1, "org", "Monitor all clients", "research")
    assert task == "Monitor all clients"
    assert needs_js is False
    db.get_document.assert_not_called()


# ---------------------------------------------------------------------------
# _fire_agent_service reader (routers/agents.py)
# ---------------------------------------------------------------------------

class _FakeResp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"run_id": 1}


class _FakeHttpxClient:
    def __init__(self, sink):
        self._sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        self._sink.update(json or {})
        return _FakeResp()


async def test_fire_agent_service_sets_use_browser_fetch_and_playbook_block(monkeypatch):
    from routers import agents as ag

    client = {"id": 1, "name": "Acme GmbH", "metadata": {"website": "https://www.acme.com"}}
    pb_meta = {"domain": "acme.com", "needs_js": True,
               "careers": {"url": "https://acme.com/jobs", "tier": "homepage"}}
    db = _pb_and_lessons_db(client, pb_meta, [])

    sent: dict = {}
    monkeypatch.setattr(ag.httpx, "AsyncClient", lambda *a, **k: _FakeHttpxClient(sent))

    with patch.object(playbook, "db_module", db):
        await ag._fire_agent_service("Acme GmbH", 1, brain="openrouter", model="test-model", agent_type="research")

    assert sent["use_browser_fetch"] is True
    assert "## Site playbook (learned)" in sent["task"]


async def test_fire_agent_service_leaves_playbook_out_for_non_research_types(monkeypatch):
    from routers import agents as ag

    db = MagicMock()
    db.get_client = AsyncMock(return_value=None)
    sent: dict = {}
    monkeypatch.setattr(ag.httpx, "AsyncClient", lambda *a, **k: _FakeHttpxClient(sent))

    with patch.object(playbook, "db_module", db):
        await ag._fire_agent_service("Acme GmbH", 1, brain="openrouter", model="test-model",
                                      agent_type="contact_extraction",
                                      task="Extract contacts from recent research findings for Acme GmbH.")

    assert sent["use_browser_fetch"] is False
    db.get_client.assert_not_called()


# ---------------------------------------------------------------------------
# lessons: propose / dedupe / block / decide
# ---------------------------------------------------------------------------

async def test_lessons_propose_dedupes_and_output_is_all_proposed():
    existing_lessons = [{
        "id": "l-aaaaaaaa",
        "text": "Prefer sitemap.xml over homepage crawling when blocked",
        "scope": "all", "status": "approved", "evidence": [],
        "proposed_at": "t", "decided_at": "t", "decided_by": 1, "proposed_by_run": None,
    }]
    db = MagicMock()
    db.list_documents = AsyncMock(return_value=[])
    db.get_agent_activity = AsyncMock(return_value={"runs": []})
    db.get_document = AsyncMock(return_value={"metadata": {"lessons": existing_lessons}})
    db.index_document = AsyncMock(return_value=1)

    candidates = json.dumps([
        {"text": "Prefer sitemap.xml over homepage crawling when a site is blocked",
         "scope": "all", "evidence": ["acme.com"]},
        {"text": "Retry SearXNG once after 20 seconds before giving up on a news search",
         "scope": "news", "evidence": []},
    ])
    llm_mock = AsyncMock(return_value=candidates)

    with patch.object(playbook, "db_module", db), patch.object(playbook.llm, "acomplete", llm_mock):
        result = await playbook.lessons_propose(1, run_id=42)

    assert len(result["proposed"]) == 1  # the near-duplicate was dropped
    assert all(l["status"] == "proposed" for l in result["proposed"])
    assert "Retry SearXNG" in result["proposed"][0]["text"]
    assert result["proposed"][0]["proposed_by_run"] == 42
    db.index_document.assert_awaited_once()


def test_lessons_block_only_injects_approved_matching_scope():
    lessons = [
        {"text": "Approved research rule", "scope": "research", "status": "approved"},
        {"text": "Approved jobs rule", "scope": "jobs", "status": "approved"},
        {"text": "Proposed research rule", "scope": "research", "status": "proposed"},
        {"text": "Rejected research rule", "scope": "research", "status": "rejected"},
        {"text": "Approved all-scope rule", "scope": "all", "status": "approved"},
    ]
    block = playbook.lessons_block(lessons, "research")
    assert "Approved research rule" in block
    assert "Approved all-scope rule" in block
    assert "Approved jobs rule" not in block
    assert "Proposed research rule" not in block
    assert "Rejected research rule" not in block


async def test_lessons_decide_never_auto_approves_without_explicit_call():
    lessons = [{
        "id": "l-1", "text": "Some lesson", "scope": "all", "status": "proposed",
        "evidence": [], "proposed_at": "t", "decided_at": None, "decided_by": None, "proposed_by_run": None,
    }]
    db = MagicMock()
    db.get_document = AsyncMock(return_value={"metadata": {"lessons": lessons}})
    db.index_document = AsyncMock(return_value=1)
    with patch.object(playbook, "db_module", db):
        updated = await playbook.lessons_decide(1, "l-1", "approve", user_id=1)
    assert updated["status"] == "approved"
    assert updated["decided_by"] == 1
    saved_meta = db.index_document.await_args.kwargs["metadata"]
    assert saved_meta["lessons"][0]["status"] == "approved"


# ---------------------------------------------------------------------------
# routers/lessons.py — admin-only decision endpoint
# ---------------------------------------------------------------------------

FAKE_ADMIN = {
    "id": 1, "org_id": 1, "username": "konrad", "display_name": "Konrad",
    "email": "k@test.com", "role": "admin", "org_name": "North", "org_slug": "north",
}
FAKE_MEMBER = {
    "id": 2, "org_id": 1, "username": "sales", "display_name": "Sales Rep",
    "email": "s@test.com", "role": "member", "org_name": "North", "org_slug": "north",
}


@contextmanager
def _client_as(fake_user):
    with (
        patch("server.db_module.init_db", new_callable=AsyncMock),
        patch("server.db_module.close_db", new_callable=AsyncMock),
        patch("server.DB_AVAILABLE", True),
    ):
        from server import app
        from routers.auth import current_user

        async def _fake_user():
            return fake_user

        app.dependency_overrides[current_user] = _fake_user
        try:
            with TestClient(app, raise_server_exceptions=True) as client:
                yield client
        finally:
            app.dependency_overrides.pop(current_user, None)


def test_lesson_decision_non_admin_gets_403():
    with _client_as(FAKE_MEMBER) as client:
        resp = client.post(
            "/api/agents/lessons/l-1/decision",
            json={"decision": "approve"},
            headers={"Authorization": "Bearer fake"},
        )
    assert resp.status_code == 403


def test_lesson_decision_admin_can_approve():
    lessons = [{
        "id": "l-1", "text": "Some lesson", "scope": "all", "status": "proposed",
        "evidence": [], "proposed_at": "t", "decided_at": None, "decided_by": None, "proposed_by_run": None,
    }]
    db = MagicMock()
    db.get_document = AsyncMock(return_value={"metadata": {"lessons": lessons}})
    db.index_document = AsyncMock(return_value=1)
    with patch.object(playbook, "db_module", db), _client_as(FAKE_ADMIN) as client:
        resp = client.post(
            "/api/agents/lessons/l-1/decision",
            json={"decision": "approve"},
            headers={"Authorization": "Bearer fake"},
        )
    assert resp.status_code == 200
    assert resp.json()["lesson"]["status"] == "approved"


def test_review_now_requires_admin():
    with _client_as(FAKE_MEMBER) as client:
        resp = client.post("/api/agents/lessons/review", headers={"Authorization": "Bearer fake"})
    assert resp.status_code == 403


def test_list_lessons_groups_by_status():
    lessons = [
        {"id": "l-1", "text": "a", "scope": "all", "status": "proposed"},
        {"id": "l-2", "text": "b", "scope": "all", "status": "approved"},
        {"id": "l-3", "text": "c", "scope": "all", "status": "rejected"},
    ]
    db = MagicMock()
    db.get_document = AsyncMock(return_value={"metadata": {"lessons": lessons}})
    with patch.object(playbook, "db_module", db), _client_as(FAKE_MEMBER) as client:
        resp = client.get("/api/agents/lessons", headers={"Authorization": "Bearer fake"})
    assert resp.status_code == 200
    data = resp.json()
    assert [l["id"] for l in data["proposed"]] == ["l-1"]
    assert [l["id"] for l in data["approved"]] == ["l-2"]
    assert [l["id"] for l in data["rejected"]] == ["l-3"]


# ---------------------------------------------------------------------------
# scan-style guard: text-only LLM bridge, org_id always threaded through
# ---------------------------------------------------------------------------

def test_playbook_has_no_bare_brain_call_and_every_acomplete_passes_org_id():
    src = (pathlib.Path(__file__).resolve().parent.parent / "playbook.py").read_text()
    assert "_call_brain_sync(" not in src
    calls = re.findall(r"llm\.acomplete\([^)]*\)", src, re.S)
    assert calls, "expected at least one llm.acomplete call in playbook.py"
    for call in calls:
        assert "org_id=" in call, f"missing org_id= in: {call}"
