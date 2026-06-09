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


class AccountStorage:
    def __init__(self):
        self.upserted = []
        self.active = []
        self.validation = []
        self.pages = []

    def upsert_account(self, account_id, cookie_header, label, user_id):
        self.upserted.append((account_id, cookie_header, label, user_id))

    def set_active_account(self, user_id, account_id):
        self.active.append((user_id, account_id))

    def update_account_cookie_validation(self, account_id, status, detail, owner_scope):
        self.validation.append((account_id, status, detail, owner_scope))

    def upsert_pages(self, account_id, pages):
        self.pages.append((account_id, pages))

    def list_pages(self, account_id, owner_scope=None):
        for stored_account_id, pages in reversed(self.pages):
            if stored_account_id == account_id:
                return pages
        return []


def test_account_add_success_sends_fresh_dashboard_to_replace_cookie_keyboard(monkeypatch):
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.storage = AccountStorage()
        app.debug_event = lambda *args, **kwargs: None
        app.account_owner_scope = lambda user_id: user_id
        app.is_admin_user = lambda user_id: False
        app.schedule_account_name_refresh = lambda *args, **kwargs: None

        async def user_language(user_id=0):
            return "en"

        async def resolve_account_label(cookie_header, fallback_label="Facebook Account"):
            return "Omar Mohamed", "Auto-detected", "تم الكشف تلقائيًا", True

        async def dashboard_state(user_id=0):
            account = {
                "account_id": "61576466101916",
                "label": "Omar Mohamed",
                "active": True,
                "page_count": 2,
                "cookie_status": "valid",
            }
            return [account], {"job_status_counts": {}}, "61576466101916"

        sent_messages = []
        dashboard_message_ids = []
        edited_texts = []
        deleted_messages = []

        async def send_message(chat_id, text, reply_to_message_id=0, *, reply_markup=None, parse_mode=""):
            sent_messages.append((chat_id, text, reply_to_message_id, reply_markup, parse_mode))
            return {"ok": True, "result": {"message_id": 555}}

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
            dashboard_message_ids.append(message_id)
            edited_texts.append(text)
            return message_id or 777

        async def delete_message(chat_id, message_id):
            deleted_messages.append((chat_id, message_id))

        parsed = types.SimpleNamespace(
            account_id="61576466101916",
            cookie_header="c_user=61576466101916; xs=session",
        )

        async def validate_facebook_session(cookies):
            return True, "Facebook session is valid"

        async def discover_pages(account_id, owner_id=None):
            return [{"page_name": "Huawei"}, {"page_name": "Oppo"}]

        monkeypatch.setattr(telegram_bot, "parse_account_cookie_payload", lambda payload, hint="auto": parsed)
        import playwright_engine

        monkeypatch.setattr(playwright_engine, "validate_facebook_session", validate_facebook_session)

        app.user_language = user_language
        app.resolve_account_label_from_cookie_header = resolve_account_label
        app.dashboard_state = dashboard_state
        app.discover_pages = discover_pages
        app.send_message = send_message
        app.edit_or_send_message = edit_or_send_message
        app.delete_message = delete_message

        ok = await app.save_account_cookie_payload(
            123,
            99,
            456,
            "raw cookie payload",
            account_hint="auto",
            cookie_message_ids=[456],
        )

        assert ok is True
        assert deleted_messages == [(123, 456), (123, 555)]
        assert dashboard_message_ids[-1] == 0
        assert app.storage.upserted == [
            ("61576466101916", "c_user=61576466101916; xs=session", "Omar Mohamed", 99)
        ]
        assert app.storage.pages == [
            ("61576466101916", [{"page_name": "Huawei"}, {"page_name": "Oppo"}])
        ]
        final_text = edited_texts[-1]
        assert "📄 Available pages: 2 (cached)" in final_text
        assert "- Huawei" in final_text
        assert "- Oppo" in final_text

    asyncio.run(run())
