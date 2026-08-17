"""Operator API auth + external JWT verification rules (Phase 6b hooks)."""

import time

import jwt
import pytest
from fastapi import HTTPException

import context
from routers import auth as auth_router
from routers import operator as op


class _Req:
    def __init__(self, headers=None, method="POST", path="/api/x"):
        self.headers = headers or {}
        self.method = method

        class _U:  # minimal url
            pass
        self.url = _U(); self.url.path = path


def test_operator_key_fail_closed(monkeypatch):
    monkeypatch.setattr(context, "DB_AVAILABLE", True)
    monkeypatch.setattr(op, "DB_AVAILABLE", True)
    monkeypatch.setattr(op, "config", {"hosted": {}})
    monkeypatch.delenv("HOSTED_OPERATOR_KEY", raising=False)
    with pytest.raises(HTTPException) as e:
        op._check_key(_Req({"x-operator-key": "anything"}))
    assert e.value.status_code == 401
    monkeypatch.setattr(op, "config", {"hosted": {"operator_key": "k1"}})
    with pytest.raises(HTTPException):
        op._check_key(_Req({"x-operator-key": "wrong"}))
    op._check_key(_Req({"x-operator-key": "k1"}))      # no raise


def test_slug():
    assert op._slug("Cloud Customer Ltd") == "cloud-customer-ltd"
    assert op._slug("  ---  ") == "org"
    assert len(op._slug("x" * 200)) <= 48


def _cfg(monkeypatch, **ext):
    monkeypatch.setattr(context, "config", {"auth": {"external": {"enabled": True, "hs256_secret": "s" * 32,
                                                                   "issuer": "https://t/auth/v1", "audience": "authenticated", **ext}}})


def test_external_jwt_hs256_roundtrip(monkeypatch):
    _cfg(monkeypatch)
    tok = jwt.encode({"sub": "u1", "email": "a@b.c", "aud": "authenticated", "iss": "https://t/auth/v1",
                      "exp": int(time.time()) + 60}, "s" * 32, algorithm="HS256")
    claims = auth_router._verify_external_jwt(tok)
    assert claims["email"] == "a@b.c"


@pytest.mark.parametrize("bad", ["not-a-jwt", "", "a.b.c"])
def test_external_jwt_rejects_garbage(monkeypatch, bad):
    _cfg(monkeypatch)
    with pytest.raises(HTTPException) as e:
        auth_router._verify_external_jwt(bad)
    assert e.value.status_code == 401


def test_external_jwt_wrong_secret_audience_or_expired(monkeypatch):
    _cfg(monkeypatch)
    for payload, key in [
        ({"sub": "u", "email": "a@b.c", "aud": "authenticated", "iss": "https://t/auth/v1", "exp": int(time.time()) + 60}, "x" * 32),
        ({"sub": "u", "email": "a@b.c", "aud": "other", "iss": "https://t/auth/v1", "exp": int(time.time()) + 60}, "s" * 32),
        ({"sub": "u", "email": "a@b.c", "aud": "authenticated", "iss": "https://t/auth/v1", "exp": int(time.time()) - 5}, "s" * 32),
        ({"sub": "u", "email": "a@b.c", "aud": "authenticated", "iss": "https://evil", "exp": int(time.time()) + 60}, "s" * 32),
    ]:
        tok = jwt.encode(payload, key, algorithm="HS256")
        with pytest.raises(HTTPException):
            auth_router._verify_external_jwt(tok)


def test_external_disabled_is_404(monkeypatch):
    monkeypatch.setattr(context, "config", {"auth": {"external": {"enabled": False}}})
    with pytest.raises(HTTPException) as e:
        auth_router._verify_external_jwt("x.y.z")
    assert e.value.status_code == 404


def test_claim_path():
    assert auth_router._claim({"app_metadata": {"org_slug": "acme"}}, "app_metadata.org_slug") == "acme"
    assert auth_router._claim({"email": "e"}, "email") == "e"
    assert auth_router._claim({"a": 1}, "a.b") is None
