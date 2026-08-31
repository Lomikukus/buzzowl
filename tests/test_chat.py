"""
tests/test_chat.py — Tests for routers/chat.py

Covers:
  - POST /api/chat  (basic response, client scope, unauthenticated)
  - Session CRUD   (create, list, load, rename, delete, 404)
  - WP11: chat resolves provider+model from the workspace's LLM roles (org
    overlay chat/default role) on both the Python and Pi paths, instead of
    pairing the wizard-configured org provider with config.yaml's legacy
    pi_chat_model/agent_service_model — an impossible combination.
"""

import time as _time

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient

import context
import llm


# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

FAKE_USER = {
    "id": 1,
    "org_id": 1,
    "username": "konrad",
    "display_name": "Konrad",
    "email": "k@test.com",
    "role": "admin",
    "org_name": "North",
    "org_slug": "north",
}

FAKE_SESSION = {
    "id": 1,
    "org_id": 1,
    "user_id": 1,
    "title": "ACME prep",
    "client_name": None,
    "messages": [],
    "created_at": "2026-05-01T10:00:00",
    "updated_at": "2026-05-01T10:00:00",
}

FAKE_DOCS = [
    {
        "id": "doc-1",
        "display_title": "ACME GmbH Profile",
        "snippet": "ACME is a leading SaaS company.",
        "result_type": "document",
        "subtype": "research",
        "metadata": {"source_url": "https://example.com/acme"},
    },
    {
        "id": "doc-2",
        "display_title": "Bosch Annual Report",
        "snippet": "Bosch reported strong growth.",
        "result_type": "document",
        "subtype": "finding",
        "metadata": {},
    },
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app_client():
    """Module-scoped TestClient with DB mocked and current_user overridden."""
    with (
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


@pytest.fixture()
def unauthed_client():
    """Per-test client with no dependency overrides — real auth guard active."""
    with (
        patch("server.db_module.init_db", new_callable=AsyncMock),
        patch("server.db_module.close_db", new_callable=AsyncMock),
        patch("server.DB_AVAILABLE", True),
        patch("server.db_module.get_user_by_token", new_callable=AsyncMock, return_value=None),
    ):
        from server import app  # noqa: F401 (already imported above via module cache)
        saved = dict(app.dependency_overrides)
        app.dependency_overrides.clear()
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                yield client
        finally:
            app.dependency_overrides.update(saved)


# ---------------------------------------------------------------------------
# TestChatEndpoint
# ---------------------------------------------------------------------------

class TestChatEndpoint:
    def test_chat_basic_response(self, app_client):
        """POST /api/chat returns answer and sources keys."""
        with (
            patch("server.db_module.list_clients", new_callable=AsyncMock, return_value=[]),
            patch("server.db_module.list_contacts", new_callable=AsyncMock, return_value=[]),
            # Tool loop calls llm.chat (via llm.achat) — patch it to return a direct answer
            patch(
                "routers.chat.llm.chat",
                return_value={"content": "You have 2 clients.", "tool_calls": []},
            ),
        ):
            resp = app_client.post(
                "/api/chat",
                json={"message": "what clients do we have?"},
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "answer" in data
        assert "sources" in data

    def test_chat_with_client_scope(self, app_client):
        """POST /api/chat with client_name scope works end-to-end."""
        fake_client = {
            "id": 10,
            "name": "Bosch",
            "metadata": {"industry": "Manufacturing"},
            "session_count": 3,
            "last_activity": "2026-04-01",
            "documents": [],
        }
        with (
            patch("server.db_module.list_clients", new_callable=AsyncMock, return_value=[fake_client]),
            patch("server.db_module.list_contacts", new_callable=AsyncMock, return_value=[]),
            patch("server.db_module.get_client", new_callable=AsyncMock, return_value=fake_client),
            patch(
                "routers.chat.llm.chat",
                return_value={"content": "Bosch is in Manufacturing.", "tool_calls": []},
            ),
        ):
            resp = app_client.post(
                "/api/chat",
                json={"message": "tell me about Bosch", "client_name": "Bosch"},
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "answer" in data
        assert "sources" in data

    def test_chat_unauthenticated(self, unauthed_client):
        """POST /api/chat without a valid token returns 401."""
        resp = unauthed_client.post(
            "/api/chat",
            json={"message": "hello"},
            headers={"Authorization": "Bearer invalid"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# TestChatSessions
# ---------------------------------------------------------------------------

class TestChatSessions:
    def test_session_create(self, app_client):
        """POST /api/chat/sessions creates a session and returns it."""
        with patch(
            "server.db_module.create_chat_session",
            new_callable=AsyncMock,
            return_value=FAKE_SESSION,
        ):
            resp = app_client.post(
                "/api/chat/sessions",
                json={"title": "ACME prep"},
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == 1
        assert data["title"] == "ACME prep"

    def test_session_list(self, app_client):
        """GET /api/chat/sessions returns a list of sessions."""
        fake_sessions = [
            {**FAKE_SESSION, "id": 1, "title": "Session One"},
            {**FAKE_SESSION, "id": 2, "title": "Session Two"},
        ]
        with patch(
            "server.db_module.list_chat_sessions",
            new_callable=AsyncMock,
            return_value=fake_sessions,
        ):
            resp = app_client.get(
                "/api/chat/sessions",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "sessions" in data
        assert len(data["sessions"]) == 2

    def test_session_load(self, app_client):
        """GET /api/chat/sessions/1 returns a session with messages."""
        session_with_messages = {
            **FAKE_SESSION,
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "ai", "content": "Hi there"},
            ],
        }
        with patch(
            "server.db_module.get_chat_session",
            new_callable=AsyncMock,
            return_value=session_with_messages,
        ):
            resp = app_client.get(
                "/api/chat/sessions/1",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == 1
        assert len(data["messages"]) == 2

    def test_session_rename(self, app_client):
        """PATCH /api/chat/sessions/1 renames a session and returns ok."""
        with patch(
            "server.db_module.update_chat_session_title",
            new_callable=AsyncMock,
        ):
            resp = app_client.patch(
                "/api/chat/sessions/1",
                json={"title": "New title"},
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_session_rename_400_empty_title(self, app_client):
        """PATCH /api/chat/sessions/1 with an empty title returns 400."""
        resp = app_client.patch(
            "/api/chat/sessions/1",
            json={"title": ""},
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 400

    def test_session_delete(self, app_client):
        """DELETE /api/chat/sessions/1 deletes a session and returns ok."""
        with patch(
            "server.db_module.delete_chat_session",
            new_callable=AsyncMock,
        ):
            resp = app_client.delete(
                "/api/chat/sessions/1",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_session_404(self, app_client):
        """GET /api/chat/sessions/999 returns 404 when session not found."""
        with patch(
            "server.db_module.get_chat_session",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = app_client.get(
                "/api/chat/sessions/999",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 404

    def test_session_create_503_db_unavailable(self, app_client):
        """POST /api/chat/sessions returns 503 when DB is unavailable."""
        with patch("routers.chat.DB_AVAILABLE", False):
            resp = app_client.post(
                "/api/chat/sessions",
                json={"title": "Test"},
                headers={"Authorization": "Bearer fake"},
            )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# TestChatActionTools — cloud-chat dispatch of the action tools
# (find_people, create_task) via routers.chat._run_tool
# ---------------------------------------------------------------------------


class TestChatActionTools:
    """Exercise the in-process tool dispatcher for the two action tools."""

    async def test_find_people_dispatch_calls_start_people_search(self):
        """find_people → routers.agents._start_people_search(org, client, roles, user)."""
        from routers.chat import _run_tool

        with (
            patch("routers.chat.DB_AVAILABLE", True),
            patch(
                "routers.agents._start_people_search",
                new_callable=AsyncMock,
                return_value={"run_id": 77, "status": "running"},
            ) as mock_ps,
        ):
            result, sources = await _run_tool(
                "find_people",
                {"client_name": "Bosch", "target_roles": "CISO, IT-Architekt"},
                org_id=1,
                user_id=5,
            )

        mock_ps.assert_awaited_once()
        # scoped to caller's org + user, roles forwarded
        assert mock_ps.await_args.args[0] == 1
        assert mock_ps.await_args.args[1] == "Bosch"
        assert mock_ps.await_args.kwargs["target_roles"] == "CISO, IT-Architekt"
        assert mock_ps.await_args.kwargs["user_id"] == 5
        assert "run #77" in result
        assert sources == []

    async def test_find_people_requires_client_name(self):
        """find_people with no client_name returns an error and never dispatches."""
        from routers.chat import _run_tool

        with (
            patch("routers.chat.DB_AVAILABLE", True),
            patch("routers.agents._start_people_search", new_callable=AsyncMock) as mock_ps,
        ):
            result, _ = await _run_tool("find_people", {"client_name": "  "}, org_id=1, user_id=5)

        mock_ps.assert_not_awaited()
        assert "No client name" in result

    async def test_create_task_dispatch_calls_db_create_task(self):
        """create_task → db.create_task(org, user, title, client_name, notes, due_date)."""
        import datetime as _dt
        from routers.chat import _run_tool

        created_row = {
            "id": 3, "org_id": 1, "user_id": 5, "title": "Call about renewal",
            "client_name": "Bosch", "due_date": _dt.date(2026, 8, 1),
        }
        with (
            patch("routers.chat.DB_AVAILABLE", True),
            patch(
                "routers.chat.db_module.create_task",
                new_callable=AsyncMock,
                return_value=created_row,
            ) as mock_ct,
        ):
            result, sources = await _run_tool(
                "create_task",
                {
                    "title": "Call about renewal",
                    "client_name": "Bosch",
                    "due_date": "2026-08-01",
                    "notes": "renewal in Q3",
                },
                org_id=1,
                user_id=5,
            )

        mock_ct.assert_awaited_once()
        # positional: org_id, user_id, title
        assert mock_ct.await_args.args[0] == 1
        assert mock_ct.await_args.args[1] == 5
        assert mock_ct.await_args.args[2] == "Call about renewal"
        assert mock_ct.await_args.kwargs["client_name"] == "Bosch"
        assert mock_ct.await_args.kwargs["notes"] == "renewal in Q3"
        assert mock_ct.await_args.kwargs["due_date"] == _dt.date(2026, 8, 1)
        assert "Call about renewal" in result
        assert sources == []

    async def test_create_task_requires_title(self):
        """create_task with an empty title returns an error and never writes."""
        from routers.chat import _run_tool

        with (
            patch("routers.chat.DB_AVAILABLE", True),
            patch("routers.chat.db_module.create_task", new_callable=AsyncMock) as mock_ct,
        ):
            result, _ = await _run_tool("create_task", {"title": "   "}, org_id=1, user_id=5)

        mock_ct.assert_not_awaited()
        assert "No task title" in result

    async def test_create_task_bad_due_date_is_ignored(self):
        """An unparseable due_date is dropped (passed as None), task still created."""
        from routers.chat import _run_tool

        with (
            patch("routers.chat.DB_AVAILABLE", True),
            patch(
                "routers.chat.db_module.create_task",
                new_callable=AsyncMock,
                return_value={"id": 9, "title": "Follow up", "client_name": None},
            ) as mock_ct,
        ):
            await _run_tool(
                "create_task",
                {"title": "Follow up", "due_date": "not-a-date"},
                org_id=1,
                user_id=5,
            )

        assert mock_ct.await_args.kwargs["due_date"] is None


# ---------------------------------------------------------------------------
# TestChatModelResolution — WP11, default/Python backend (POST /api/chat).
#
# Asserts the outgoing LLM call's provider+model by mocking only the HTTP
# transport (llm.requests.post) — llm.resolve()'s real org-overlay / platform
# config / legacy-key fallback chain runs end to end, exactly as it would in
# production. Regression coverage for: an org configured a provider through
# the setup wizard (which only ever writes the 'default' role — see
# static/setup.html saveProvider()) and chat used to still pull its model
# from config.yaml's legacy pi_chat_model/agent_service_model, pairing the
# org's own provider with a model it was never told about.
# ---------------------------------------------------------------------------

def _fake_openai_response(content="ok"):
    """Stands in for requests.post()'s Response in llm.py's openai-compat
    adapter (_openai_chat) — status/json/raise_for_status only, no tool_calls
    so the tool-calling loop returns after exactly one round."""
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock(return_value=None)
    resp.json = MagicMock(return_value={
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    })
    return resp


# WP11c — fixed values for the legacy chat-role config keys. Tests reasoning
# about llm._legacy_synthesis()'s "no llm: block" fallback pin these instead
# of reading context.config's ambient values: a real checkout can carry an
# untracked, gitignored config.local.yaml overlay (merged into context.config
# at load time) that sets pi_chat_brain to a value outside
# llm._LEGACY_BRAIN_TO_PROVIDER's known set ({"openrouter","claude","ollama"})
# — e.g. a subscription-OAuth brain like "openai-codex". _legacy_synthesis()
# then maps that unknown brain to "openrouter" (its own fallback), while
# llm.provider_for_brain() — used elsewhere, including by the pre-WP11c
# version of these tests to compute the "expected" provider — passes an
# unknown brain through unchanged, producing "openai-codex". Two different
# mappings for the same unmapped input meant a test computing its expectation
# from the ambient brain agreed with production only by coincidence (when
# config.local.yaml happens to leave pi_chat_brain at a value both mappings
# handle alike, e.g. "openrouter"). Pinning to "ollama" here sidesteps that:
# it's in both mappings' known set, so brain and provider name coincide.
_LEGACY_CHAT_CONFIG = {
    "pi_chat_brain": "ollama",
    "pi_chat_model": "test-legacy-pi-chat-model",
    "agent_service_brain": "claude",
    "agent_service_model": "test-legacy-agent-service-model",
}


def _pin_legacy_chat_config(monkeypatch):
    """Pin _LEGACY_CHAT_CONFIG's keys onto context.config and remove any llm:
    block, so a test exercising the "no org overlay, no llm: block" fallback
    is hermetic to ambient config.yaml/config.local.yaml content. monkeypatch
    restores every value (or absence) automatically at test teardown."""
    for key, value in _LEGACY_CHAT_CONFIG.items():
        monkeypatch.setitem(context.config, key, value)
    monkeypatch.delitem(context.config, "llm", raising=False)


class TestChatModelResolution:
    ORG_ID = 1   # FAKE_USER["org_id"]

    def setup_method(self, method):
        llm.invalidate_org_overlay(self.ORG_ID)

    def teardown_method(self, method):
        llm.invalidate_org_overlay(self.ORG_ID)

    def _seed_org_overlay(self, providers, roles, plan="light", enforce=False):
        llm._org_overlays[self.ORG_ID] = (
            _time.monotonic() + 60,
            {"plan": plan, "providers": providers, "roles": roles,
             "budget": None, "month_cost": 0.0, "enforce": enforce},
        )

    def _post_chat(self, app_client, message="hi", model=None):
        """POST /api/chat with the HTTP transport mocked; returns the captured
        {url, json} of the single outgoing llm.requests.post call."""
        capture: dict = {}

        def fake_post(url, headers=None, json=None, timeout=None, stream=False):
            capture["url"] = url
            capture["json"] = json
            return _fake_openai_response("ok")

        body = {"message": message}
        if model is not None:
            body["model"] = model

        with (
            patch("server.db_module.list_clients", new_callable=AsyncMock, return_value=[]),
            patch("server.db_module.list_contacts", new_callable=AsyncMock, return_value=[]),
            # ensure_org_overlay's DB round trip — {} when no cache hit applies
            # (the "no org config" test), harmless no-op when a cache entry is
            # seeded (the TTL check short-circuits before these run).
            patch("context.db_module.get_org_settings", new_callable=AsyncMock, return_value={}),
            patch("context.db_module.llm_usage_month_cost", new_callable=AsyncMock, return_value=0.0),
            # llm.chat()'s post-call usage metering (fire-and-forget) — mocked
            # so it doesn't try a real DB write off the (unconnected) pool.
            patch("context.db_module.record_llm_usage", new_callable=AsyncMock),
            patch("llm.requests.post", side_effect=fake_post),
        ):
            resp = app_client.post(
                "/api/chat", json=body, headers={"Authorization": "Bearer fake"},
            )
        assert resp.status_code == 200, resp.text
        assert capture, "llm.requests.post was never called"
        return capture

    def test_org_chat_role_model_wins(self, app_client):
        """(a) org has its own 'chat' role -> that role's provider+model are
        used, not config.yaml's pi_chat_model/agent_service_model."""
        self._seed_org_overlay(
            providers={"ollama": {"kind": "openai-compat",
                                   "base_url": "http://127.0.0.1:19999/v1",
                                   "api_key": "local"}},
            roles={"chat": {"provider": "ollama", "model": "llama3.2:latest"},
                   "default": {"provider": "ollama", "model": "should-not-be-used"}},
        )
        cap = self._post_chat(app_client)
        assert cap["json"]["model"] == "llama3.2:latest"
        assert cap["url"].startswith("http://127.0.0.1:19999/v1/")

    def test_org_default_role_used_when_no_chat_role(self, app_client):
        """(b) the wizard only ever writes a 'default' role (never 'chat') ->
        resolve()'s `roles.get(role) or roles.get('default')` picks it up."""
        self._seed_org_overlay(
            providers={"ollama": {"kind": "openai-compat",
                                   "base_url": "http://127.0.0.1:19999/v1",
                                   "api_key": "local"}},
            roles={"default": {"provider": "ollama", "model": "llama3.2:latest"}},
        )
        cap = self._post_chat(app_client)
        assert cap["json"]["model"] == "llama3.2:latest"

    def test_no_org_config_falls_back_to_legacy_config_keys(self, app_client, monkeypatch):
        """(c) no org overlay + no llm: block -> byte-identical to today's
        hardcoded `body.model or config.get('pi_chat_model') or
        config.get('agent_service_model', 'deepseek/deepseek-v4-flash')`.

        The legacy config keys are pinned (see _pin_legacy_chat_config, WP11c)
        rather than read from ambient context.config — a real checkout's
        untracked config.local.yaml overlay must not change this test's
        outcome."""
        _pin_legacy_chat_config(monkeypatch)
        expected_model = (
            context.config.get("pi_chat_model")
            or context.config.get("agent_service_model", "deepseek/deepseek-v4-flash")
        )
        cap = self._post_chat(app_client)
        assert cap["json"]["model"] == expected_model

    def test_platform_llm_block_chat_role_wins_over_legacy_keys(self, app_client, monkeypatch):
        """(2a review follow-up) A platform install WITH an llm: block whose
        roles.chat.model differs from the legacy pi_chat_model must use the
        llm: block's model. config.yaml ships both values equal today, so
        test (c) alone would pass even if the llm: block were ignored
        entirely — this pins the actual precedence (llm: block beats the
        legacy keys) rather than relying on that coincidence. Legacy keys are
        pinned too (WP11c) so an ambient config.local.yaml can't coincidentally
        make them equal and mask a regression."""
        _pin_legacy_chat_config(monkeypatch)
        monkeypatch.setitem(context.config, "llm", {
            "providers": {"openrouter": {"kind": "openai-compat",
                                          "base_url": "https://openrouter.example/v1",
                                          "api_key": "test-key"}},
            "roles": {"chat": {"provider": "openrouter", "model": "llm-block-chat-model"}},
        })
        assert context.config["pi_chat_model"] != "llm-block-chat-model"   # sanity: genuinely different
        cap = self._post_chat(app_client)
        assert cap["json"]["model"] == "llm-block-chat-model"

    def test_explicit_body_model_wins(self, app_client):
        """(d) body.model, when supplied, wins over the org's own role model."""
        self._seed_org_overlay(
            providers={"ollama": {"kind": "openai-compat",
                                   "base_url": "http://127.0.0.1:19999/v1",
                                   "api_key": "local"}},
            roles={"chat": {"provider": "ollama", "model": "llama3.2:latest"}},
        )
        cap = self._post_chat(app_client, model="explicit-override-model")
        assert cap["json"]["model"] == "explicit-override-model"


# ---------------------------------------------------------------------------
# TestPiChatModelResolution — WP11, Pi backend path.
#
# agent-pi resolves PROVIDER credentials itself from org_id (agent.ts
# buildModel() -> resolveProviderForOrg()), but never a MODEL — the payload's
# `model` field is used exactly as sent — so _resolve_pi_chat_target must
# resolve it on the Python side, from the same org-overlay chat/default role
# as the Python backend, with the same legacy-key fallback when there is
# nothing to resolve from.
# ---------------------------------------------------------------------------

class TestPiChatModelResolution:
    ORG_ID = 1

    def setup_method(self, method):
        llm.invalidate_org_overlay(self.ORG_ID)

    def teardown_method(self, method):
        llm.invalidate_org_overlay(self.ORG_ID)

    def _seed_org_overlay(self, providers, roles, plan="light", enforce=False):
        llm._org_overlays[self.ORG_ID] = (
            _time.monotonic() + 60,
            {"plan": plan, "providers": providers, "roles": roles,
             "budget": None, "month_cost": 0.0, "enforce": enforce},
        )

    async def test_org_chat_role_resolved(self):
        """(a) org has its own 'chat' role -> its provider+model are resolved
        for the agent-pi payload."""
        from routers.chat import _resolve_pi_chat_target

        self._seed_org_overlay(
            providers={"ollama": {"kind": "openai-compat",
                                   "base_url": "http://127.0.0.1:19999/v1",
                                   "api_key": "local"}},
            roles={"chat": {"provider": "ollama", "model": "llama3.2:latest"},
                   "default": {"provider": "ollama", "model": "should-not-be-used"}},
        )
        with (
            patch("context.db_module.get_org_settings", new_callable=AsyncMock, return_value={}),
            patch("context.db_module.llm_usage_month_cost", new_callable=AsyncMock, return_value=0.0),
        ):
            provider, brain, model = await _resolve_pi_chat_target(self.ORG_ID)
        assert provider == "ollama"
        assert model == "llama3.2:latest"

    async def test_org_default_role_resolved_when_no_chat_role(self):
        """(b) org configured only 'default' -> resolve() falls back to it."""
        from routers.chat import _resolve_pi_chat_target

        self._seed_org_overlay(
            providers={"ollama": {"kind": "openai-compat",
                                   "base_url": "http://127.0.0.1:19999/v1",
                                   "api_key": "local"}},
            roles={"default": {"provider": "ollama", "model": "llama3.2:latest"}},
        )
        with (
            patch("context.db_module.get_org_settings", new_callable=AsyncMock, return_value={}),
            patch("context.db_module.llm_usage_month_cost", new_callable=AsyncMock, return_value=0.0),
        ):
            provider, brain, model = await _resolve_pi_chat_target(self.ORG_ID)
        assert provider == "ollama"
        assert model == "llama3.2:latest"

    async def test_no_org_config_falls_back_to_legacy_config_keys(self, monkeypatch):
        """(c) no org overlay + no llm: block -> byte-identical to today's
        provider_for_brain(pi_chat_brain) / pi_chat_model formula.

        The legacy config keys are pinned (see _pin_legacy_chat_config, WP11c)
        rather than read from ambient context.config — an untracked
        config.local.yaml in a real checkout can set pi_chat_brain to a value
        (e.g. "openai-codex") that llm._legacy_synthesis()'s brain->provider
        map and llm.provider_for_brain() disagree on, which made this test's
        "expected" computation diverge from what production actually resolves
        to whenever such an overlay is present."""
        from routers.chat import _resolve_pi_chat_target

        _pin_legacy_chat_config(monkeypatch)
        expected_brain = (context.config.get("pi_chat_brain")
                           or context.config.get("agent_service_brain", "openrouter"))
        expected_model = (context.config.get("pi_chat_model")
                           or context.config.get("agent_service_model", "deepseek/deepseek-v4-flash"))
        with (
            patch("context.db_module.get_org_settings", new_callable=AsyncMock, return_value={}),
            patch("context.db_module.llm_usage_month_cost", new_callable=AsyncMock, return_value=0.0),
        ):
            provider, brain, model = await _resolve_pi_chat_target(self.ORG_ID)
        assert provider == llm.provider_for_brain(expected_brain)
        assert brain == expected_brain
        assert model == expected_model

    async def test_platform_llm_block_chat_role_wins_over_legacy_keys(self, monkeypatch):
        """(2a review follow-up) Same precedence pin as the Python path: a
        platform llm: block's roles.chat.model must win over the legacy
        pi_chat_model when the two differ, not just when they happen to
        coincide (as they do in the shipped config.yaml). Legacy keys are
        pinned too (WP11c) so an ambient config.local.yaml can't
        coincidentally make them equal and mask a regression."""
        from routers.chat import _resolve_pi_chat_target

        _pin_legacy_chat_config(monkeypatch)
        monkeypatch.setitem(context.config, "llm", {
            "providers": {"openrouter": {"kind": "openai-compat",
                                          "base_url": "https://openrouter.example/v1",
                                          "api_key": "test-key"}},
            "roles": {"chat": {"provider": "openrouter", "model": "llm-block-chat-model"}},
        })
        assert context.config["pi_chat_model"] != "llm-block-chat-model"   # sanity: genuinely different
        with (
            patch("context.db_module.get_org_settings", new_callable=AsyncMock, return_value={}),
            patch("context.db_module.llm_usage_month_cost", new_callable=AsyncMock, return_value=0.0),
        ):
            provider, brain, model = await _resolve_pi_chat_target(self.ORG_ID)
        assert provider == "openrouter"
        assert model == "llm-block-chat-model"

    async def test_resolve_error_falls_back_to_legacy_config_keys(self):
        """An enforced light org with no provider makes llm.resolve() raise;
        _resolve_pi_chat_target must not propagate that — it falls back to
        the same legacy formula (budget/enforce were never wired into the Pi
        chat path before this fix either, so this preserves that instead of
        newly blocking chat on it)."""
        from routers.chat import _resolve_pi_chat_target

        self._seed_org_overlay(providers={}, roles={}, enforce=True)
        expected_brain = (context.config.get("pi_chat_brain")
                           or context.config.get("agent_service_brain", "openrouter"))
        expected_model = (context.config.get("pi_chat_model")
                           or context.config.get("agent_service_model", "deepseek/deepseek-v4-flash"))
        provider, brain, model = await _resolve_pi_chat_target(self.ORG_ID)
        assert provider == llm.provider_for_brain(expected_brain)
        assert model == expected_model

    async def test_call_pi_chat_sends_resolved_provider_and_model(self):
        """End-to-end: _call_pi_chat's payload carries the org's resolved
        provider+model, not the legacy config-key pair (mocks httpx, the Pi
        service's own HTTP transport)."""
        from routers.chat import _call_pi_chat

        self._seed_org_overlay(
            providers={"ollama": {"kind": "openai-compat",
                                   "base_url": "http://127.0.0.1:19999/v1",
                                   "api_key": "local"}},
            roles={"chat": {"provider": "ollama", "model": "llama3.2:latest"}},
        )
        fake_resp = MagicMock()
        fake_resp.raise_for_status = MagicMock(return_value=None)
        fake_resp.json = MagicMock(return_value={"answer": "hi from ollama", "sources": []})
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=fake_resp) as mock_post:
            answer, sources = await _call_pi_chat("hello", self.ORG_ID, None, "Acme", [])
        assert answer == "hi from ollama"
        payload = mock_post.call_args.kwargs["json"]
        assert payload["provider"] == "ollama"
        assert payload["model"] == "llama3.2:latest"
