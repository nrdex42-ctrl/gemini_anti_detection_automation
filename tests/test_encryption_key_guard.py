import asyncio
import sys
import types


if "aiohttp" not in sys.modules:
    aiohttp_stub = types.ModuleType("aiohttp")
    aiohttp_stub.ClientSession = object
    aiohttp_stub.web = types.SimpleNamespace()
    sys.modules["aiohttp"] = aiohttp_stub

from telegram_bot import TelegramBotApp


class BrokenCookieStorage:
    def __init__(self):
        self.validation_updates = []
        self.created_jobs = False

    def get_account_cookie(self, account_id, owner_id=None):
        raise RuntimeError("Stored cookie could not be decrypted with this ENCRYPTION_KEY")

    def update_account_cookie_validation(self, account_id, status, detail="", owner_id=None):
        self.validation_updates.append((account_id, status, detail, owner_id))
        return True

    def account_exists(self, account_id, active_only=True, owner_id=None):
        return True

    def get_user_language(self, telegram_user_id):
        return "en"

    def create_post_jobs(self, jobs):
        self.created_jobs = True
        return ["unexpected"]


def build_app(storage):
    app = TelegramBotApp.__new__(TelegramBotApp)
    app.storage = storage
    app.admin_ids = set()
    app.debug_event = lambda *args, **kwargs: None
    return app


def test_encryption_key_guard_marks_cookie_invalid():
    storage = BrokenCookieStorage()
    app = build_app(storage)

    try:
        asyncio.run(app.ensure_account_cookie_readable("123", 99, "en"))
    except RuntimeError as exc:
        assert "old Render ENCRYPTION_KEY" in str(exc)
    else:
        raise AssertionError("expected encryption key RuntimeError")

    assert storage.validation_updates
    assert storage.validation_updates[0][0] == "123"
    assert storage.validation_updates[0][1] == "invalid"


def test_bulk_queue_stops_before_creating_jobs_when_cookie_cannot_decrypt():
    storage = BrokenCookieStorage()
    app = build_app(storage)

    try:
        asyncio.run(
            app.queue_bulk_post_jobs(
                1,
                99,
                "123",
                [{"page_url": "https://facebook.com/page", "page_name": "Page"}],
                "text",
                "caption",
                "",
            )
        )
    except RuntimeError as exc:
        assert "old Render ENCRYPTION_KEY" in str(exc)
    else:
        raise AssertionError("expected encryption key RuntimeError")

    assert storage.created_jobs is False
