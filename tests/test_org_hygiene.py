"""
tests/test_org_hygiene.py — Unit tests for the org-hygiene agent's client
deduplication logic.

Pure functions only — no DB, no LLM, no network. Covers:
  - _normalize_client_name        (legal-form / punctuation / id-suffix stripping)
  - _domain                       (website-domain extraction)
  - _is_subsidiary_pair           (the subsidiary guard)
  - _classify_client_pair         (high vs needs_review vs no-candidate)

Real-world examples from production drive the assertions:
  Acme / Acme AG        -> merge (high)
  BBraun / B.Braun                  -> merge (high)
  IGS-Apleona-724 / Apleona GmbH    -> merge (high, junk suffix/prefix + legal form)
  EUMETSAT / long form              -> merge (high, acronym/long-form)
  X Shared Service GmbH / X         -> flag (needs_review), NOT merge
"""

from unittest.mock import AsyncMock, patch

import pytest

import agents.org as org
from agents.org import (
    _normalize_client_name,
    _domain,
    _is_subsidiary_pair,
    _classify_client_pair,
    _auto_merge_high_confidence,
)


def _client(cid: int, name: str, website: str | None = None) -> dict:
    meta = {}
    if website:
        meta["website"] = website
    return {"id": cid, "name": name, "metadata": meta}


# ---------------------------------------------------------------------------
# _normalize_client_name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("Acme", "acme"),
    ("Acme AG", "acme"),
    ("Apleona GmbH", "apleona"),
    ("IGS-Apleona-724", "apleona"),          # junk prefix + numeric id-suffix
    ("ACME GmbH & Co KG", "acme"),           # multi-word legal fragment
    ("EUMETSAT", "eumetsat"),
    ("  Foo   Bar  ", "foo bar"),            # whitespace collapse
    ("", ""),
])
def test_normalize_client_name(raw, expected):
    assert _normalize_client_name(raw) == expected


def test_normalize_strips_legal_tokens_but_keeps_stem():
    # Legal tokens gone, brand stem preserved.
    assert _normalize_client_name("Siemens Energy SE") == "siemens energy"
    assert _normalize_client_name("Muster Holding GmbH") == "muster"


def test_normalize_acme_variants_collapse_equal():
    assert _normalize_client_name("Acme") == _normalize_client_name("Acme AG")


def test_normalize_apleona_junk_and_legal_collapse_equal():
    # The "IGS-...-724" junk form must normalize to the same stem as the clean name.
    assert _normalize_client_name("IGS-Apleona-724") == _normalize_client_name("Apleona GmbH")


# ---------------------------------------------------------------------------
# _domain
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url, expected", [
    ("https://www.acme.com/de", "acme.com"),
    ("http://apleona.com", "apleona.com"),
    ("www.foo.co.uk", "foo.co.uk"),
    ("https://sub.foo.com:8443/path?x=1", "sub.foo.com"),
    ("", ""),
    (None, ""),
])
def test_domain_extraction(url, expected):
    assert _domain(url) == expected


# ---------------------------------------------------------------------------
# _is_subsidiary_pair  (the guard)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("a, b, is_sub", [
    # Genuine subsidiaries: shared stem + descriptive token -> flagged.
    ("bosch shared service", "bosch", True),
    ("siemens digital solutions", "siemens", True),
    ("vodafone deutschland", "vodafone", True),
    ("acme automotive", "acme", True),
    ("acme engineering", "acme", True),
    # Not subsidiaries: identical, or no shared stem, or non-descriptive diff.
    ("acme", "acme", False),           # equal
    ("apleona", "apleona", False),                 # equal
    ("acme", "zenith", False),                     # no shared stem
    ("eumetsat europäische meteorologische satelliten", "eumetsat", False),  # diff tokens not descriptive
])
def test_is_subsidiary_pair(a, b, is_sub):
    assert _is_subsidiary_pair(a, b) is is_sub


# ---------------------------------------------------------------------------
# _classify_client_pair  (high / needs_review / None)
# ---------------------------------------------------------------------------

def _confidence(a_name, b_name, a_web=None, b_web=None):
    v = _classify_client_pair(_client(1, a_name, a_web), _client(2, b_name, b_web))
    return v["confidence"] if v else None


def test_classify_acme_high():
    assert _confidence("Acme", "Acme AG") == "high"


def test_classify_bbraun_high():
    # Punctuation-only split — resolved via spaceless equality.
    assert _confidence("BBraun", "B.Braun") == "high"


def test_classify_apleona_junk_suffix_high():
    assert _confidence("IGS-Apleona-724", "Apleona GmbH") == "high"


def test_classify_eumetsat_longform_needs_review():
    # Long-form / acronym expansion is the same entity but not an EXACT normalized
    # match, so under the conservative policy it's held for review (not auto-merged).
    assert _confidence(
        "Eumetsat Europäische Meteorologische Satelliten", "EUMETSAT"
    ) == "needs_review"


def test_classify_shared_website_domain_needs_review():
    # A shared website domain never auto-merges (holding vs op-co, former names,
    # umbrella domains) — only exact normalized-name equality is HIGH.
    assert _confidence(
        "Acme Widgets", "Acme Gadgets",
        a_web="https://www.acme.com/products", b_web="http://acme.com",
    ) == "needs_review"


def test_classify_shared_umbrella_domain_dissimilar_names_downgraded():
    # Shared domain but unrelated names (umbrella/parent domain, e.g. ihk.de)
    # must NOT auto-merge — downgrade to needs_review.
    assert _confidence(
        "IHK Aschaffenburg", "Handelskammer Rheinhessen",
        a_web="https://www.ihk.de/a", b_web="https://www.ihk.de/b",
    ) == "needs_review"


