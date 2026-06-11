import asyncio
import time

from telegram_bot import TelegramBotApp


def test_account_picker_card_has_activate_rename_and_delete_buttons():
    app = TelegramBotApp.__new__(TelegramBotApp)
    accounts = [
        {"account_id": "acct_1", "label": "Omar Mohamed", "cookie_status": "valid"},
        {"account_id": "acct_2", "label": "اسماء ضياء", "cookie_status": "invalid"},
    ]

    text = app.account_picker_card("Select the account to make active.", accounts, "acct_1", lang="en")
    markup = app.account_picker_markup(accounts, "acct_1", lang="en")

    assert "Select the account to make active." in text
    assert "Omar Mohamed" in text
    assert "اسماء ضياء" in text
    assert "keyboard" not in markup
    callbacks = [button["callback_data"] for row in markup["inline_keyboard"] for button in row]
    assert "acctsel:0" in callbacks
    assert "acctren:0" in callbacks
    assert "acctdel:0" in callbacks
    assert "acctproxy:0" in callbacks


def test_manage_accounts_dashboard_button_sends_inline_card_without_editing_user_message():
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.dashboard_sessions = {}
        sent = []

        class Storage:
            def list_accounts(self, owner_id=None):
                return [{"account_id": "acct_1", "label": "Omar Mohamed", "cookie_status": "valid"}]

        async def user_language(user_id=0):
            return "en"

        async def active_account_id(user_id):
            return "acct_1"

        async def send_message(chat_id, text, reply_to_message_id=0, *, reply_markup=None, parse_mode=""):
            sent.append({"text": text, "reply_markup": reply_markup, "reply_to_message_id": reply_to_message_id})
            return {"ok": True, "result": {"message_id": 777}}

        async def edit_or_send_message(*args, **kwargs):
            raise AssertionError("reply-keyboard account action should send a fresh inline card")

        app.storage = Storage()
        app.user_language = user_language
        app.active_account_id = active_account_id
        app.send_message = send_message
        app.edit_or_send_message = edit_or_send_message
        app.account_owner_scope = lambda user_id: user_id
        app.schedule_account_name_refresh = lambda *args, **kwargs: None

        await app.handle_dashboard_button(123, 99, 456, "manage_accounts")

        assert sent
        assert "Select the account to make active." in sent[0]["text"]
        assert "inline_keyboard" in sent[0]["reply_markup"]
        assert "keyboard" not in sent[0]["reply_markup"]

    asyncio.run(run())


def test_account_picker_card_shows_clear_proxy_for_configured_accounts():
    app = TelegramBotApp.__new__(TelegramBotApp)
    accounts = [
        {"account_id": "acct_1", "label": "Omar Mohamed", "cookie_status": "valid", "proxy_configured": True},
    ]

    text = app.account_picker_card("Select account.", accounts, "acct_1", lang="en")
    markup = app.account_picker_markup(accounts, "acct_1", lang="en")
    callbacks = [button["callback_data"] for row in markup["inline_keyboard"] for button in row]

    assert "proxy set" in text
    assert "acctproxy:0" in callbacks
    assert "acctproxyclear:0" in callbacks


def test_account_proxy_input_updates_storage_and_refreshes_accounts_card():
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.dashboard_sessions = {
            "123:99": {
                "action": "manage_accounts",
                "step": "proxy_input",
                "proxy_account_id": "acct_1",
                "lang": "en",
                "updated_at": time.time(),
            }
        }
        calls = []
        account_cards = []

        class Storage:
            def update_account_proxy(self, account_id, proxy_url, owner_id=None):
                calls.append(("update_account_proxy", account_id, proxy_url, owner_id))
                return True

            def get_account(self, account_id, owner_id=None):
                return {"account_id": account_id, "label": "Omar Mohamed", "proxy_configured": True}

            def list_accounts(self, owner_id=None):
                return [{"account_id": "acct_1", "label": "Omar Mohamed", "proxy_configured": True}]

            def dashboard_summary(self, owner_id=None):
                return {"job_status_counts": {}, "page_counts_by_account": {}}

            def get_active_account(self, user_id, owner_id=None):
                return "acct_1"

        async def user_language(user_id=0):
            return "en"

        async def send_message(chat_id, text, reply_to_message_id=0, *, reply_markup=None, parse_mode=""):
            account_cards.append(text)
            return {"ok": True, "result": {"message_id": 777}}

        app.storage = Storage()
        app.user_language = user_language
        app.send_message = send_message
        app.account_owner_scope = lambda user_id: user_id
        app.schedule_account_name_refresh = lambda *args, **kwargs: None

        handled = await app.handle_dashboard_session(
            123,
            99,
            456,
            "http://user:pass@proxy.example.com:8080",
            {},
        )

        assert handled is True
        assert calls == [
            ("update_account_proxy", "acct_1", "http://user:pass@proxy.example.com:8080", 99)
        ]
        assert account_cards
        assert "Proxy updated for Omar Mohamed: http://proxy.example.com:8080" in account_cards[-1]
        assert "123:99" in app.dashboard_sessions

    asyncio.run(run())


