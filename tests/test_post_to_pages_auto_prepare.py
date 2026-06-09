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


class AutoPrepareStorage:
    def __init__(self):
        self.pages = []
        self.validation_updates = []
        self.upserted = []

    def get_account(self, account_id, owner_id=None):
        if account_id == "acct_1":
            return {"account_id": "acct_1", "label": "Omar Mohamed", "active": True}
        return None

    def get_account_cookie(self, account_id, owner_id=None):
        return "c_user=100; xs=session"

    def update_account_cookie_validation(self, account_id, status, detail, owner_scope):
        self.validation_updates.append((account_id, status, detail, owner_scope))

    def upsert_pages(self, account_id, pages):
        self.upserted.append((account_id, pages))
        self.pages = [
            {
                "page_id": str(page.get("id") or page.get("page_id") or ""),
                "page_name": str(page.get("name") or page.get("page_name") or ""),
                "page_url": str(page.get("url") or page.get("page_url") or ""),
            }
            for page in pages
        ]

    def list_pages(self, account_id, owner_id=None):
        return list(self.pages)


def test_post_to_pages_auto_checks_cookie_refreshes_pages_and_opens_page_card(monkeypatch):
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.storage = AutoPrepareStorage()
        app.dashboard_sessions = {}
        app.account_owner_scope = lambda user_id: user_id
        app.active_account_id = lambda user_id: asyncio.sleep(0, result="acct_1")

        async def user_language(user_id=0):
            return "en"

        async def validate_facebook_session(cookies):
            return True, "Facebook session is valid"

        async def discover_pages(account_id, owner_id=None):
            return [
                {"id": "p1", "name": "Insan", "url": "https://facebook.com/insan"},
                {"id": "p2", "name": "Oppo", "url": "https://facebook.com/oppo"},
            ]

        sent = []
        edits = []
        stage_controls = []

        async def send_message(chat_id, text, reply_to_message_id=0, *, reply_markup=None, parse_mode=""):
            sent.append({"text": text, "reply_markup": reply_markup})
            return {"ok": True, "result": {"message_id": 777}}

        async def edit_or_send_message(
            chat_id,
            message_id,
            text,
            *,
            reply_to_message_id=0,
            reply_markup=None,
            parse_mode="",
            timeout_seconds=12,
        ):
            edits.append({"message_id": message_id, "text": text, "reply_markup": reply_markup})
            return message_id or 777

        async def send_post_stage_controls(chat_id, user_id, reply_to_message_id, session, stage):
            stage_controls.append((stage, list(session.get("pages") or [])))

        import playwright_engine

        monkeypatch.setattr(playwright_engine, "validate_facebook_session", validate_facebook_session)
        app.user_language = user_language
        app.discover_pages = discover_pages
        app.send_message = send_message
        app.edit_or_send_message = edit_or_send_message
        app.send_post_stage_controls = send_post_stage_controls

        await app.handle_dashboard_button(123, 99, 456, "post_active")

        final = edits[-1]
        labels = [button["text"] for row in final["reply_markup"]["inline_keyboard"] for button in row]
        callbacks = [button["callback_data"] for row in final["reply_markup"]["inline_keyboard"] for button in row]

        assert app.storage.validation_updates == [("acct_1", "valid", "Facebook session is valid", 99)]
        assert app.storage.upserted == [
            (
                "acct_1",
                [
                    {"id": "p1", "name": "Insan", "url": "https://facebook.com/insan"},
                    {"id": "p2", "name": "Oppo", "url": "https://facebook.com/oppo"},
                ],
            )
        ]
        assert "📄 Select Pages" in final["text"]
        assert "Available pages: 2" in final["text"]
        assert "Insan" in final["text"]
        assert "Oppo" in final["text"]
        assert "🔄 Refresh Pages" not in labels
        assert "🧪 Check Account" not in labels
        assert "pg:refresh" not in callbacks
        assert stage_controls and stage_controls[-1][0] == "page_select"

    asyncio.run(run())


