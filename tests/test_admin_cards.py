import asyncio
import sys
import types
from datetime import datetime, timezone


if "aiohttp" not in sys.modules:
    aiohttp_stub = types.ModuleType("aiohttp")
    aiohttp_stub.ClientSession = object
    aiohttp_stub.web = types.SimpleNamespace()
    sys.modules["aiohttp"] = aiohttp_stub

from telegram_bot import TelegramBotApp


class AdminStorage:
    def __init__(self):
        self.touched = []
        self.broadcast_target_calls = []

    def admin_users(self):
        return [
            {
                "telegram_user_id": 373303307,
                "first_name": "Mohammed",
                "last_name": "Shabana",
                "username": "m_shabana",
                "active_account_id": "61576466101916",
                "account_count": 3,
                "job_count": 83,
                "last_seen": datetime(2026, 6, 7, 14, 15, tzinfo=timezone.utc),
            }
        ]

    def admin_user_page(self, limit=7, offset=0):
        rows = self.admin_users()
        return {"total": len(rows), "rows": rows[offset:offset + limit], "limit": limit, "offset": offset}

    def get_user_language(self, user_id):
        return "en"

    def touch_user(self, *args):
        self.touched.append(args)

    def admin_broadcast_targets(self, user_ids=None):
        self.broadcast_target_calls.append(user_ids)
        return [
            {
                "telegram_user_id": 111,
                "chat_id": 111,
                "first_name": "One",
                "last_name": "",
                "username": "",
            },
            {
                "telegram_user_id": 222,
                "chat_id": 222,
                "first_name": "Two",
                "last_name": "",
                "username": "",
            },
        ]


def test_admin_users_card_includes_profile_name_and_pm_time():
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.storage = AdminStorage()
        app.dashboard_sessions = {}
        sent = []

        async def send_message(chat_id, text, reply_to_message_id=0, *, reply_markup=None, parse_mode=""):
            sent.append({"text": text, "reply_markup": reply_markup})
            return {"ok": True, "result": {"message_id": 777}}

        app.send_message = send_message

        await app.show_admin_users(123, 456, 99)

        text = sent[0]["text"]
        assert "Mohammed Shabana" in text
        assert "@m_shabana" not in text
        assert "id=373303307" in text
        assert "last=05:15 PM" in text
        assert "UTC+3" not in text

    asyncio.run(run())


def test_touch_user_seen_stores_telegram_profile_fields():
    async def run():
        storage = AdminStorage()
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.storage = storage

        await app.touch_user_seen(
            373303307,
            123,
            {"first_name": "Mohammed", "last_name": "Shabana", "username": "m_shabana"},
        )

        assert storage.touched == [(373303307, 123, "Mohammed", "Shabana", "m_shabana")]

    asyncio.run(run())


def test_admin_user_detail_card_includes_accounts_pages_and_last_posting_status():
    app = TelegramBotApp.__new__(TelegramBotApp)
    detail = {
        "telegram_user_id": 373303307,
        "first_name": "Mohammed",
        "last_name": "Shabana",
        "username": "m_shabana",
        "lang": "en",
        "first_seen": datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        "last_seen": datetime(2026, 6, 7, 14, 15, tzinfo=timezone.utc),
        "account_count": 1,
        "page_count": 2,
        "job_count": 4,
        "job_status_counts": {"success": 3, "failed": 1},
        "last_job": {
            "status": "success",
            "post_type": "video",
            "page_name": "Insan",
            "created_at": datetime(2026, 6, 7, 14, 10, tzinfo=timezone.utc),
        },
        "accounts": [{"account_id": "acct_1", "label": "Omar Mohamed", "active": True}],
        "pages_by_account": {
            "acct_1": [
                {"page_name": "Insan", "page_id": "p1", "page_url": ""},
                {"page_name": "Oppo", "page_id": "p2", "page_url": ""},
            ]
        },
    }

    text = app.admin_user_detail_card(detail)

    assert "First used: 2026-06-01" in text
    assert "Last posting status:" in text
    assert "success video -> Insan" in text
    assert "Omar Mohamed" in text
    assert "Insan" in text
    assert "Oppo" in text


def test_admin_broadcast_sends_to_selected_targets_with_result_card():
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.storage = AdminStorage()
        app.dashboard_sessions = {"123:99": {"action": "admin_broadcast"}}
        sent = []
        edits = []

        async def send_message(chat_id, text, reply_to_message_id=0, *, reply_markup=None, parse_mode=""):
            sent.append({"chat_id": chat_id, "text": text, "reply_to_message_id": reply_to_message_id})
            return {"ok": True, "result": {"message_id": 700 + len(sent)}}

        async def edit_or_send_message(chat_id, message_id, text, *, reply_to_message_id=0, reply_markup=None, parse_mode=""):
            edits.append({"chat_id": chat_id, "message_id": message_id, "text": text})
            return message_id

        app.send_message = send_message
        app.edit_or_send_message = edit_or_send_message

        await app.send_admin_broadcast(
            123,
            99,
            456,
            {"lang": "en", "audience": "selected", "selected_user_ids": [111, 222]},
            "Admin notice",
        )

        assert app.storage.broadcast_target_calls == [[111, 222]]
        assert [item["chat_id"] for item in sent[1:]] == [111, 222]
        assert sent[1]["text"] == "Admin notice"
        assert "Delivered: 2/2" in edits[-1]["text"]
        assert app.dashboard_sessions == {}

    asyncio.run(run())


def test_admin_delete_confirmation_lists_selected_users():
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.storage = AdminStorage()
        app.dashboard_sessions = {}
        edited = []

        async def user_language(user_id):
            return "en"

        async def edit_or_send_message(chat_id, message_id, text, *, reply_to_message_id=0, reply_markup=None, parse_mode=""):
            edited.append({"text": text, "reply_markup": reply_markup})
            return message_id

        app.user_language = user_language
        app.edit_or_send_message = edit_or_send_message

        await app.handle_admin_callback(
            123,
            99,
            456,
            "adm:del:confirm",
            {"selected_user_ids": [111, 222], "page": 0},
        )

        text = edited[0]["text"]
        assert "Selected users:" in text
        assert "One | id=111" in text
        assert "Two | id=222" in text
        assert "Delete Now" in str(edited[0]["reply_markup"])

    asyncio.run(run())


def test_admin_reply_button_bypasses_existing_admin_card_session():
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.storage = AdminStorage()
        app.admin_ids = {99}
        app.dashboard_sessions = {"123:99": {"action": "admin_dashboard", "step": "users", "lang": "en"}}
        app.update_locks = {}
        called = []

        def start_background_task(coro, label):
            coro.close()
            return None

        async def handle_dashboard_session(*args, **kwargs):
            raise AssertionError("admin reply button should route as a dashboard action")

        async def show_admin_delete_users(chat_id, message_id, user_id, page=0, *, edit=False):
            called.append((chat_id, message_id, user_id, page, edit))

        app.start_background_task = start_background_task
        app.handle_dashboard_session = handle_dashboard_session
        app.show_admin_delete_users = show_admin_delete_users

        await app.handle_update(
            {
                "message": {
                    "message_id": 456,
                    "text": "🗑 Delete Users",
                    "chat": {"id": 123},
                    "from": {"id": 99},
                }
            }
        )

        assert called == [(123, 456, 99, 0, False)]

    asyncio.run(run())