def test_account_selection_callback_sets_active_account_and_returns_dashboard():
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.dashboard_sessions = {"123:99": {"action": "switch_account", "step": "account"}}
        selected = []
        dashboards = []

        class Storage:
            def account_exists(self, account_id, active_only=True, owner_id=None):
                return account_id == "acct_1"

            def set_active_account(self, user_id, account_id):
                selected.append((user_id, account_id))

            def get_account(self, account_id, owner_id=None):
                return {"account_id": account_id, "label": "Omar Mohamed"}

        async def user_language(user_id=0):
            return "en"

        async def show_dashboard(chat_id, message_id=0, prefix="", user_id=0, edit_message_id=0):
            dashboards.append({"chat_id": chat_id, "message_id": message_id, "prefix": prefix, "user_id": user_id})
            return 888

        auto_refreshes = []

        async def auto_check_and_refresh_pages(account_id, user_id, lang, progress_callback=None):
            auto_refreshes.append((account_id, user_id, lang))
            if progress_callback:
                await progress_callback(1, "Finding available pages...")
                await progress_callback(2, "Saving 2 available page(s)...")
            return {"ok": True, "pages": [{"id": "p1"}, {"id": "p2"}]}

        async def edit_or_send_message(chat_id, message_id, text, **kwargs):
            return message_id or 777

        app.storage = Storage()
        app.user_language = user_language
        app.show_dashboard = show_dashboard
        app.auto_check_and_refresh_pages = auto_check_and_refresh_pages
        app.edit_or_send_message = edit_or_send_message
        app.account_owner_scope = lambda user_id: user_id

        await app.handle_account_selected(123, 99, 456, {"action": "switch_account", "lang": "en"}, "acct_1", edit=True)

        assert selected == [(99, "acct_1")]
        assert auto_refreshes == [("acct_1", 99, "en")]
        assert dashboards[0]["message_id"] == 456
        assert "Active account switched to Omar Mohamed. Refreshed 2 available page(s)." in dashboards[0]["prefix"]
        assert "123:99" not in app.dashboard_sessions

    asyncio.run(run())


def test_account_selection_auto_checks_cookie_refreshes_pages_and_updates_dashboard(monkeypatch):
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.dashboard_sessions = {"123:99": {"action": "manage_accounts", "step": "account", "lang": "en"}}
        selected = []
        dashboards = []
        progress_texts = []

        class Storage:
            def __init__(self):
                self.validation_updates = []
                self.upserted = []

            def account_exists(self, account_id, active_only=True, owner_id=None):
                return account_id == "acct_1" and owner_id == 99

            def set_active_account(self, user_id, account_id):
                selected.append((user_id, account_id))

            def get_account(self, account_id, owner_id=None):
                return {"account_id": account_id, "label": "Omar Mohamed"}

            def get_account_cookie(self, account_id, owner_id=None):
                assert (account_id, owner_id) == ("acct_1", 99)
                return "c_user=100; xs=session"

            def update_account_cookie_validation(self, account_id, status, detail, owner_id=None):
                self.validation_updates.append((account_id, status, detail, owner_id))

            def upsert_pages(self, account_id, pages):
                self.upserted.append((account_id, pages))

        async def user_language(user_id=0):
            return "en"

        async def validate_facebook_session(cookies):
            return True, "Facebook session is valid"

        async def discover_pages(account_id, owner_id=None):
            assert (account_id, owner_id) == ("acct_1", 99)
            return [
                {"id": "p1", "name": "Insan", "url": "https://facebook.com/insan"},
                {"id": "p2", "name": "Oppo", "url": "https://facebook.com/oppo"},
            ]

        async def edit_or_send_message(chat_id, message_id, text, **kwargs):
            progress_texts.append(text)
            return message_id or 777

        async def show_dashboard(chat_id, message_id=0, prefix="", user_id=0, edit_message_id=0):
            dashboards.append({"chat_id": chat_id, "message_id": message_id, "prefix": prefix, "user_id": user_id})
            return 888

        import playwright_engine

        monkeypatch.setattr(playwright_engine, "validate_facebook_session", validate_facebook_session)
        app.storage = Storage()
        app.user_language = user_language
        app.discover_pages = discover_pages
        app.edit_or_send_message = edit_or_send_message
        app.show_dashboard = show_dashboard
        app.account_owner_scope = lambda user_id: user_id

        await app.handle_account_selected(123, 99, 456, {"action": "manage_accounts", "lang": "en"}, "acct_1", edit=True)

        assert selected == [(99, "acct_1")]
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
        assert any("Finding available pages..." in text for text in progress_texts)
        assert any("Saving 2 available page(s)..." in text for text in progress_texts)
        assert dashboards == [
            {
                "chat_id": 123,
                "message_id": 456,
                "prefix": "Active account switched to Omar Mohamed. Refreshed 2 available page(s).",
                "user_id": 99,
            }
        ]
        assert "123:99" not in app.dashboard_sessions

    asyncio.run(run())


