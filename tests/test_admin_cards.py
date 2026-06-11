import asyncio
import logging
import sys
import types
from datetime import datetime, timezone


if "aiohttp" not in sys.modules:
    aiohttp_stub = types.ModuleType("aiohttp")
    aiohttp_stub.ClientSession = object
    aiohttp_stub.web = types.SimpleNamespace()
    sys.modules["aiohttp"] = aiohttp_stub

from telegram_bot import TelegramBotApp


def test_admin_user_dashboard_uses_only_admin_owned_accounts():
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.admin_ids = {99}

        class Storage:
            def __init__(self):
                self.list_scopes = []
                self.summary_scopes = []
                self.active_scopes = []

            def list_accounts(self, owner_scope=None):
                self.list_scopes.append(owner_scope)
                return [{"account_id": "admin_acct", "label": "Admin Account", "active": True}]

            def dashboard_summary(self, owner_scope=None):
                self.summary_scopes.append(owner_scope)
                return {"job_status_counts": {}, "page_count": 1}

            def get_active_account(self, user_id, owner_scope=None):
                self.active_scopes.append((user_id, owner_scope))
                return "admin_acct"

        storage = Storage()
        app.storage = storage

        accounts, summary, active = await app.dashboard_state(99)

        assert app.is_admin_user(99)
        assert app.account_owner_scope(99) == 99
        assert accounts[0]["account_id"] == "admin_acct"
        assert summary["page_count"] == 1
        assert active == "admin_acct"
        assert storage.list_scopes == [99]
        assert storage.summary_scopes == [99]
        assert storage.active_scopes == [(99, 99)]

    asyncio.run(run())


def test_admin_dashboard_fallback_keeps_admin_button():
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.admin_ids = {99}
        captured = []

        async def dashboard_state(user_id=0):
            raise RuntimeError("database unavailable")

        async def edit_or_send_message(chat_id, message_id, text, **kwargs):
            captured.append(kwargs.get("reply_markup"))
            return message_id or 777

        app.dashboard_state = dashboard_state
        app.edit_or_send_message = edit_or_send_message

        await app.show_dashboard(123, 456, user_id=99)

        labels = [button for row in captured[0]["keyboard"] for button in row]
        assert "🔒 Admin Dashboard" in labels

    asyncio.run(run())


def test_admin_no_accounts_reply_keyboard_keeps_admin_button():
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.admin_ids = {99}
        sent = []

        class Storage:
            def list_accounts(self, owner_scope=None):
                return []

        async def user_language(user_id=0):
            return "en"

        async def send_message(chat_id, text, reply_to_message_id=0, *, reply_markup=None, parse_mode=""):
            sent.append(reply_markup)
            return {"ok": True, "result": {"message_id": 777}}

        app.storage = Storage()
        app.user_language = user_language
        app.send_message = send_message
        app.account_owner_scope = lambda user_id: user_id

        await app.command_accounts(123, 456, 99)

        labels = [button for row in sent[0]["keyboard"] for button in row]
        assert "🔒 Admin Dashboard" in labels

    asyncio.run(run())


class AdminStorage:
    def __init__(self):
        self.touched = []
        self.broadcast_target_calls = []
        self.meta = {}
        self.restart_targets = []

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

    def get_meta(self, key):
        return self.meta.get(key)

    def set_meta(self, key, value):
        self.meta[key] = value

    def list_restart_targets(self):
        return list(self.restart_targets)

    def dashboard_summary(self, owner_scope=None):
        return {"job_status_counts": {}, "total_accounts": 0, "page_count": 0}

    def list_accounts(self, owner_scope=None):
        return []

    def get_active_account(self, user_id, owner_scope=None):
        return None


def test_admin_users_card_includes_profile_name_and_date_time():
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
        assert "last=2026-06-07 05:15 PM" in text
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

    assert "First used: 2026-06-01 12:00 PM" in text
    assert "Last seen: 2026-06-07 05:15 PM" in text
    assert "UTC+3" not in text
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


def test_admin_posting_mode_card_updates_persistent_setting():
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.storage = AdminStorage()
        app.admin_ids = {99}
        app.dashboard_sessions = {}
        edits = []

        async def edit_message(chat_id, message_id, text, *, reply_markup=None, parse_mode=""):
            edits.append({"chat_id": chat_id, "message_id": message_id, "text": text, "reply_markup": reply_markup})
            return message_id

        app.edit_message = edit_message

        await app.show_admin_posting_mode(123, 456, 99, edit=True)

        assert "Current: Sequential" in edits[-1]["text"]
        labels = [button["text"] for row in edits[-1]["reply_markup"]["inline_keyboard"] for button in row]
        assert "✅ Sequential" in labels
        assert "⬜ Parallel" in labels

        await app.handle_admin_callback(123, 99, 456, "adm:posting_mode:parallel", {})

        assert app.storage.meta["posting_mode"] == "parallel"
        assert "Posting mode updated to Parallel." in edits[-1]["text"]
        labels = [button["text"] for row in edits[-1]["reply_markup"]["inline_keyboard"] for button in row]
        assert "⬜ Sequential" in labels
        assert "✅ Parallel" in labels

    asyncio.run(run())


