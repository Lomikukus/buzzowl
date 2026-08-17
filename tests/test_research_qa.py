"""
tests/test_research_qa.py — research-QA reviewer heuristics + orchestration.

No DB, no LLM, no network — the pure heuristics are exercised directly and the
DB-backed orchestration runs against an in-memory mock of the `db` module.

Covers agents.research_qa:
  - detect_stale_synthesis  (synthesis lags newest finding)
  - detect_contamination    (foreign client in a leadership context)
  - detect_no_sources       (no ## Sources / URL / (unconfirmed))
  - run_research_qa         (flags written, summary written) — all mocked
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents import research_qa


def _dt(days_ago: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


# ---------------------------------------------------------------------------
# detect_stale_synthesis
# ---------------------------------------------------------------------------

class TestStaleSynthesis:
    def test_flags_when_synthesis_older_than_finding_by_more_than_n(self):
        docs = [
            {"doc_id": "r1", "type": "research", "title": "Globex brief", "created_at": _dt(40)},
            {"doc_id": "f1", "type": "finding",  "title": "Lawsuit update", "created_at": _dt(5)},
        ]
        flag = research_qa.detect_stale_synthesis(docs, stale_days=10)
        assert flag is not None
        assert flag["doc_id"] == "r1"
        assert flag["lag_days"] == 35
        assert flag["type"] == "research"

    def test_no_flag_when_within_threshold(self):
        docs = [
            {"doc_id": "r1", "type": "brief",   "created_at": _dt(12)},
            {"doc_id": "f1", "type": "finding", "created_at": _dt(5)},
        ]
        # lag = 7 days, threshold 10 → not stale
        assert research_qa.detect_stale_synthesis(docs, stale_days=10) is None

    def test_no_flag_when_synthesis_newer_than_finding(self):
        docs = [
            {"doc_id": "r1", "type": "research", "created_at": _dt(2)},
            {"doc_id": "f1", "type": "finding",  "created_at": _dt(30)},
        ]
        assert research_qa.detect_stale_synthesis(docs, stale_days=10) is None

    def test_no_flag_without_findings(self):
        docs = [{"doc_id": "r1", "type": "research", "created_at": _dt(40)}]
        assert research_qa.detect_stale_synthesis(docs, stale_days=10) is None

    def test_no_flag_without_synthesis(self):
        docs = [{"doc_id": "f1", "type": "finding", "created_at": _dt(40)}]
        assert research_qa.detect_stale_synthesis(docs, stale_days=10) is None

    def test_uses_newest_synthesis_and_newest_finding(self):
        docs = [
            {"doc_id": "old", "type": "research", "created_at": _dt(90)},
            {"doc_id": "new", "type": "research", "created_at": _dt(50)},  # newest synth
            {"doc_id": "f_old", "type": "finding", "created_at": _dt(60)},
            {"doc_id": "f_new", "type": "finding", "created_at": _dt(10)},  # newest finding
        ]
        flag = research_qa.detect_stale_synthesis(docs, stale_days=10)
        assert flag["doc_id"] == "new"
        assert flag["lag_days"] == 40  # 50 - 10

    def test_accepts_iso_string_dates(self):
        docs = [
            {"doc_id": "r1", "type": "client_brief", "created_at": _dt(40).isoformat()},
            {"doc_id": "f1", "type": "finding", "created_at": _dt(2).isoformat()},
        ]
        flag = research_qa.detect_stale_synthesis(docs, stale_days=10)
        assert flag is not None and flag["doc_id"] == "r1"


# ---------------------------------------------------------------------------
# detect_contamination
# ---------------------------------------------------------------------------

class TestContamination:
    def test_flags_foreign_client_in_leadership_context(self):
        # A Globex research doc that lists Solaris's CIO as a contact.
        content = (
            "Globex AG is a life-sciences company. "
            "Key decision maker: Jane Example, CIO at Solaris, oversees IT strategy."
        )
        hits = research_qa.detect_contamination(
            content,
            own_names=["Globex AG"],
            other_client_names=["Solaris", "Siemens"],
        )
        assert any(h["client"] == "Solaris" for h in hits)
        assert not any(h["client"] == "Siemens" for h in hits)

    def test_no_flag_when_foreign_client_not_near_leadership_keyword(self):
        content = (
            "Globex AG competes with Solaris in the consumer-health market. "
            "Both firms reported revenue growth in the last quarter."
        )
        hits = research_qa.detect_contamination(
            content, own_names=["Globex"], other_client_names=["Solaris"],
        )
        assert hits == []

    def test_excludes_own_client(self):
        content = "Globex AG CEO Bill Anderson leads the company."
        hits = research_qa.detect_contamination(
            content, own_names=["Globex AG"], other_client_names=["Globex AG"],
        )
        assert hits == []

    def test_german_leadership_keyword(self):
        content = "Bericht über Globex. Geschäftsführer der Solaris-Sparte ist Herr Meyer."
        hits = research_qa.detect_contamination(
            content, own_names=["Globex"], other_client_names=["Solaris"],
        )
        assert any(h["client"] == "Solaris" for h in hits)

    def test_empty_content(self):
        assert research_qa.detect_contamination("", ["Globex"], ["Solaris"]) == []


# ---------------------------------------------------------------------------
# detect_no_sources
# ---------------------------------------------------------------------------

class TestNoSources:
    def test_flags_doc_with_no_sourcing(self):
        content = "Globex is facing about 100,000 lawsuits. Revenue grew last year."
        assert research_qa.detect_no_sources(content) is True

    def test_sources_heading_counts_as_sourced(self):
        content = "Globex overview.\n\n## Sources\n- https://bayer.com/report"
        assert research_qa.detect_no_sources(content) is False

    def test_german_quellen_heading_counts(self):
        content = "Globex Übersicht.\n\n### Quellen\n- Handelsblatt"
        assert research_qa.detect_no_sources(content) is False

    def test_bare_url_counts_as_sourced(self):
        content = "Globex revenue rose. See https://reuters.com/article/bayer for details."
        assert research_qa.detect_no_sources(content) is False

    def test_unconfirmed_marker_counts(self):
        content = "Globex may acquire a startup (unconfirmed)."
        assert research_qa.detect_no_sources(content) is False

    def test_inferred_marker_counts(self):
        content = "Globex likely plans expansion (inferred)."
        assert research_qa.detect_no_sources(content) is False

    def test_empty_content_is_unsourced(self):
        assert research_qa.detect_no_sources("") is True
        assert research_qa.detect_no_sources("   \n  ") is True


# ---------------------------------------------------------------------------
# run_research_qa — orchestration with mocked db module
# ---------------------------------------------------------------------------

def _make_mock_db(clients, docs_by_client, docs_by_type, full_docs):
    """Build a MagicMock standing in for the `db` module.

    clients:         list of {id, name}
    docs_by_client:  {client_id: [doc summaries]}  (list_documents client_id=...)
    docs_by_type:    {type: [doc summaries]}        (list_documents doc_type=...)
    full_docs:       {doc_id: {content, title, type}} (get_document)
    """
    db = MagicMock()
    db.list_clients = AsyncMock(return_value=clients)

    async def _list_documents(org_id, doc_type=None, client_id=None, contact_id=None):
        if client_id is not None:
            return docs_by_client.get(client_id, [])
        if doc_type is not None:
            return docs_by_type.get(doc_type, [])
        return []

    db.list_documents = AsyncMock(side_effect=_list_documents)

    async def _get_document(org_id, doc_id):
        d = full_docs.get(doc_id)
        return dict(d, doc_id=doc_id) if d else None

    db.get_document = AsyncMock(side_effect=_get_document)
    db.update_document = AsyncMock(return_value={})
    db.index_document = AsyncMock(return_value=1)
    return db


@pytest.mark.asyncio
async def test_run_research_qa_flags_all_three(monkeypatch):
    clients = [
        {"id": 1, "name": "Globex AG"},
        {"id": 2, "name": "Solaris"},
    ]
    # Globex: stale synthesis (research 40d, finding 5d) + a contaminated,
    # unsourced research doc. Solaris: clean.
    docs_by_client = {
        1: [
            {"doc_id": "bayer-research", "type": "research", "title": "Globex brief",
             "source": "agent", "created_at": _dt(40)},
            {"doc_id": "bayer-finding", "type": "finding", "title": "Lawsuit update",
             "source": "agent", "created_at": _dt(5)},
        ],
        2: [
            {"doc_id": "solaris-research", "type": "research", "title": "Solaris brief",
             "source": "agent", "created_at": _dt(3)},
        ],
    }
    docs_by_type = {
        "research": [
            {"doc_id": "bayer-research", "type": "research", "source": "agent", "created_at": _dt(40)},
            {"doc_id": "solaris-research", "type": "research", "source": "agent", "created_at": _dt(3)},
        ],
        "brief": [], "client_brief": [], "osint": [],
    }
    full_docs = {
        # Contaminated (Solaris CIO in a Globex doc) AND unsourced.
        "bayer-research": {
            "type": "research", "title": "Globex brief",
            "content": "Globex AG faces ~100,000 lawsuits. Contact: Jane Example, CIO at Solaris.",
        },
        # Clean: own client only + a Sources section.
        "solaris-research": {
            "type": "research", "title": "Solaris brief",
            "content": "Solaris overview.\n\n## Sources\n- https://solaris.com",
        },
    }
    db_mock = _make_mock_db(clients, docs_by_client, docs_by_type, full_docs)
    monkeypatch.setattr(research_qa, "_db", db_mock)
    # keep config default (stale_days=10) regardless of on-disk config
    monkeypatch.setattr(research_qa, "_load_config", lambda: {"research_qa_stale_days": 10})

    result = await research_qa.run_research_qa(org_id=1, run_id=7)

    assert result["stale_count"] == 1
    assert result["contamination_count"] == 1
    assert result["no_sources_count"] == 1
    assert result["stale"][0]["doc_id"] == "bayer-research"
    assert result["contamination"][0]["doc_id"] == "bayer-research"
    assert result["no_sources"][0]["doc_id"] == "bayer-research"

    # Flags written into the flagged doc's metadata (merge patch).
    flagged = {
        call.args[1]: call.args[2]["metadata"]["qa_flag"]
        for call in db_mock.update_document.await_args_list
    }
    assert flagged["bayer-research"] in {"stale", "contamination", "no_sources"}
    # bayer-research got 3 flag writes (stale, contamination, no_sources)
    bayer_flags = [
        c.args[2]["metadata"]["qa_flag"]
        for c in db_mock.update_document.await_args_list
        if c.args[1] == "bayer-research"
    ]
    assert set(bayer_flags) == {"stale", "contamination", "no_sources"}

    # A QA summary document was written with source='agent' + run id.
    db_mock.index_document.assert_awaited()
    idx_kwargs = db_mock.index_document.await_args.kwargs
    assert idx_kwargs["doc_type"] == "research_qa"
    assert idx_kwargs["source"] == "agent"
    assert idx_kwargs["agent_run_id"] == 7
    assert idx_kwargs["metadata"]["brief_type"] == "research_qa"


@pytest.mark.asyncio
async def test_run_research_qa_clean_org_no_flags(monkeypatch):
    clients = [{"id": 1, "name": "Solaris"}]
    docs_by_client = {
        1: [
            {"doc_id": "solaris-research", "type": "research", "source": "agent", "created_at": _dt(2)},
            {"doc_id": "solaris-finding", "type": "finding", "source": "agent", "created_at": _dt(3)},
        ],
    }
    docs_by_type = {
        "research": [{"doc_id": "solaris-research", "type": "research", "source": "agent", "created_at": _dt(2)}],
        "brief": [], "client_brief": [], "osint": [],
    }
    full_docs = {
        "solaris-research": {
            "type": "research", "title": "Solaris brief",
            "content": "Solaris overview.\n\n## Sources\n- https://solaris.com",
        },
    }
    db_mock = _make_mock_db(clients, docs_by_client, docs_by_type, full_docs)
    monkeypatch.setattr(research_qa, "_db", db_mock)
    monkeypatch.setattr(research_qa, "_load_config", lambda: {"research_qa_stale_days": 10})

    result = await research_qa.run_research_qa(org_id=1, run_id=None)
    assert result["stale_count"] == 0
    assert result["contamination_count"] == 0
    assert result["no_sources_count"] == 0
    # No doc flags written; summary still written.
    db_mock.update_document.assert_not_awaited()
    db_mock.index_document.assert_awaited()


@pytest.mark.asyncio
async def test_run_research_qa_only_scans_agent_docs(monkeypatch):
    """Human-authored research must not be flagged for missing sources."""
    clients = [{"id": 1, "name": "Solaris"}]
    docs_by_client = {1: [
        {"doc_id": "human-doc", "type": "research", "source": "human", "created_at": _dt(2)},
    ]}
    docs_by_type = {
        "research": [
            {"doc_id": "human-doc", "type": "research", "source": "human", "created_at": _dt(2)},
        ],
        "brief": [], "client_brief": [], "osint": [],
    }
    full_docs = {
        "human-doc": {"type": "research", "title": "Human note", "content": "No sources here at all."},
    }
    db_mock = _make_mock_db(clients, docs_by_client, docs_by_type, full_docs)
    monkeypatch.setattr(research_qa, "_db", db_mock)
    monkeypatch.setattr(research_qa, "_load_config", lambda: {"research_qa_stale_days": 10})

    result = await research_qa.run_research_qa(org_id=1)
    assert result["scanned"] == 0            # human doc excluded from candidate set
    assert result["no_sources_count"] == 0
    db_mock.get_document.assert_not_awaited()  # never fetched content for a human doc
