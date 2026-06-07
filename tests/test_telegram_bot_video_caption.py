import asyncio
import sys
import time
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


def build_session_app(session):
    app = TelegramBotApp.__new__(TelegramBotApp)
    app.dashboard_sessions = {"123:99": {**session, "updated_at": time.time()}}
    app.update_locks = {}
    sent = []

    def start_background_task(coro, label):
        coro.close()
        return None

    async def send_message(chat_id, text, reply_to_message_id=0, *, reply_markup=None, parse_mode=""):
        sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_to_message_id": reply_to_message_id,
                "reply_markup": reply_markup,
            }
        )
        return {"ok": True, "result": {"message_id": 777}}

    async def user_language(user_id=0):
        return "en"

    app.start_background_task = start_background_task
    app.send_message = send_message
    app.user_language = user_language
    return app, sent


def test_active_multi_video_upload_consumes_stale_dashboard_button_text():
    async def run():
        app, sent = build_session_app(
            {
                "action": "post",
                "step": "multi_video_upload",
                "lang": "en",
                "account_id": "acct_1",
                "pages": [{"page_name": "Huawei"}, {"page_name": "Oppo"}],
                "selected_pages": [0, 1],
                "multi_media_paths": [],
            }
        )

        async def handle_dashboard_button(*args, **kwargs):
            raise AssertionError("stale dashboard button should not replace the upload card")

        app.handle_dashboard_button = handle_dashboard_button

        await app.handle_update(
            {
                "message": {
                    "message_id": 456,
                    "text": "🎬 Video Post",
                    "chat": {"id": 123},
                    "from": {"id": 99},
                }
            }
        )

        assert len(sent) == 1
        assert "Multi Video Upload" in sent[0]["text"]
        assert "Send video 1 of 2 now." in sent[0]["text"]

    asyncio.run(run())


def test_free_text_without_active_session_does_not_send_dashboard_card():
    async def run():
        app, sent = build_session_app({})
        app.dashboard_sessions = {}

        async def show_dashboard(*args, **kwargs):
            raise AssertionError("free text without a session should be ignored")

        app.show_dashboard = show_dashboard

        await app.handle_update(
            {
                "message": {
                    "message_id": 456,
                    "text": "caption that is not part of an active flow",
                    "chat": {"id": 123},
                    "from": {"id": 99},
                }
            }
        )

        assert sent == []

    asyncio.run(run())


def test_multi_video_download_error_keeps_upload_session_active():
    async def run():
        app, sent = build_session_app(
            {
                "action": "post",
                "step": "multi_video_upload",
                "lang": "en",
                "account_id": "acct_1",
                "pages": [{"page_name": "Huawei"}, {"page_name": "Oppo"}],
                "selected_pages": [0, 1],
                "multi_media_paths": [],
            }
        )

        async def download_file(file_id, account_id):
            raise RuntimeError("Telegram did not return a downloadable file path")

        app.download_file = download_file

        handled = await app.handle_dashboard_session(
            123,
            99,
            456,
            "",
            {"video": {"file_id": "video_1"}},
        )

        assert handled is True
        assert len(sent) == 1
        assert "I could not download that video from Telegram" in sent[0]["text"]
        assert "Send video 1 of 2 now." in sent[0]["text"]
        session = app.get_dashboard_session(123, 99)
        assert session["step"] == "multi_video_upload"
        assert session["multi_media_paths"] == []

    asyncio.run(run())
