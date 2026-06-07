import asyncio
import sys
import types


if "aiohttp" not in sys.modules:
    aiohttp_stub = types.ModuleType("aiohttp")
    aiohttp_stub.ClientSession = object
    aiohttp_stub.web = types.SimpleNamespace()
    sys.modules["aiohttp"] = aiohttp_stub

from telegram_bot import TelegramBotApp


class PageStorage:
    def __init__(self):
        self.upserted = []

    def upsert_pages(self, account_id, pages):
        self.upserted.append((account_id, pages))


def build_refresh_app():
    app = TelegramBotApp.__new__(TelegramBotApp)
    app.storage = PageStorage()
    app.debug_event = lambda *args, **kwargs: None
    app.account_owner_scope = lambda user_id: user_id

    async def user_language(user_id=0):
        return "en"

    async def discover_pages(account_id, owner_id=None):
        return [{"page_name": "Huawei"}, {"page_name": "Oppo"}]

    app.user_language = user_language
    app.discover_pages = discover_pages
    return app


def test_refresh_pages_uses_one_message_with_in_place_updates():
    app = build_refresh_app()
    sends = []
    edits = []

    async def send_message(chat_id, text, reply_to_message_id=0, *, reply_markup=None, parse_mode=""):
        sends.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_to_message_id": reply_to_message_id,
                "reply_markup": reply_markup,
            }
        )
        return {"ok": True, "result": {"message_id": 777}}

    async def try_edit_message(chat_id, message_id, text, *, reply_markup=None, parse_mode="", timeout_seconds=12):
        edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
                "timeout_seconds": timeout_seconds,
            }
        )
        return True

    app.send_message = send_message
    app.try_edit_message = try_edit_message

    asyncio.run(app.command_discover_pages(123, 456, ["acct_1"], 99, refresh=True))

    all_text = "\n".join([item["text"] for item in sends] + [item["text"] for item in edits])
    assert len(sends) == 1
    assert len(edits) >= 3
    assert "Dashboard keyboard restored" not in all_text
    assert "Refreshed and cached 2 page(s):" in edits[-1]["text"]
    assert "- Huawei" in edits[-1]["text"]
    assert "- Oppo" in edits[-1]["text"]
    assert "inline_keyboard" in sends[0]["reply_markup"]
    assert "keyboard" not in sends[0]["reply_markup"]
    assert app.storage.upserted == [
        ("acct_1", [{"page_name": "Huawei"}, {"page_name": "Oppo"}])
    ]
