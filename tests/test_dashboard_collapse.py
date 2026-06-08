import asyncio
import sys
import types


if "aiohttp" not in sys.modules:
    aiohttp_stub = types.ModuleType("aiohttp")
    aiohttp_stub.ClientSession = object
    aiohttp_stub.web = types.SimpleNamespace()
    sys.modules["aiohttp"] = aiohttp_stub

from telegram_bot import TelegramBotApp


def test_dashboard_collapse_and_expand_actions_refresh_user_dashboard():
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.dashboard_sessions = {"123:99": {"action": "post", "step": "caption"}}
        app.dashboard_collapsed_by_user = {}
        shown = []

        async def user_language(user_id=0):
            return "en"

        async def show_dashboard(chat_id, message_id=0, prefix="", user_id=0, edit_message_id=0):
            shown.append(
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "prefix": prefix,
                    "user_id": user_id,
                    "collapsed": app.dashboard_is_collapsed(user_id),
                }
            )
            return 700 + len(shown)

        app.user_language = user_language
        app.show_dashboard = show_dashboard

        await app.handle_dashboard_button(123, 99, 456, "collapse_dashboard")
        await app.handle_dashboard_button(123, 99, 457, "expand_dashboard")

        assert "123:99" not in app.dashboard_sessions
        assert shown[0]["collapsed"] is True
        assert shown[0]["prefix"] == "Dashboard collapsed."
        assert shown[1]["collapsed"] is False
        assert shown[1]["prefix"] == "Dashboard expanded."

    asyncio.run(run())
