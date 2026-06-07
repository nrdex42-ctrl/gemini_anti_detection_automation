import asyncio
import sys
import types


if "aiohttp" not in sys.modules:
    aiohttp_stub = types.ModuleType("aiohttp")
    aiohttp_stub.ClientSession = object
    aiohttp_stub.web = types.SimpleNamespace()
    sys.modules["aiohttp"] = aiohttp_stub

from telegram_bot import TelegramBotApp


class CookieReportStorage:
    def __init__(self):
        self.validation_updates = []

    def list_accounts(self, owner_id=None):
        return [
            {"account_id": "valid_acct", "label": "Omar Mohamed", "active": True},
            {"account_id": "invalid_acct", "label": "اسماء ضياء", "active": True},
        ]

    def get_account_cookie(self, account_id, owner_id=None):
        if account_id == "valid_acct":
            return "c_user=100; xs=session"
        return "c_user=200; xs=session"

    def update_account_cookie_validation(self, account_id, status, detail, owner_scope):
        self.validation_updates.append((account_id, status, detail, owner_scope))


def test_cookie_validation_report_shows_only_valid_or_invalid(monkeypatch):
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.storage = CookieReportStorage()
        app.account_owner_scope = lambda user_id: user_id

        async def user_language(user_id=0):
            return "en"

        async def dashboard_reply_markup(user_id=0):
            return {}

        sent = []
        edits = []

        async def send_message(chat_id, text, reply_to_message_id=0, *, reply_markup=None, parse_mode=""):
            sent.append(text)
            return {"ok": True, "result": {"message_id": 777}}

        async def edit_or_send_message(chat_id, message_id, text, *, reply_to_message_id=0, reply_markup=None, parse_mode=""):
            edits.append(text)
            return message_id

        async def validate_facebook_session(cookies):
            cookie_text = str(cookies)
            if "100" in cookie_text:
                return True, "Facebook session is valid"
            return (
                False,
                "Session check inconclusive; browser validation skipped to protect cookie session. "
                "HTTP detail: Facebook returned homepage without auth failure markers",
            )

        import playwright_engine

        monkeypatch.setattr(playwright_engine, "validate_facebook_session", validate_facebook_session)
        app.user_language = user_language
        app.dashboard_reply_markup = dashboard_reply_markup
        app.send_message = send_message
        app.edit_or_send_message = edit_or_send_message

        await app.command_check_cookies(123, 456, 99)

        final_text = edits[-1]
        assert "Omar Mohamed" in final_text
        assert "valid" in final_text
        assert "اسماء ضياء" in final_text
        assert "invalid" in final_text
        assert "Session check inconclusive" not in final_text
        assert "HTTP detail" not in final_text

    asyncio.run(run())
