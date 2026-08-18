"""Per-rep outreach identity: GET/PATCH /api/auth/identity (Phase 3 gap closed)."""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import context
from routers import auth as auth_router


class _FakeDB:
    def __init__(self):
        self.settings = {}

    async def get_user_identity(self, org_id, user_id):
        return {
            "display_name": self.settings.get("outreach_display_name") or "Anna",
            "reply_to": self.settings.get("outreach_reply_to") or "anna@login.example",
            "signature": self.settings.get("outreach_signature") or "",
            "email": "anna@login.example",
        }

    async def update_user_settings(self, org_id, user_id, patch):
        self.settings.update(patch)
        return self.settings


USER = {"id": 1, "org_id": 8, "username": "anna", "display_name": "Anna", "role": "member"}


@pytest.fixture
def db(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr(context, "db_module", fake)
    monkeypatch.setattr(auth_router, "db_module", fake)
    monkeypatch.setattr(auth_router, "DB_AVAILABLE", True)
    return fake


async def test_get_returns_account_fallbacks(db):
    out = await auth_router.get_identity(user=USER)
    assert out["identity"]["display_name"] == "Anna"
    assert out["identity"]["reply_to"] == "anna@login.example"


async def test_patch_stores_prefixed_keys(db):
    out = await auth_router.set_identity(
        {"display_name": "Anna Weber", "reply_to": "anna@corp.example", "signature": "Anna\nAE"},
        user=USER,
    )
    assert db.settings == {
        "outreach_display_name": "Anna Weber",
        "outreach_reply_to": "anna@corp.example",
        "outreach_signature": "Anna\nAE",
    }
    assert out["identity"]["reply_to"] == "anna@corp.example"


async def test_patch_only_touches_sent_fields(db):
    await auth_router.set_identity({"signature": "just this"}, user=USER)
    assert set(db.settings) == {"outreach_signature"}


async def test_empty_string_clears_the_override(db):
    await auth_router.set_identity({"reply_to": "anna@corp.example"}, user=USER)
    await auth_router.set_identity({"reply_to": ""}, user=USER)
    assert db.settings["outreach_reply_to"] == ""
    assert (await auth_router.get_identity(user=USER))["identity"]["reply_to"] == "anna@login.example"


@pytest.mark.parametrize("bad", ["not-an-email", "a@b", "a b@c.de", "@corp.example"])
async def test_invalid_reply_to_rejected(db, bad):
    with pytest.raises(HTTPException) as exc:
        await auth_router.set_identity({"reply_to": bad}, user=USER)
    assert exc.value.status_code == 400
    assert db.settings == {}


async def test_long_values_are_truncated(db):
    await auth_router.set_identity({"display_name": "x" * 300, "signature": "y" * 5000}, user=USER)
    assert len(db.settings["outreach_display_name"]) == 120
    assert len(db.settings["outreach_signature"]) == 2000


async def test_db_unavailable_is_503(monkeypatch):
    monkeypatch.setattr(auth_router, "DB_AVAILABLE", False)
    with pytest.raises(HTTPException) as exc:
        await auth_router.set_identity({"signature": "x"}, user=USER)
    assert exc.value.status_code == 503