def test_account_selection_cookie_failure_uses_short_dashboard_prefix():
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.dashboard_sessions = {"123:99": {"action": "switch_account", "step": "account", "lang": "en"}}
        selected = []
        dashboards = []

        class Storage:
            def account_exists(self, account_id, active_only=True, owner_id=None):
                return account_id == "acct_1"

            def set_active_account(self, user_id, account_id):
                selected.append((user_id, account_id))

            def get_account(self, account_id, owner_id=None):
                return {"account_id": account_id, "label": "Omar Mohamed"}

        async def user_language(user_id=0):
            return "en"

        async def auto_check_and_refresh_pages(account_id, user_id, lang, progress_callback=None):
            return {
                "ok": False,
                "stage": "cookie",
                "detail": (
                    "Session check inconclusive; browser validation skipped to protect cookie session. "
                    "HTTP detail: Facebook returned homepage without auth failure markers"
                ),
            }

        async def edit_or_send_message(chat_id, message_id, text, **kwargs):
            return message_id or 777

        async def show_dashboard(chat_id, message_id=0, prefix="", user_id=0, edit_message_id=0):
            dashboards.append({"prefix": prefix, "message_id": message_id})
            return 888

        app.storage = Storage()
        app.user_language = user_language
        app.auto_check_and_refresh_pages = auto_check_and_refresh_pages
        app.edit_or_send_message = edit_or_send_message
        app.show_dashboard = show_dashboard
        app.account_owner_scope = lambda user_id: user_id

        await app.handle_account_selected(123, 99, 456, {"action": "switch_account", "lang": "en"}, "acct_1", edit=True)

        assert selected == [(99, "acct_1")]
        assert dashboards == [{"prefix": "Active account switched to Omar Mohamed. Invalid cookie.", "message_id": 456}]
        assert "Session check inconclusive" not in dashboards[0]["prefix"]
        assert "HTTP detail" not in dashboards[0]["prefix"]

    asyncio.run(run())


def test_account_delete_confirmation_deactivates_account_and_clears_active_account():
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.dashboard_sessions = {
            "123:99": {
                "action": "manage_accounts",
                "step": "delete_confirm",
                "lang": "en",
                "delete_account_id": "acct_1",
                "updated_at": time.time(),
            }
        }
        calls = []
        dashboards = []

        class Storage:
            def deactivate_account(self, account_id, owner_id=None):
                calls.append(("deactivate_account", account_id, owner_id))
                return True

            def clear_active_account(self, user_id, account_id=""):
                calls.append(("clear_active_account", user_id, account_id))

        def start_background_task(coro, label):
            coro.close()
            return None

        async def user_language(user_id=0):
            return "en"

        async def show_dashboard(chat_id, message_id=0, prefix="", user_id=0, edit_message_id=0):
            dashboards.append({"chat_id": chat_id, "message_id": message_id, "prefix": prefix, "user_id": user_id})
            return 888

        app.storage = Storage()
        app.start_background_task = start_background_task
        app.user_language = user_language
        app.show_dashboard = show_dashboard
        app.account_owner_scope = lambda user_id: user_id

        await app.handle_callback_query(
            {
                "callback_query": {
                    "data": "acctdel:confirm",
                    "message": {"message_id": 456, "chat": {"id": 123}},
                    "from": {"id": 99},
                }
            }
        )

        assert calls == [
            ("deactivate_account", "acct_1", 99),
            ("clear_active_account", 99, "acct_1"),
        ]
        assert dashboards[0]["prefix"] == "Account deleted and related stored pages removed."
        assert "123:99" not in app.dashboard_sessions

    asyncio.run(run())
