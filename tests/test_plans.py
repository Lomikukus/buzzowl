"""plans.py + llm per-org overlay (Phase 6a hosted plans)."""

import pytest

import context
import llm
import plans


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    # No BUZZOWL_SECRET_KEY → key material comes from agent_service_token, the
    # default install shape these tests care about.
    monkeypatch.delenv("BUZZOWL_SECRET_KEY", raising=False)
    monkeypatch.setattr(context, "config", {
        "agent_service_token": "unit-test-secret",
        "llm": {"providers": {"platform": {"kind": "openai-compat", "base_url": "http://p", "api_key": "pk"}},
                "roles": {"default": {"provider": "platform", "model": "platform-model"}}},
        "hosted": {},
    })
    llm.invalidate_org_overlay()
    yield
    llm.invalidate_org_overlay()


def test_encrypt_roundtrip_and_mask():
    tok = plans.encrypt_secret("sk-live-1234567890")
    assert tok.startswith("enc:v1:") and plans.is_encrypted(tok)
    assert plans.decrypt_secret(tok) == "sk-live-1234567890"
    assert plans.encrypt_secret(tok) == tok            # idempotent
    assert "1234567890" not in plans.mask_key(tok)
    assert plans.decrypt_secret("plain") == "plain"


def test_sanitize_and_merge_keeps_stored_key():
    stored = plans.sanitize_org_llm({"providers": {"or": {"kind": "openai-compat", "base_url": "https://x/", "api_key": "sk-1"}},
                                     "roles": {"default": {"provider": "or", "model": "m1"}}})
    assert stored["providers"]["or"]["api_key"].startswith("enc:v1:")
    assert stored["providers"]["or"]["base_url"] == "https://x"
    incoming = plans.sanitize_org_llm({"providers": {"or": {"kind": "openai-compat", "base_url": "https://x", "api_key": ""}},
                                       "roles": {"default": {"provider": "or", "model": "m2"}}})
    merged = plans.merge_org_llm(stored, incoming)
    assert merged["providers"]["or"]["api_key"] == stored["providers"]["or"]["api_key"]
    assert merged["roles"]["default"]["model"] == "m2"
    pub = plans.public_org_llm(merged)
    assert pub["providers"]["or"]["has_key"] and "sk-1" not in pub["providers"]["or"]["api_key"]
    assert plans.overlay_for_llm(merged)["providers"]["or"]["api_key"] == "sk-1"
    # 'pi' kind is platform-only
    assert plans.sanitize_org_llm({"providers": {"x": {"kind": "pi"}}})["providers"] == {}


# ---------------------------------------------------------------------------
# Key rotation: a ciphertext that no longer opens must never escape as an
# exception — the org behaves as if it had stored no key at all.
# ---------------------------------------------------------------------------

def _rotate_key(monkeypatch, new_token="rotated-agent-service-token"):
    """Simulate AGENT_SERVICE_TOKEN rotation without a BUZZOWL_SECRET_KEY."""
    monkeypatch.delenv("BUZZOWL_SECRET_KEY", raising=False)
    monkeypatch.setitem(context.config, "agent_service_token", new_token)


def test_decrypt_after_key_rotation_raises_typed_error(monkeypatch):
    tok = plans.encrypt_secret("sk-live-1234567890")
    _rotate_key(monkeypatch)
    with pytest.raises(plans.SecretDecryptError):
        plans.decrypt_secret(tok)
    assert plans.try_decrypt_secret(tok) is None
    assert plans.key_readable(tok) is False
    # untouched values still round-trip / pass through
    assert plans.key_readable("plain") is True
    assert plans.try_decrypt_secret("plain") == "plain"
    assert plans.try_decrypt_secret("") == ""


def test_decrypt_of_corrupted_ciphertext_is_graceful(monkeypatch, caplog):
    tok = plans.encrypt_secret("sk-live-1234567890")
    corrupt = tok[:-4] + ("AAAA" if not tok.endswith("AAAA") else "BBBB")
    with pytest.raises(plans.SecretDecryptError):
        plans.decrypt_secret(corrupt)
    with caplog.at_level("WARNING"):
        assert plans.try_decrypt_secret(corrupt, "the stored LLM key for org 8") is None
    assert "cannot be decrypted" in caplog.text
    # truncated / non-base64 payloads take the same path, never a raw ValueError
    with pytest.raises(plans.SecretDecryptError):
        plans.decrypt_secret(plans._ENC_PREFIX + "!!!not-base64!!!")
    with pytest.raises(plans.SecretDecryptError):
        plans.decrypt_secret(plans._ENC_PREFIX + "AAAA")


def test_views_report_needs_reconnect_after_rotation(monkeypatch, caplog):
    stored = plans.sanitize_org_llm({"providers": {"or": {"kind": "openai-compat", "base_url": "https://x", "api_key": "sk-1"}},
                                     "roles": {"default": {"provider": "or", "model": "m1"}}})
    _rotate_key(monkeypatch)

    pub = plans.public_org_llm(stored)                     # no exception
    assert pub["providers"]["or"]["has_key"] is False
    assert pub["providers"]["or"]["needs_reconnect"] is True
    assert "sk-1" not in str(pub)

    with caplog.at_level("WARNING"):
        ov = plans.overlay_for_llm(stored, org_id=8)       # no exception
    assert ov["providers"] == {}                           # dropped: as if never stored
    assert ov["undecryptable_providers"] == ["or"]
    assert ov["roles"]["default"]["model"] == "m1"
    assert "Reconnect the key in Settings" in caplog.text
    assert "org 8" in caplog.text


