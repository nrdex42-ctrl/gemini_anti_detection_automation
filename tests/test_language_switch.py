import asyncio
import sys
import types


if "aiohttp" not in sys.modules:
    aiohttp_stub = types.ModuleType("aiohttp")
    aiohttp_stub.ClientSession = object
    aiohttp_stub.web = types.SimpleNamespace()
    sys.modules["aiohttp"] = aiohttp_stub

from telegram_bot import TelegramBotApp


def test_language_callback_sends_fresh_dashboard_to_update_reply_keyboard():
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.dashboard_sessions = {}
        shown = []
        saved = []

        def start_background_task(coro, label):
            coro.close()
            return None

        async def answer_callback_query(callback_query_id, text=""):
            return None

        async def user_language(user_id=0):
            return "en"

        def set_user_language(user_id, lang):
            saved.append((user_id, lang))
            return True

        async def show_dashboard(chat_id, message_id=0, prefix="", user_id=0, edit_message_id=0):
            shown.append(
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "prefix": prefix,
                    "user_id": user_id,
                    "edit_message_id": edit_message_id,
                }
            )
            return 777

        app.start_background_task = start_background_task
        app.answer_callback_query = answer_callback_query
        app.user_language = user_language
        app.storage = types.SimpleNamespace(set_user_language=set_user_language)
        app.is_admin_user = lambda user_id: False
        app.show_dashboard = show_dashboard

        await app.handle_callback_query(
            {
                "callback_query": {
                    "id": "cb_1",
                    "data": "lang:ar",
                    "message": {"message_id": 456, "chat": {"id": 123}},
                    "from": {"id": 99},
                }
            }
        )

        assert saved == [(99, "ar")]
        assert shown
        assert shown[0]["message_id"] == 0
        assert shown[0]["user_id"] == 99
        assert "العربية" in shown[0]["prefix"]

    asyncio.run(run())
