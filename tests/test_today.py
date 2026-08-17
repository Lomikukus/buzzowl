"""
tests/test_today.py — Next-best-action queue (F1, Session 89).

Covers: pure scoring (routers.today.compute_scores), LLM reason prompt/parse/
template fallback, compute_nba_queue orchestration + snapshot persistence,
the /api/next-actions endpoints, and the nba_queue heartbeat dispatch.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from routers import pipeline, today

FAKE_USER = {
    "id": 1, "org_id": 1, "username": "konrad", "display_name": "Konrad",
    "email": "k@test.com", "role": "admin", "org_name": "North", "org_slug": "north",
}

NOW = datetime(2026, 6, 13, 8, 0, tzinfo=timezone.utc)


def _client(name, last_activity=None, **meta):
    return {"id": 1, "name": name, "metadata": meta,
            "session_count": 0, "last_activity": last_activity}


def _signal(client, relevance=3, signal_type="news", title="Some headline",
            source_url="https://example.com/a"):
    return {"doc_id": "sig-1", "title": title, "client": client,
            "signal_type": signal_type, "relevance": str(relevance),
            "source_url": source_url, "created_at": NOW}


def _mail(client, status="generated", sent_days_ago=None, doc_id=80, title="Mail to X"):
    history = []
    if sent_days_ago is not None:
        history = [{"status": "sent", "user_id": 1,
                    "ts": (NOW - timedelta(days=sent_days_ago)).isoformat()}]
    return {"id": doc_id, "title": title, "client": client, "status": status,
            "history": history, "created_at": NOW - timedelta(days=sent_days_ago or 0)}


FRESH = NOW.isoformat()  # last_activity "now" → zero staleness points


def _score(clients, signals=None, outreach=None, **kw):
    return today.compute_scores(clients, signals or {}, outreach or {}, now=NOW, **kw)


# ---------------------------------------------------------------------------
# Pure scoring
# ---------------------------------------------------------------------------

class TestScoring:
    def test_signal_relevance_and_type_bonus(self):
        entries = _score([_client("ACME", FRESH)],
                         {"ACME": [_signal("ACME", relevance=5, signal_type="opportunity")]})
        assert entries[0]["score"] == 5 * 3 + 2
        assert entries[0]["suggested_action"] == "mail"
        fact = entries[0]["facts"][0]
        assert fact["type"] == "signal" and fact["source_url"] == "https://example.com/a"

    def test_top_three_signals_cap(self):
        sigs = [_signal("ACME", relevance=3) for _ in range(5)]
        entries = _score([_client("ACME", FRESH)], {"ACME": sigs})
        assert entries[0]["score"] == 3 * (3 * 3 + 0)
        assert len([f for f in entries[0]["facts"] if f["type"] == "signal"]) == 3

    def test_news_pending_bonus_and_action(self):
        entries = _score([_client("ACME", FRESH, news_pending=True,
                                  news_pending_reason=["Newsroom"])])
        assert entries[0]["score"] == 12
        assert entries[0]["suggested_action"] == "research"
        assert entries[0]["facts"][0]["detail"] == ["Newsroom"]

    def test_draft_mail_wins_action_precedence(self):
        entries = _score([_client("ACME", FRESH, news_pending=True)],
                         outreach={"ACME": [_mail("ACME", status="generated")]})
        assert entries[0]["suggested_action"] == "send_draft"
        assert entries[0]["score"] == 12 + 10
        assert entries[0]["action_link"].endswith("#documents")

    def test_overdue_sent_mail_is_followup(self):
        entries = _score([_client("ACME", FRESH)],
                         outreach={"ACME": [_mail("ACME", status="sent", sent_days_ago=8)]})
        assert entries[0]["suggested_action"] == "follow_up"
        assert entries[0]["score"] == 14
        fact = entries[0]["facts"][0]
        assert fact["type"] == "outreach_followup" and fact["days_since_sent"] == 8

    def test_recent_sent_mail_dampens_and_excludes(self):
        entries = _score([_client("ACME", FRESH)],
                         outreach={"ACME": [_mail("ACME", status="sent", sent_days_ago=2)]})
        assert entries == []  # -6 with nothing else → score <= 0 → excluded

    def test_focus_multiplier(self):
        plain = _score([_client("ACME", FRESH, news_pending=True)])
        focus = _score([_client("ACME", FRESH, news_pending=True, is_focus=True)])
        assert focus[0]["score"] == plain[0]["score"] * 1.5
        assert focus[0]["is_focus"] is True

    def test_staleness_from_text_column_and_cap(self):
        month_old = (NOW - timedelta(days=30)).isoformat()
        entries = _score([_client("ACME", month_old)])
        assert entries[0]["score"] == 2  # 30 // 14 = 2 two-week blocks
        entries = _score([_client("ACME", None)])  # unknown → 60d fallback
        assert entries[0]["score"] == 4  # capped
        entries = _score([_client("ACME", "not-a-date")])
        assert entries[0]["score"] == 4

    def test_zero_score_client_excluded(self):
        assert _score([_client("ACME", FRESH)]) == []

    def test_deterministic_tiebreak_and_truncation(self):
        clients = [_client(n, None) for n in ("Zeta", "Alpha", "Mid")]
        entries = _score(clients, queue_size=2)
        assert [e["client"] for e in entries] == ["Alpha", "Mid"]
        assert [e["rank"] for e in entries] == [1, 2]


# ---------------------------------------------------------------------------
# Reasons: prompt builder, parser, template fallback
# ---------------------------------------------------------------------------

class TestReasons:
    def _entry(self):
        return _score([_client("ACME", FRESH)],
                      {"ACME": [_signal("ACME", relevance=5, signal_type="opportunity",
                                        source_url="https://secret.example/x")]})[0]

    def test_prompt_excludes_links_and_urls(self):
        prompt = today._build_reason_prompt([self._entry()])
        assert "secret.example" not in prompt
        assert '"link"' not in prompt
        assert "ACME" in prompt and "Some headline" in prompt

    def test_parse_handles_fenced_json(self):
        text = '```json\n[{"client": "ACME", "reason": "Call them."}]\n```'
        assert today._parse_reason_json(text) == {"acme": "Call them."}

    def test_parse_raises_on_garbage(self):
        with pytest.raises(Exception):
            today._parse_reason_json("I cannot answer that.")

    def test_template_reason_cites_facts(self):
        draft = _score([_client("ACME", FRESH)],
                       outreach={"ACME": [_mail("ACME", title="Intro mail")]})[0]
        assert "Intro mail" in today._template_reason(draft)
        followup = _score([_client("ACME", FRESH)],
                          outreach={"ACME": [_mail("ACME", status="sent", sent_days_ago=8)]})[0]
        assert "8 days ago" in today._template_reason(followup)
        stale = _score([_client("ACME", None)])[0]
        assert "No activity for 60 days" in today._template_reason(stale)


class TestContactLogScoring:
    """compute_scores now factors in real logged outreach (contact_log)."""

    def test_recent_logged_contact_deprioritises(self):
        clients = [_client("ACME", FRESH)]
        signals = {"ACME": [_signal("ACME", relevance=5, signal_type="opportunity")]}
        base = _score(clients, signals)[0]["score"]
        recent = {"ACME": [{"sent_at": (NOW - timedelta(days=1)).isoformat(), "replied": False}]}
        after = _score(clients, signals, contacts_log_by_client=recent)[0]["score"]
        assert after == base - 6  # recent_contact penalty applied

    def test_aged_unreplied_contact_boosts_followup(self):
        clients = [_client("ACME", FRESH)]
        aged = {"ACME": [{"sent_at": (NOW - timedelta(days=20)).isoformat(), "replied": False}]}
        res = _score(clients, contacts_log_by_client=aged)
        assert res and res[0]["suggested_action"] == "follow_up"
        assert any(f["type"] == "contact_followup" for f in res[0]["facts"])

    def test_replied_contact_needs_no_action(self):
        clients = [_client("ACME", FRESH)]
        replied = {"ACME": [{"sent_at": (NOW - timedelta(days=20)).isoformat(), "replied": True}]}
        assert _score(clients, contacts_log_by_client=replied) == []


# ---------------------------------------------------------------------------
# Signal-relevant focus-product picker (overlooked reach-out angle)
# ---------------------------------------------------------------------------

def _fp(name, score, *enrich):
    """A focus-product match candidate, shaped like _focus_product_matches output.
    Pre-tokenises name + enrichment text into `_tokens`, exactly as production does."""
    blob = " ".join([name, *enrich])
    return {"product": name, "score": score, "category": (enrich[0] if enrich else ""),
            "_tokens": today._tokenize(blob)}


class TestPickSignalProduct:
    """_pick_signal_product picks the focus product most relevant to a signal,
    falling back to the top-fit product when nothing meaningfully overlaps."""

    def test_prefers_topical_overlap_over_top_fit(self):
        # Best-fit-first order: the AI-coding product outranks observability on fit,
        # but the signal is about monitoring — the picker must choose observability.
        products = [
            _fp("Bob Coding Agent", 9, "developer productivity", "ai pair programming"),
            _fp("Instana Observability", 6, "monitoring", "observability metrics tracing"),
        ]
        picked = today._pick_signal_product(
            "Client rolls out new observability and monitoring stack", products)
        assert picked["product"] == "Instana Observability"

    def test_falls_back_to_top_fit_when_no_overlap(self):
        products = [
            _fp("Bob Coding Agent", 9, "developer productivity", "ai pair programming"),
            _fp("Instana Observability", 6, "monitoring", "observability metrics"),
        ]
        # Litigation signal overlaps neither product → top-fit (Bob, score 9) wins.
        picked = today._pick_signal_product(
            "Supreme Court denies appeal in long-running litigation", products)
        assert picked["product"] == "Bob Coding Agent"

    def test_empty_signal_title_falls_back_to_top_fit(self):
        products = [_fp("Alpha", 7, "security"), _fp("Beta", 5, "data")]
        assert today._pick_signal_product("", products)["product"] == "Alpha"

    def test_no_products_returns_none(self):
        assert today._pick_signal_product("anything topical", []) is None

    def test_overlap_beats_higher_fit_score(self):
        # Overlap is the primary key; a lower fit score still wins on a real hit.
        products = [
            _fp("Guardium Security", 10, "compliance governance"),
            _fp("QRadar SIEM", 4, "security", "threat detection siem monitoring"),
        ]
        picked = today._pick_signal_product(
            "Breach prompts urgent threat detection and siem review", products)
        assert picked["product"] == "QRadar SIEM"

    def test_tie_on_overlap_breaks_on_fit_score(self):
        # Both share exactly one topical token ("cloud") → higher fit score wins.
        products = [
            _fp("CloudPak Low", 5, "cloud"),
            _fp("CloudPak High", 8, "cloud"),
        ]
        picked = today._pick_signal_product("Migrating workloads to the cloud", products)
        assert picked["product"] == "CloudPak High"

    def test_stopwords_do_not_create_false_overlap(self):
        # The ONLY word shared between signal and the lower-fit product is the
        # generic "platform" (a stopword). If stopwords counted, Beta would win on
        # overlap; because they don't, this falls back to the top-fit product Alpha.
        products = [
            _fp("Alpha Security", 9, "compliance"),
            _fp("Beta Platform", 6, "the platform suite"),
        ]
        picked = today._pick_signal_product("New enterprise platform rollout", products)
        assert picked["product"] == "Alpha Security"  # top-fit fallback, not overlap-driven

    def test_focus_stopword_ibm_prefix_ignored(self):
        # A product literally named "IBM X" must not overlap a signal that says
        # "IBM" — the vendor prefix is a stopword, so this is a top-fit fallback.
        products = [_fp("IBM Alpha", 8, "networking"), _fp("IBM Beta", 4, "storage")]
        picked = today._pick_signal_product("IBM named preferred vendor", products)
        assert picked["product"] == "IBM Alpha"


# ---------------------------------------------------------------------------
# compute_nba_queue orchestration
# ---------------------------------------------------------------------------

def _mock_db(clients, sig_rows=None, mail_rows=None):
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[sig_rows or [], mail_rows or []])
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    db = MagicMock()
    db.list_clients = AsyncMock(return_value=clients)
    db.list_tasks = AsyncMock(return_value=[])
    db.list_contact_log = AsyncMock(return_value=[])
    db._pool = MagicMock()
    db._pool.acquire = MagicMock(return_value=ctx)
    db.index_document = AsyncMock(return_value=99)
    return db


class TestComputeQueue:
    @pytest.mark.asyncio
    async def test_one_llm_call_for_whole_queue(self):
        clients = [_client("ACME", None), _client("Beta", None)]
        db = _mock_db(clients)
        reasons = json.dumps([{"client": "ACME", "reason": "R1"},
                              {"client": "Beta", "reason": "R2"}])
        with (patch.object(today, "db_module", db),
              patch("routers.knowledge._call_brain_sync", return_value=reasons) as llm):
            snapshot = await today.compute_nba_queue(1)
        assert llm.call_count == 1
        assert snapshot["llm_used"] is True
        assert all(e["reason_source"] == "llm" for e in snapshot["queue"])
        assert snapshot["queue"][0]["reason"] in ("R1", "R2")

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_templates(self):
        db = _mock_db([_client("ACME", None)])
        with (patch.object(today, "db_module", db),
              patch("routers.knowledge._call_brain_sync", side_effect=RuntimeError("down"))):
            snapshot = await today.compute_nba_queue(1)
        assert snapshot["llm_used"] is False
        assert snapshot["queue"][0]["reason_source"] == "template"
        assert snapshot["queue"][0]["reason"]

    @pytest.mark.asyncio
    async def test_snapshot_persisted_as_nba_queue_doc(self):
        db = _mock_db([_client("ACME", None)])
        with (patch.object(today, "db_module", db),
              patch("routers.knowledge._call_brain_sync", return_value="[]")):
            await today.compute_nba_queue(1)
        kwargs = db.index_document.await_args.kwargs
        assert kwargs["doc_type"] == "nba_queue"
        assert kwargs["doc_id"].startswith("nba-queue-")
        assert kwargs["source"] == "agent"
        assert kwargs["metadata"]["queue"][0]["client"] == "ACME"

    @pytest.mark.asyncio
    async def test_missing_client_in_llm_reply_gets_template(self):
        db = _mock_db([_client("ACME", None), _client("Beta", None)])
        reasons = json.dumps([{"client": "ACME", "reason": "Only one"}])
        with (patch.object(today, "db_module", db),
              patch("routers.knowledge._call_brain_sync", return_value=reasons)):
            snapshot = await today.compute_nba_queue(1)
        by_name = {e["client"]: e for e in snapshot["queue"]}
        assert by_name["ACME"]["reason_source"] == "llm"
        assert by_name["Beta"]["reason_source"] == "template"


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app_client():
    with (
        patch("server.get_live_model", return_value=MagicMock()),
        patch("server.db_module.init_db", new_callable=AsyncMock),
        patch("server.db_module.close_db", new_callable=AsyncMock),
        patch("server.DB_AVAILABLE", True),
    ):
        from server import app
        from routers.auth import current_user

        async def _fake_user():
            return FAKE_USER

        app.dependency_overrides[current_user] = _fake_user
        with TestClient(app, raise_server_exceptions=True) as client:
            yield client
        app.dependency_overrides.pop(current_user, None)


def _snapshot(age_hours=0):
    return {
        "queue": [{"rank": 1, "client": "ACME", "score": 12, "is_focus": False,
                   "suggested_action": "research", "action_link": "/client/ACME",
                   "reason": "r", "reason_source": "llm", "facts": []},
                  {"rank": 2, "client": "Beta", "score": 4, "is_focus": False,
                   "suggested_action": "research", "action_link": "/client/Beta",
                   "reason": "r", "reason_source": "llm", "facts": []}],
        "computed_at": (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat(),
        "llm_used": True, "clients_considered": 2,
    }


class TestApi:
    def test_get_serves_fresh_snapshot_without_recompute(self, app_client):
        with (patch("routers.today._load_latest_snapshot", new_callable=AsyncMock,
                    return_value=_snapshot(age_hours=1)),
              patch("routers.today.compute_nba_queue", new_callable=AsyncMock) as compute):
            r = app_client.get("/api/next-actions?limit=1")
        assert r.status_code == 200
        data = r.json()
        assert data["recomputed"] is False
        assert len(data["queue"]) == 1
        compute.assert_not_awaited()

    def test_get_recomputes_when_stale(self, app_client):
        with (patch("routers.today._load_latest_snapshot", new_callable=AsyncMock,
                    return_value=_snapshot(age_hours=25)),
              patch("routers.today.compute_nba_queue", new_callable=AsyncMock,
                    return_value=_snapshot()) as compute):
            r = app_client.get("/api/next-actions")
        assert r.status_code == 200
        assert r.json()["recomputed"] is True
        compute.assert_awaited_once_with(1, owner_id=1)

    def test_get_recomputes_when_no_snapshot(self, app_client):
        with (patch("routers.today._load_latest_snapshot", new_callable=AsyncMock,
                    return_value=None),
              patch("routers.today.compute_nba_queue", new_callable=AsyncMock,
                    return_value=_snapshot()) as compute):
            r = app_client.get("/api/next-actions")
        assert r.status_code == 200
        compute.assert_awaited_once()

    def test_refresh_forces_recompute(self, app_client):
        with patch("routers.today.compute_nba_queue", new_callable=AsyncMock,
                   return_value=_snapshot()) as compute:
            r = app_client.post("/api/next-actions/refresh")
        assert r.status_code == 200
        assert r.json()["recomputed"] is True
        compute.assert_awaited_once_with(1, owner_id=1)

    def test_click_logs_nba_click(self, app_client):
        with patch("server.db_module.log_prompt") as log:
            r = app_client.post("/api/next-actions/click",
                                json={"client": "ACME", "action": "research",
                                      "rank": 1, "page": "today"})
        assert r.status_code == 200
        assert log.call_args.args[2] == "nba_click"
        assert log.call_args.args[3] == "ACME"
        assert log.call_args.args[4]["action"] == "research"

    def test_click_requires_client(self, app_client):
        r = app_client.post("/api/next-actions/click", json={"action": "mail"})
        assert r.status_code == 400

    def test_unauthed_get_is_401(self):
        with (
            patch("server.get_live_model", return_value=MagicMock()),
            patch("server.db_module.init_db", new_callable=AsyncMock),
            patch("server.db_module.close_db", new_callable=AsyncMock),
            patch("server.DB_AVAILABLE", True),
            patch("server.db_module.get_user_by_token", new_callable=AsyncMock, return_value=None),
        ):
            from server import app
            saved = dict(app.dependency_overrides)
            app.dependency_overrides.clear()
            try:
                with TestClient(app, raise_server_exceptions=False) as client:
                    assert client.get("/api/next-actions").status_code == 401
            finally:
                app.dependency_overrides.update(saved)


# ---------------------------------------------------------------------------
# Heartbeat dispatch
# ---------------------------------------------------------------------------

class TestHeartbeatDispatch:
    @pytest.mark.asyncio
    async def test_nba_queue_branch_dispatches_to_compute(self):
        db = MagicMock()
        db.create_agent_run = AsyncMock(return_value=42)
        db.update_agent_run = AsyncMock()
        db.update_heartbeat_last_run = AsyncMock()
        # Two primary owners (1, 2) + a co-owner (3) → 3 distinct reps pre-warmed.
        db.list_clients = AsyncMock(return_value=[
            {"name": "ACME", "created_by": 1, "metadata": {}},
            {"name": "Beta", "created_by": 2, "metadata": {"owner_ids": [3]}},
        ])
        snapshot = {"queue": [{"client": "ACME"}], "llm_used": True}
        with (patch.object(pipeline, "db_module", db),
              patch.object(pipeline, "DB_AVAILABLE", True),
              patch("routers.today.compute_nba_queue", new_callable=AsyncMock,
                    return_value=snapshot) as compute,
              patch("agents.runner.run_agent", new_callable=AsyncMock) as legacy):
            await pipeline._run_heartbeat_job(1, 1, "nba_queue", "compute the queue")
        # One snapshot computed per distinct owner, each scoped by owner_id.
        assert compute.await_count == 3
        assert all(c.args == (1,) for c in compute.await_args_list)
        assert sorted(c.kwargs["owner_id"] for c in compute.await_args_list) == [1, 2, 3]
        legacy.assert_not_awaited()
        call = db.update_agent_run.await_args
        assert call.args[1] == "done"
        assert call.kwargs["output"] == {"reps_pre_warmed": 3, "clients_ranked": 3}
        db.update_heartbeat_last_run.assert_awaited_once_with(1)
