import asyncio
import sys
import types


if "aiohttp" not in sys.modules:
    aiohttp_stub = types.ModuleType("aiohttp")
    aiohttp_stub.ClientSession = object
    aiohttp_stub.web = types.SimpleNamespace()
    sys.modules["aiohttp"] = aiohttp_stub

from telegram_bot import TelegramBotApp


def test_multi_video_caption_prompt_uses_shared_caption_step():
    app = TelegramBotApp.__new__(TelegramBotApp)
    session = {
        "lang": "en",
        "pages": [{"page_name": "Huawei"}, {"page_name": "Oppo"}],
        "selected_pages": [0, 1],
        "multi_media_paths": ["/tmp/one.mp4", "/tmp/two.mp4"],
        "caption_draft": "Shared caption",
    }

    text = app.multi_video_caption_prompt(session)

    assert "Video Caption" in text
    assert "Send one shared caption for all videos." in text
    assert "tap ✅ Done" in text
    assert "Current caption: Shared caption" in text


def test_edit_message_ignores_telegram_message_not_modified():
    app = TelegramBotApp.__new__(TelegramBotApp)
    calls = []

    async def fake_telegram_api(method, payload):
        calls.append((method, payload))
        return {
            "ok": False,
            "description": "Bad Request: message is not modified: specified new message content and reply markup are exactly the same",
        }

    app.telegram_api = fake_telegram_api

    asyncio.run(app.edit_message(123, 456, "same text", reply_markup={"inline_keyboard": []}))

    assert calls[0][0] == "editMessageText"