def test_classify_shared_domain_division_token_downgraded():
    # Same corporate domain + same lead brand, but the names differ by a
    # descriptive division token ("Construction") — likely distinct divisions,
    # so flag rather than auto-merge.
    assert _confidence(
        "Bilfinger Construction GmbH", "Bilfinger Berger Bau AG",
        a_web="https://bilfinger.com/a", b_web="https://bilfinger.com/b",
    ) == "needs_review"


def test_classify_shared_service_flagged_not_merged():
    # CRITICAL: genuine subsidiary must be flagged, never auto-merged.
    assert _confidence("Acme Shared Service GmbH", "Acme") == "needs_review"


@pytest.mark.parametrize("a, b", [
    ("Siemens Digital Solutions", "Siemens"),
    ("Vodafone Deutschland", "Vodafone"),
    ("Bosch Automotive", "Bosch"),
])
def test_classify_subsidiaries_needs_review(a, b):
    assert _confidence(a, b) == "needs_review"


def test_classify_unrelated_returns_none():
    assert _confidence("Acme Corp", "Zenith Ltd") is None


def test_classify_high_similarity_but_not_equal_needs_review():
    # Fuzzy-but-not-exact (and not a subsidiary) surfaces for review.
    # "Microsft" vs "Microsoft": similar spelling, not a normalized match.
    assert _confidence("Microsft", "Microsoft") == "needs_review"


def test_classify_returns_reason_and_similarity():
    v = _classify_client_pair(_client(1, "Acme"), _client(2, "Acme AG"))
    assert v is not None
    assert v["confidence"] == "high"
    assert "reason" in v and v["reason"]
    assert isinstance(v["similarity"], float)


def test_classify_empty_names_no_candidate():
    # Two empty/blank names must not be treated as a duplicate.
    assert _confidence("", "") is None


# ---------------------------------------------------------------------------
# _auto_merge_high_confidence  (orchestration — db.merge_clients mocked)
# ---------------------------------------------------------------------------

def _pair(a_id, a_name, b_id, b_name):
    return {
        "a": {"id": a_id, "name": a_name},
        "b": {"id": b_id, "name": b_name},
        "confidence": "high",
    }


@pytest.mark.asyncio
async def test_auto_merge_picks_canonical_by_doc_count():
    """The client with MORE linked documents is canonical; the other is the dupe."""
    calls = []

    async def fake_merge(org_id, dupe_id, canonical_id):
        calls.append((dupe_id, canonical_id))
        return {"dupe_name": "d", "canonical_name": "c", "links_moved": 1,
                "links_dropped": 0, "contacts_moved": 0}

    with patch.object(org._db, "count_client_document_links",
                      AsyncMock(return_value={10: 5, 20: 99})), \
         patch.object(org._db, "list_clients", AsyncMock(return_value=[
             {"id": 10, "name": "Foo", "metadata": {}, "created_at": None},
             {"id": 20, "name": "Foo AG", "metadata": {}, "created_at": None},
         ])), \
         patch.object(org._db, "merge_clients", AsyncMock(side_effect=fake_merge)):
        merges = await _auto_merge_high_confidence(1, [_pair(10, "Foo", 20, "Foo AG")])

    assert len(merges) == 1
    # 20 has more docs -> canonical; 10 -> dupe.
    assert calls == [(10, 20)]


@pytest.mark.asyncio
async def test_auto_merge_transitive_chain_collapses_to_one_canonical():
    """A↔B and B↔C should collapse to a single canonical (no merge into a
    row that was itself merged away)."""
    calls = []

    async def fake_merge(org_id, dupe_id, canonical_id):
        calls.append((dupe_id, canonical_id))
        return {"dupe_name": "d", "canonical_name": "c", "links_moved": 0,
                "links_dropped": 0, "contacts_moved": 0}

    # doc counts: C(30)=100 > A(10)=10 > B(20)=1  -> canonical should be 30.
    with patch.object(org._db, "count_client_document_links",
                      AsyncMock(return_value={10: 10, 20: 1, 30: 100})), \
         patch.object(org._db, "list_clients", AsyncMock(return_value=[
             {"id": 10, "name": "A", "metadata": {}, "created_at": None},
             {"id": 20, "name": "B", "metadata": {}, "created_at": None},
             {"id": 30, "name": "C", "metadata": {}, "created_at": None},
         ])), \
         patch.object(org._db, "merge_clients", AsyncMock(side_effect=fake_merge)):
        merges = await _auto_merge_high_confidence(1, [
            _pair(10, "A", 20, "B"),
            _pair(20, "B", 30, "C"),
        ])

    assert len(merges) == 2
    # 30 (highest doc-count) must be the final canonical and never a dupe.
    assert 30 not in {dupe for dupe, _ in calls}
    assert calls[-1][1] == 30
    # Both 10 and 20 are eliminated (each appears as a dupe at least once).
    assert {10, 20}.issubset({dupe for dupe, _ in calls})


@pytest.mark.asyncio
async def test_auto_merge_ignores_non_high_pairs():
    review_pair = _pair(1, "X", 2, "Y")
    review_pair["confidence"] = "needs_review"
    with patch.object(org._db, "merge_clients", AsyncMock()) as m:
        merges = await _auto_merge_high_confidence(1, [review_pair])
    assert merges == []
    m.assert_not_called()
