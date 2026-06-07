import asyncio
import sys
import types


if "aiohttp" not in sys.modules:
    aiohttp_stub = types.ModuleType("aiohttp")
    aiohttp_stub.ClientSession = object
    aiohttp_stub.web = types.SimpleNamespace()
    sys.modules["aiohttp"] = aiohttp_stub

import telegram_bot
from telegram_bot import TelegramBotApp


def test_resolve_account_label_uses_detected_facebook_name(monkeypatch):
    async def fake_lookup(cookies_json):
        assert "c_user" in cookies_json
        return True, "Mohammed Mohammed", ""

    monkeypatch.setitem(
        sys.modules,
        "playwright_engine",
        types.SimpleNamespace(get_facebook_account_name=fake_lookup),
    )

    app = TelegramBotApp.__new__(TelegramBotApp)
    label, source_en, source_ar, resolved = asyncio.run(
        app.resolve_account_label_from_cookie_header("c_user=123; xs=token", "Facebook Account")
    )

    assert resolved is True
    assert label == "Mohammed Mohammed"
    assert source_en == "Auto-detected"
    assert source_ar == "تم الكشف تلقائيًا"


def test_resolve_account_label_keeps_fallback_when_lookup_fails(monkeypatch):
    async def fake_lookup(cookies_json):
        return False, "", "not found"

    monkeypatch.setitem(
        sys.modules,
        "playwright_engine",
        types.SimpleNamespace(get_facebook_account_name=fake_lookup),
    )
    monkeypatch.setattr(telegram_bot, "_env_int", lambda *args, **kwargs: 3)

    app = TelegramBotApp.__new__(TelegramBotApp)
    label, source_en, source_ar, resolved = asyncio.run(
        app.resolve_account_label_from_cookie_header("c_user=123; xs=token", "Facebook Account")
    )

    assert resolved is False
    assert label == "Facebook Account"
    assert source_en == "Lookup queued"
    assert source_ar == "البحث عن الاسم في الانتظار"
