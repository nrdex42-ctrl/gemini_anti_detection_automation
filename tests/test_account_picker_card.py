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

        app.storage = Storage()
        app.user_language = user_language
        app.show_dashboard = show_dashboard
        app.account_owner_scope = lambda user_id: user_id

        await app.handle_account_selected(123, 99, 456, {"action": "switch_account", "lang": "en"}, "acct_1", edit=True)

        assert selected == [(99, "acct_1")]
        assert dashboards[0]["message_id"] == 456
        assert "Active account switched to Omar Mohamed." in dashboards[0]["prefix"]
        assert "123:99" not in app.dashboard_sessions

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