def test_admin_proxy_card_updates_global_proxy_and_tests_status():
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.storage = AdminStorage()
        app.admin_ids = {99}
        app.dashboard_sessions = {}
        sent = []
        edits = []
        health_calls = []

        async def user_language(user_id=0):
            return "en"

        async def send_message(chat_id, text, reply_to_message_id=0, *, reply_markup=None, parse_mode=""):
            sent.append({"text": text, "reply_markup": reply_markup})
            return {"ok": True, "result": {"message_id": 777}}

        async def edit_or_send_message(chat_id, message_id=0, text="", *, reply_markup=None, **kwargs):
            edits.append({"text": text, "reply_markup": reply_markup})
            return message_id or 777

        async def global_proxy_health_line(proxy_url="", *, force=False):
            health_calls.append((proxy_url, force))
            return "Proxy status: working/reachable | Location: Test City | IP: 203.0.113.10"

        app.user_language = user_language
        app.send_message = send_message
        app.edit_or_send_message = edit_or_send_message
        app.global_proxy_health_line = global_proxy_health_line

        await app.show_admin_proxy(123, 456, 99)

        assert "🌐 Global Proxy" in sent[-1]["text"]
        assert "Status: none" in sent[-1]["text"]
        callbacks = [
            button["callback_data"]
            for row in sent[-1]["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        assert callbacks == ["adm:proxy:set", "adm:dash"]

        app.set_dashboard_session(123, 99, {"action": "admin_proxy", "step": "proxy_input", "lang": "en"})
        handled = await app.handle_dashboard_session(
            123,
            99,
            456,
            "http://user:pass@proxy.example.com:8080",
            {},
        )

        assert handled is True
        assert app.storage.meta["global_proxy_ciphertext"] == "http://user:pass@proxy.example.com:8080"
        assert health_calls == [("http://user:pass@proxy.example.com:8080", True)]
        assert "Global proxy updated: http://proxy.example.com:8080" in sent[-1]["text"]
        assert "Current: http://proxy.example.com:8080" in sent[-1]["text"]
        assert "Scope: all users and accounts" in sent[-1]["text"]
        assert "Proxy status: working/reachable" in sent[-1]["text"]
        callbacks = [
            button["callback_data"]
            for row in sent[-1]["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        assert "adm:proxy:set" in callbacks
        assert "adm:proxy:test" in callbacks
        assert "adm:proxy:clear" in callbacks

        await app.handle_admin_callback(123, 99, 456, "adm:proxy:test", {})

        assert health_calls[-1] == ("http://user:pass@proxy.example.com:8080", True)
        assert "Global proxy test complete." in edits[-1]["text"]
        assert "Checked:" in edits[-1]["text"]
        assert "Proxy status: working/reachable" in edits[-1]["text"]

    asyncio.run(run())


def test_old_global_proxy_callback_routes_to_admin_proxy_card():
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.storage = AdminStorage()
        app.storage.meta["global_proxy_ciphertext"] = "http://user:pass@proxy.example.com:8080"
        app.admin_ids = {99}
        app.dashboard_sessions = {}
        edits = []

        async def user_language(user_id=0):
            return "en"

        async def edit_or_send_message(chat_id, message_id=0, text="", *, reply_markup=None, **kwargs):
            edits.append({"text": text, "reply_markup": reply_markup})
            return message_id or 777

        async def answer_callback_query(callback_query_id, text=""):
            return None

        async def global_proxy_health_line(proxy_url="", *, force=False):
            assert proxy_url == "http://user:pass@proxy.example.com:8080"
            assert force is True
            return "Proxy status: working/reachable | Location: Test City | IP: 203.0.113.10"

        def start_background_task(coro, *args, **kwargs):
            coro.close()
            return None

        app.user_language = user_language
        app.edit_or_send_message = edit_or_send_message
        app.answer_callback_query = answer_callback_query
        app.global_proxy_health_line = global_proxy_health_line
        app.start_background_task = start_background_task

        await app.handle_callback_query(
            {
                "callback_query": {
                    "id": "cb_1",
                    "data": "globalproxytest",
                    "message": {"chat": {"id": 123}, "message_id": 456},
                    "from": {"id": 99},
                }
            }
        )

        assert edits
        assert "🌐 Global Proxy" in edits[-1]["text"]
        assert "Global proxy test complete." in edits[-1]["text"]
        assert "Checked:" in edits[-1]["text"]
        callbacks = [
            button["callback_data"]
            for row in edits[-1]["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        assert "adm:proxy:test" in callbacks
        assert "globalproxytest" not in callbacks

    asyncio.run(run())


def test_restart_dashboard_broadcast_keeps_revision_unmarked_when_any_send_fails(caplog):
    async def run():
        storage = AdminStorage()
        storage.restart_targets = [
            {"telegram_user_id": 111, "chat_id": 111},
            {"telegram_user_id": 222, "chat_id": 222},
        ]
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.storage = storage
        app.admin_ids = set()
        app.deploy_revision = lambda: "test:rev"
        app.account_owner_scope = lambda user_id: str(user_id)
        app.is_admin_user = lambda user_id: False
        sent = []

        async def send_message(chat_id, text, reply_to_message_id=0, *, reply_markup=None, parse_mode=""):
            sent.append(chat_id)
            return {"ok": chat_id == 111}

        app.send_message = send_message

        caplog.set_level(logging.INFO, logger="telegram_bot")
        await app.notify_restart_dashboard()

        assert sent == [111, 222]
        assert "last_restart_broadcast_revision" not in storage.meta
        assert "Restart dashboard broadcast sent to 1 user(s), failed=1" in caplog.text
        assert "Restart dashboard broadcast not marked sent for test:rev because 1 target(s) failed" in caplog.text

    asyncio.run(run())


def test_restart_dashboard_broadcast_marks_revision_after_all_sends_succeed():
    async def run():
        storage = AdminStorage()
        storage.restart_targets = [
            {"telegram_user_id": 111, "chat_id": 111},
            {"telegram_user_id": 222, "chat_id": 222},
        ]
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.storage = storage
        app.admin_ids = set()
        app.deploy_revision = lambda: "test:rev"
        app.account_owner_scope = lambda user_id: str(user_id)
        app.is_admin_user = lambda user_id: False
        sent = []

        async def send_message(chat_id, text, reply_to_message_id=0, *, reply_markup=None, parse_mode=""):
            sent.append(chat_id)
            return {"ok": True}

        app.send_message = send_message

        await app.notify_restart_dashboard()

        assert sent == [111, 222]
        assert storage.meta["last_restart_broadcast_revision"] == "test:rev"

    asyncio.run(run())


def test_startup_sends_restart_dashboard_before_cookie_validation(monkeypatch):
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        calls = []

        async def notify_restart_dashboard():
            calls.append("notify")

        async def validate_all_accounts_cookies_startup():
            calls.append("validate")

        app.notify_restart_dashboard = notify_restart_dashboard
        app.validate_all_accounts_cookies_startup = validate_all_accounts_cookies_startup
        monkeypatch.setenv("RESTART_BROADCAST_ENABLED", "true")

        await app.startup_validation_and_broadcast()

        assert calls == ["notify", "validate"]

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


def test_pending_user_is_blocked_and_admin_is_notified():
    async def run():
        class ApprovalStorage:
            def __init__(self):
                self.pending = []

            def get_user_approval_status(self, user_id):
                return ""

            def upsert_pending_user(self, user_id, chat_id, first_name="", last_name="", username=""):
                self.pending.append((user_id, chat_id, first_name, last_name, username))
                return {
                    "telegram_user_id": user_id,
                    "last_chat_id": chat_id,
                    "first_name": first_name,
                    "last_name": last_name,
                    "username": username,
                    "approval_status": "pending",
                    "request_created": True,
                }

        app = TelegramBotApp.__new__(TelegramBotApp)
        app.storage = ApprovalStorage()
        app.admin_ids = {99}
        app.require_user_approval = True
        app.dashboard_sessions = {}
        sent = []

        async def send_message(chat_id, text, reply_to_message_id=0, *, reply_markup=None, parse_mode=""):
            sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
            return {"ok": True, "result": {"message_id": 700 + len(sent)}}

        async def show_dashboard(*args, **kwargs):
            raise AssertionError("pending user should not reach the dashboard")

        app.send_message = send_message
        app.show_dashboard = show_dashboard

        await app.handle_update(
            {
                "message": {
                    "message_id": 456,
                    "text": "/start",
                    "chat": {"id": 111},
                    "from": {"id": 111, "first_name": "Ali", "last_name": "Hassan", "username": "ali_h"},
                }
            }
        )

        assert app.storage.pending == [(111, 111, "Ali", "Hassan", "ali_h")]
        assert sent[0]["chat_id"] == 99
        assert "New User Approval Request" in sent[0]["text"]
        assert "adm:approve:111:0" in str(sent[0]["reply_markup"])
        assert sent[1]["chat_id"] == 111
        assert "pending" in sent[1]["text"].lower()

    asyncio.run(run())


def test_approved_user_reaches_dashboard_when_approval_gate_is_enabled():
    async def run():
        class ApprovalStorage:
            def __init__(self):
                self.touched = []

            def get_user_approval_status(self, user_id):
                return "approved"

            def touch_user(self, *args):
                self.touched.append(args)

        app = TelegramBotApp.__new__(TelegramBotApp)
        app.storage = ApprovalStorage()
        app.admin_ids = {99}
        app.require_user_approval = True
        app.dashboard_sessions = {}
        called = []

        def start_background_task(coro, label):
            coro.close()
            return None

        async def show_dashboard(chat_id, message_id=0, *, prefix="", user_id=0):
            called.append((chat_id, message_id, user_id))

        app.start_background_task = start_background_task
        app.show_dashboard = show_dashboard

        await app.handle_update(
            {
                "message": {
                    "message_id": 456,
                    "text": "/start",
                    "chat": {"id": 111},
                    "from": {"id": 111, "first_name": "Ali"},
                }
            }
        )

        assert called == [(111, 0, 111)]

    asyncio.run(run())
