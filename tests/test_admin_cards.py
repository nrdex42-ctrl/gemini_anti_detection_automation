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

    def touch_user(self, *args):
        self.touched.append(args)


def test_admin_users_card_includes_profile_name_and_pm_time():
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.storage = AdminStorage()
        sent = []

        async def send_message(chat_id, text, reply_to_message_id=0, *, reply_markup=None, parse_mode=""):
            sent.append({"text": text, "reply_markup": reply_markup})
            return {"ok": True, "result": {"message_id": 777}}

        app.send_message = send_message

        await app.show_admin_users(123, 456, 99)

        text = sent[0]["text"]
        assert "Mohammed Shabana (@m_shabana)" in text
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
