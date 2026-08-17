"""Telegram per-user routing (Phase 6b): prefs, linking, notify_user/org/run, bot commands."""

import asyncio
import time

import pytest

import context
import notifications as n


class _FakeDB:
    def __init__(self):
        # users: id → row (settings JSONB)
        self.users = {
            1: {"id": 1, "org_id": 8, "username": "anna", "display_name": "Anna", "role": "admin", "org_name": "Demo",
                "settings": {"telegram": {"chat_id": "111"}}},
            2: {"id": 2, "org_id": 8, "username": "ben", "display_name": "Ben", "role": "member", "org_name": "Demo",
                "settings": {"telegram": {"chat_id": "222"}, "telegram_prefs": {"digest": False, "auto_runs": True}}},
            3: {"id": 3, "org_id": 8, "username": "cara", "display_name": "Cara", "role": "member", "org_name": "Demo",
                "settings": {}},                                     # not linked
            4: {"id": 4, "org_id": 9, "username": "pat", "display_name": "Pat", "role": "admin", "org_name": "Other",
                "settings": {"telegram": {"chat_id": "444"}}},
        }

    async def get_user_with_settings(self, uid): return self.users.get(uid)
    async def list_users_with_settings(self, org_id): return [u for u in self.users.values() if u["org_id"] == org_id]
    async def find_user_by_telegram_chat(self, chat_id):
        return next((u for u in self.users.values() if (u["settings"].get("telegram") or {}).get("chat_id") == str(chat_id)), None)
    async def find_user_by_telegram_link_code(self, code):
        return next((u for u in self.users.values() if (u["settings"].get("telegram_link") or {}).get("code") == code), None)
    async def patch_user_settings_by_id(self, uid, patch): self.users[uid]["settings"].update(patch); return self.users[uid]["settings"]
    async def remove_user_setting_keys(self, uid, keys):
        for k in keys: self.users[uid]["settings"].pop(k, None)


@pytest.fixture
def env(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(context, "db_module", db)
    monkeypatch.setattr(context, "DB_AVAILABLE", True)
    monkeypatch.setenv("TELEGRAMBOT", "123:token")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    sent = []
    monkeypatch.setattr(n, "send_to", lambda chat, text: (sent.append((chat, text)) or True))
    monkeypatch.setattr(n, "_send_document_to", lambda chat, fn, content, caption: (sent.append((chat, "DOC:" + fn)) or True))
    monkeypatch.setattr(n, "bot_username", lambda: "buzzowl_test_bot")
    return db, sent


def test_prefs_defaults_and_overrides():
    assert n.prefs_of({}) == n.DEFAULT_PREFS
    assert n.prefs_of({"telegram_prefs": {"digest": False, "bogus": True}})["digest"] is False
    assert n.linked_chat({"telegram": {"chat_id": 5}}) == "5" and n.linked_chat({}) is None


def test_notify_user_respects_link_and_prefs(env):
    db, sent = env
    assert asyncio.run(n.notify_user(1, "hi", "runs")) is True
    assert asyncio.run(n.notify_user(3, "hi", "runs")) is False          # not linked
    assert asyncio.run(n.notify_user(2, "hi", "digest")) is False        # opted out
    assert [c for c, _ in sent] == ["111"]


def test_notify_org_filters_org_role_and_pref(env):
    db, sent = env
    assert asyncio.run(n.notify_org(8, "digest!", "digest")) == 1        # anna only (ben opted out, cara unlinked, pat other org)
    assert asyncio.run(n.notify_org(8, "adm", "admin", roles=("admin",))) == 1
    assert asyncio.run(n.notify_org(8, "run", "runs", exclude_user_id=1)) == 1   # ben
    assert "444" not in [c for c, _ in sent]


def test_notify_run_goes_to_trigger_or_auto_runs_optins(env):
    db, sent = env
    assert asyncio.run(n.notify_run(8, {"triggered_by": 1}, "done")) == 1
    assert sent[-1][0] == "111"
    # automatic run: only Ben opted into auto_runs
    assert asyncio.run(n.notify_run(8, {"triggered_by": None}, "auto done", document=("cap", "r.md", b"x"))) == 1
    assert sent[-1] == ("222", "DOC:r.md")


def test_link_flow_and_bot_commands(env):
    db, sent = env
    link = asyncio.run(n.start_link(3))
    assert link["deep_link"] == f"https://t.me/buzzowl_test_bot?start={link['code']}"
    # unknown code
    asyncio.run(n.handle_update({"message": {"chat": {"id": 333}, "text": "/start nope", "from": {"username": "cara_tg"}}}))
    assert "unknown or expired" in sent[-1][1]
    # right code links chat 333 to Cara
    asyncio.run(n.handle_update({"message": {"chat": {"id": 333}, "text": f"/start {link['code']}", "from": {"username": "cara_tg"}}}))
    assert db.users[3]["settings"]["telegram"]["chat_id"] == "333" and "telegram_link" not in db.users[3]["settings"]
    assert "Linked to Buzzowl as Cara" in sent[-1][1]
    # status + stop
    asyncio.run(n.handle_update({"message": {"chat": {"id": 333}, "text": "/status"}}))
    assert "Connected as Cara" in sent[-1][1]
    asyncio.run(n.handle_update({"message": {"chat": {"id": 333}, "text": "/stop"}}))
    assert "telegram" not in db.users[3]["settings"] and "Disconnected" in sent[-1][1]
    # unlinked chat asking a question gets a hint, never an org guess
    asyncio.run(n.handle_update({"message": {"chat": {"id": 999}, "text": "who is acme?"}}))
    assert "isn't linked" in sent[-1][1]


def test_expired_code_rejected(env):
    db, sent = env
    db.users[3]["settings"]["telegram_link"] = {"code": "old", "expires": time.time() - 5}
    assert asyncio.run(n.complete_link("old", "5", {})) is None


def test_admin_chat_is_separate(monkeypatch):
    monkeypatch.setenv("TELEGRAMBOT", "t"); monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert n._configured() and not n._admin_chat_configured()
    assert n.notify_admin_chat("x") is False
