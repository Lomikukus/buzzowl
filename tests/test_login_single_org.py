"""Login without a workspace slug on single-workspace installs (self-host default)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from routers import auth as auth_router

ORG_A = {"id": 1, "name": "My Organization", "slug": "my-organization"}
ORG_B = {"id": 2, "name": "Other", "slug": "other"}
USER = {"id": 7, "username": "admin", "display_name": "Admin", "role": "admin", "password_hash": "hash"}


class _Req:
    """FastAPI Request stand-in — the rate limiter only reads client/scope."""
    client = type("C", (), {"host": "127.0.0.1"})()
    scope = {"type": "http"}
    headers: dict = {}


@pytest.fixture
def db(monkeypatch):
    fake = MagicMock()
    fake.list_orgs = AsyncMock(return_value=[ORG_A])
    fake.get_org_by_slug = AsyncMock(side_effect=lambda slug: {"my-organization": ORG_A, "other": ORG_B}.get(slug))
    fake.get_user_by_username = AsyncMock(return_value=dict(USER))
    fake.create_session_token = AsyncMock()
    fake.log_prompt = MagicMock()
    monkeypatch.setattr(auth_router, "db_module", fake)
    monkeypatch.setattr(auth_router, "DB_AVAILABLE", True)
    monkeypatch.setattr(auth_router.pwd_context, "verify", lambda p, h: p == "pw")
    return fake


async def test_login_without_slug_uses_the_only_workspace(db):
    out = await auth_router.login(_Req(), {"username": "admin", "password": "pw"})
    assert out["org"]["slug"] == "my-organization"
    assert out["token"]


async def test_login_with_slug_still_works(db):
    out = await auth_router.login(_Req(), {"org_slug": "my-organization", "username": "admin", "password": "pw"})
    assert out["org"]["id"] == 1
    db.list_orgs.assert_not_awaited()


async def test_slug_required_when_several_workspaces_exist(db):
    db.list_orgs.return_value = [ORG_A, ORG_B]
    with pytest.raises(HTTPException) as exc:
        await auth_router.login(_Req(), {"username": "admin", "password": "pw"})
    assert exc.value.status_code == 400
    assert "org_slug" in exc.value.detail


async def test_wrong_password_is_still_401(db):
    with pytest.raises(HTTPException) as exc:
        await auth_router.login(_Req(), {"username": "admin", "password": "nope"})
    assert exc.value.status_code == 401


async def test_missing_username_is_400(db):
    with pytest.raises(HTTPException) as exc:
        await auth_router.login(_Req(), {"password": "pw"})
    assert exc.value.status_code == 400


async def test_signup_status_reports_the_single_workspace(db):
    out = await auth_router.signup_status()
    assert out["single_org_slug"] == "my-organization"
    db.list_orgs.return_value = [ORG_A, ORG_B]
    assert (await auth_router.signup_status())["single_org_slug"] is None
