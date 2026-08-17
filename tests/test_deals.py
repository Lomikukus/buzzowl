"""deals.py domain rules — stages, probabilities, legacy mapping, value parsing."""

import pytest

import context
import deals as dl


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(context, "config", {})


def test_default_stages_ordered():
    assert dl.stage_ids() == ["lead", "qualified", "proposal", "negotiation", "won", "lost"]
    assert dl.status_for_stage("won") == "won" and dl.status_for_stage("lost") == "lost"
    assert dl.status_for_stage("proposal") == "open"


def test_default_probability_and_weighted():
    assert dl.default_probability("proposal") == 50
    assert dl.weighted_value(1000, None, "proposal") == 500.0
    assert dl.weighted_value(1000, 80, "proposal") == 800.0     # explicit override wins
    assert dl.weighted_value(None, 80, "proposal") == 0.0


def test_validate_stage():
    assert dl.validate_stage(" Proposal ") == "proposal"
    with pytest.raises(dl.DealError):
        dl.validate_stage("closed-maybe")


@pytest.mark.parametrize("raw,expected", [
    ("Proposal", "proposal"), ("Angebot", "proposal"), ("Closed Won", "won"),
    ("verloren", "lost"), ("prospecting", "lead"), ("in negotiation with CFO", "negotiation"),
    ("", None), (None, None), ("something odd", None),
])
def test_normalize_legacy_stage(raw, expected):
    assert dl.normalize_legacy_stage(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("120000", 120000.0), ("120k", 120000.0), ("1.2M", 1200000.0),
    ("€ 120.000", 120000.0), ("$1,500", 1500.0), ("1.5", 1.5), ("12,5k", 12500.0),
    (2500, 2500.0), ("", None), ("tbd", None), (None, None),
])
def test_parse_value(raw, expected):
    assert dl.parse_value(raw) == expected


def test_custom_stages_from_config(monkeypatch):
    monkeypatch.setattr(context, "config", {"deal_stages": [
        {"id": "new", "label": "New", "probability": 5},
        {"id": "signed", "label": "Signed", "probability": 100, "status": "won"},
    ]})
    assert dl.stage_ids() == ["new", "signed"]
    assert dl.status_for_stage("signed") == "won"
    assert dl.default_probability("new") == 5