def test_post_to_pages_stops_before_page_discovery_when_cookie_is_invalid(monkeypatch):
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.storage = AutoPrepareStorage()
        app.dashboard_sessions = {}
        app.account_owner_scope = lambda user_id: user_id
        app.active_account_id = lambda user_id: asyncio.sleep(0, result="acct_1")
        app.maybe_quarantine_account_cookie = lambda *args, **kwargs: asyncio.sleep(0, result=False)

        async def user_language(user_id=0):
            return "en"

        async def dashboard_reply_markup(user_id=0):
            return {}

        async def validate_facebook_session(cookies):
            return False, "Session expired"

        async def discover_pages(account_id, owner_id=None):
            raise AssertionError("invalid cookies should not discover pages")

        sent = []
        edits = []

        async def send_message(chat_id, text, reply_to_message_id=0, *, reply_markup=None, parse_mode=""):
            sent.append(text)
            return {"ok": True, "result": {"message_id": 777}}

        async def edit_or_send_message(chat_id, message_id, text, **kwargs):
            edits.append(text)
            return message_id or 777

        import playwright_engine

        monkeypatch.setattr(playwright_engine, "validate_facebook_session", validate_facebook_session)
        app.user_language = user_language
        app.dashboard_reply_markup = dashboard_reply_markup
        app.discover_pages = discover_pages
        app.send_message = send_message
        app.edit_or_send_message = edit_or_send_message

        await app.handle_dashboard_button(123, 99, 456, "post_active")

        assert app.storage.validation_updates == [("acct_1", "invalid", "Session expired", 99)]
        assert app.storage.upserted == []
        assert "Cookie check failed" in edits[-1]
        assert "Session expired" in edits[-1]

    asyncio.run(run())


def test_post_to_pages_times_out_slow_cookie_validation_before_page_discovery(monkeypatch):
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.storage = AutoPrepareStorage()
        app.dashboard_sessions = {}
        app.account_owner_scope = lambda user_id: user_id
        app.active_account_id = lambda user_id: asyncio.sleep(0, result="acct_1")
        app.maybe_quarantine_account_cookie = lambda *args, **kwargs: asyncio.sleep(0, result=False)

        async def user_language(user_id=0):
            return "en"

        async def dashboard_reply_markup(user_id=0):
            return {}

        async def validate_facebook_session(cookies):
            await asyncio.sleep(2)
            return True, "Facebook session is valid"

        async def discover_pages(account_id, owner_id=None):
            raise AssertionError("timed-out cookie checks should not discover pages")

        sent = []
        edits = []

        async def send_message(chat_id, text, reply_to_message_id=0, *, reply_markup=None, parse_mode=""):
            sent.append(text)
            return {"ok": True, "result": {"message_id": 777}}

        async def edit_or_send_message(chat_id, message_id, text, **kwargs):
            edits.append(text)
            return message_id or 777

        import playwright_engine

        monkeypatch.setattr(playwright_engine, "validate_facebook_session", validate_facebook_session)
        monkeypatch.setattr(
            telegram_bot,
            "_env_float",
            lambda name, default, minimum=0.0: 0.6
            if name == "BOT_COOKIE_VALIDATION_TIMEOUT_SECONDS"
            else max(float(default), float(minimum)),
        )
        app.user_language = user_language
        app.dashboard_reply_markup = dashboard_reply_markup
        app.discover_pages = discover_pages
        app.send_message = send_message
        app.edit_or_send_message = edit_or_send_message

        await app.handle_dashboard_button(123, 99, 456, "post_active")

        assert app.storage.validation_updates == [
            ("acct_1", "invalid", "Cookie validation timed out after 1s. Stopped before refreshing pages.", 99)
        ]
        assert app.storage.upserted == []
        assert "Cookie check failed" in edits[-1]
        assert "Cookie validation timed out after 1s" in edits[-1]

    asyncio.run(run())
