import asyncio
import json
import sys
import types

import pytest

from session_manager import SessionManager


if "aiohttp" not in sys.modules:
    aiohttp_stub = types.ModuleType("aiohttp")
    aiohttp_stub.ClientSession = object
    aiohttp_stub.web = types.SimpleNamespace()
    sys.modules["aiohttp"] = aiohttp_stub

from telegram_bot import TelegramBotApp


class CookieSafetyStorage:
    def __init__(self):
        self.validation_updates = []
        self.get_cookie_called = False
        self.account = {
            "account_id": "acct_1",
            "cookie_status": "valid",
            "cookie_status_detail": "Facebook session is valid",
        }

    def get_account(self, account_id, owner_id=None):
        return dict(self.account)

    def get_account_cookie(self, account_id, owner_id=None):
        self.get_cookie_called = True
        return "c_user=123; xs=session"

    def update_account_cookie_validation(self, account_id, status, detail="", owner_id=None):
        self.validation_updates.append((account_id, status, detail, owner_id))
        self.account["cookie_status"] = status
        self.account["cookie_status_detail"] = detail
        return True


def test_logout_text_triggers_cookie_session_security_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    manager = SessionManager(str(tmp_path / "sessions.json"))
    cookies = json.dumps([{"name": "c_user", "value": "123"}, {"name": "xs", "value": "session"}])

    manager.mark_session_used(cookies, False, "Facebook says this session is logged out and must log in again.")

    can_use, reason = manager.can_use_session(cookies, security_cooldown_seconds=3600)
    assert can_use is False
    assert "Unlock the account manually and re-add fresh cookies" in reason


def test_successful_use_respects_configured_min_interval(tmp_path, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    manager = SessionManager(str(tmp_path / "sessions.json"))
    cookies = json.dumps([{"name": "c_user", "value": "123"}, {"name": "xs", "value": "session"}])

    manager.mark_session_used(cookies, True, "")

    can_use, reason = manager.can_use_session(cookies, min_interval_seconds=600)
    assert can_use is False
    assert "before reusing this Facebook cookie session" in reason

    can_use, reason = manager.can_use_session(cookies, min_interval_seconds=0)
    assert can_use is True
    assert reason == "OK"


def test_playwright_session_guard_honors_min_interval_toggle(monkeypatch):
    async def run():
        import playwright_engine

        calls = []

        def fake_can_use_session(cookies_json, *, min_interval_seconds=0, security_cooldown_seconds=0):
            calls.append((cookies_json, min_interval_seconds, security_cooldown_seconds))
            return True, "OK"

        monkeypatch.setattr(playwright_engine, "POST_COOKIE_SESSION_TRACKING_ENABLED", True)
        monkeypatch.setattr(playwright_engine, "POST_COOKIE_MIN_INTERVAL_SECONDS", 600)
        monkeypatch.setattr(playwright_engine, "POST_COOKIE_SECURITY_COOLDOWN_SECONDS", 21600)
        monkeypatch.setattr(playwright_engine.session_manager, "can_use_session", fake_can_use_session)

        await playwright_engine._ensure_cookie_session_can_run(
            "cookies",
            "posting",
            None,
            enforce_min_interval=True,
        )
        await playwright_engine._ensure_cookie_session_can_run(
            "cookies",
            "page discovery",
            None,
            enforce_min_interval=False,
        )

        assert calls == [
            ("cookies", 600, 21600),
            ("cookies", 0, 21600),
        ]

    asyncio.run(run())


def test_posting_session_loss_quarantines_account_cookie():
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.storage = CookieSafetyStorage()
        app.admin_ids = set()

        changed = await app.maybe_quarantine_account_cookie(
            "acct_1",
            99,
            "Cookies expired or invalid. Facebook login required.",
            trace_id="test_trace",
            context="test",
        )

        assert changed is True
        update = app.storage.validation_updates[0]
        assert update[0] == "acct_1"
        assert update[1] == "invalid"
        assert "Re-login manually and add fresh cookies" in update[2]
        assert update[3] == 99

    asyncio.run(run())


def test_quarantined_cookie_blocks_future_queue_preflight():
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        storage = CookieSafetyStorage()
        storage.account["cookie_status"] = "invalid"
        storage.account["cookie_status_detail"] = (
            "Posting stopped because Facebook reported this session as logged out."
        )
        app.storage = storage
        app.admin_ids = set()

        with pytest.raises(RuntimeError) as exc_info:
            await app.ensure_account_cookie_readable("acct_1", 99, "en")

        assert "Invalid cookie. Re-login to Facebook manually" in str(exc_info.value)
        assert storage.get_cookie_called is False

    asyncio.run(run())