def test_resolve_with_undecryptable_key_falls_back_or_says_reconnect(monkeypatch):
    stored = plans.sanitize_org_llm({"providers": {"or": {"kind": "openai-compat", "base_url": "https://x", "api_key": "sk-1"}},
                                     "roles": {"default": {"provider": "or", "model": "m1"}}})
    _rotate_key(monkeypatch)
    ov = plans.overlay_for_llm(stored, org_id=8)

    # plans enforced → a clear "enter it again", not a 500 and not an empty key
    _seed_overlay(8, {"plan": "light", **ov, "budget": None, "month_cost": 0.0, "enforce": True})
    with pytest.raises(llm.LLMError, match="decrypted"):
        llm.resolve("chat", None, 8)

    # self-hosted (not enforced) → platform fallback, same as having no own key
    _seed_overlay(8, {"plan": "light", **ov, "budget": None, "month_cost": 0.0, "enforce": False})
    assert llm.resolve("chat", None, 8)[0].name == "platform"


async def test_plan_endpoint_reports_reconnect_instead_of_500(monkeypatch):
    """GET /api/org/plan must answer, not blow up, once the key stopped opening."""
    from routers import org_settings as os_router

    stored = plans.sanitize_org_llm({"providers": {"or": {"kind": "openai-compat", "base_url": "https://x", "api_key": "sk-1"}},
                                     "roles": {"default": {"provider": "or", "model": "m1"}}})
    _rotate_key(monkeypatch)

    class _DB:
        async def get_org_settings(self, org_id):
            return {"plan": "light", "llm": stored}

        async def llm_usage_summary(self, org_id, days=31):
            return {"month": {"cost_usd": 0.0}}

    monkeypatch.setattr(os_router, "DB_AVAILABLE", True)
    monkeypatch.setattr(os_router, "db_module", _DB())
    monkeypatch.setattr(os_router, "_config", {"hosted": {}})

    out = await os_router.get_plan(user={"org_id": 8, "role": "admin"})
    assert out["keys_need_reconnect"] == ["or"]
    assert out["has_own_providers"] is False        # unusable key == no key
    assert out["plan"] == "light"


def test_plan_and_budget():
    assert plans.plan_of({}) == "light" and plans.plan_of({"plan": "premium"}) == "premium"
    assert plans.budget_usd({"plan": "light"}, {}) is None
    assert plans.budget_usd({"plan": "premium"}, {}) == plans.DEFAULT_PREMIUM_BUDGET_USD
    assert plans.budget_usd({"plan": "premium"}, {"hosted": {"premium_monthly_budget_usd": 5}}) == 5.0
    assert plans.budget_usd({"plan": "premium", "llm_budget_usd_per_month": 7.5}, {}) == 7.5


@pytest.mark.parametrize("model,pt,ct,expected", [
    ("openai/gpt-4o-mini", 1_000_000, 0, 0.15),
    ("gpt-4o-mini-2025-01-01", 0, 1_000_000, 0.60),
    ("deepseek/deepseek-v4-flash:free", 5000, 5000, 0.0),
    ("mystery-model", 1, 1, None),
])
def test_estimate_cost(model, pt, ct, expected):
    assert plans.estimate_cost(model, pt, ct) == expected


def test_estimate_cost_config_override():
    assert plans.estimate_cost("mystery-model", 1_000_000, 0, {"llm_prices": {"mystery-model": [2, 4]}}) == 2.0


def _seed_overlay(org_id, ov):
    import time
    llm._org_overlays[org_id] = (time.monotonic() + 60, ov)


def test_resolve_light_org_uses_own_provider():
    _seed_overlay(8, {"plan": "light", "providers": {"own": {"kind": "openai-compat", "base_url": "http://own", "api_key": "ok"}},
                      "roles": {"default": {"provider": "own", "model": "own-model"}}, "budget": None, "month_cost": 0.0, "enforce": True})
    p, m = llm.resolve("chat", None, 8)
    assert p.name == "own" and p.api_key == "ok" and m == "own-model"
    # another org without overlay → platform
    p2, m2 = llm.resolve("chat", None, 9)
    assert p2.name == "platform" and m2 == "platform-model"


def test_resolve_light_org_without_provider_refused_only_when_enforced():
    _seed_overlay(8, {"plan": "light", "providers": {}, "roles": {}, "budget": None, "month_cost": 0.0, "enforce": True})
    with pytest.raises(llm.LLMError):
        llm.resolve("chat", None, 8)
    _seed_overlay(8, {"plan": "light", "providers": {}, "roles": {}, "budget": None, "month_cost": 0.0, "enforce": False})
    assert llm.resolve("chat", None, 8)[0].name == "platform"


def test_resolve_premium_budget_soft_block():
    _seed_overlay(8, {"plan": "premium", "providers": {}, "roles": {}, "budget": 10.0, "month_cost": 10.5, "enforce": True})
    with pytest.raises(llm.LLMError, match="budget"):
        llm.resolve("chat", None, 8)
    _seed_overlay(8, {"plan": "premium", "providers": {}, "roles": {}, "budget": 10.0, "month_cost": 2.0, "enforce": True})
    assert llm.resolve("chat", None, 8)[0].name == "platform"
