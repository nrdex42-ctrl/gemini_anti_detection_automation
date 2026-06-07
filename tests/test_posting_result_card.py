import sys
import types
import asyncio
from datetime import datetime, timezone


if "aiohttp" not in sys.modules:
    aiohttp_stub = types.ModuleType("aiohttp")
    aiohttp_stub.ClientSession = object
    aiohttp_stub.web = types.SimpleNamespace()
    sys.modules["aiohttp"] = aiohttp_stub

from telegram_bot import (
    LTR_MARK,
    POP_DIRECTIONAL_ISOLATE,
    POSTING_STATUS_SYNC_TEXT,
    TelegramBotApp,
    format_elapsed_seconds,
    posting_live_status_card,
    posting_result_card,
    status_detail_line,
)


def test_format_elapsed_seconds_is_compact():
    assert format_elapsed_seconds(42.2) == "42s"
    assert format_elapsed_seconds(192.4) == "3m 12s"
    assert format_elapsed_seconds(336) == "5m 36s"
    assert format_elapsed_seconds(3900) == "1h 05m"


def test_posting_result_card_includes_overall_elapsed_time():
    card = posting_result_card(
        [
            {"page": "Caption page", "success": True, "result": "caption accepted"},
            {"page": "Image page", "success": True, "result": "image accepted"},
            {"page": "Video page", "success": False, "result": "video rejected"},
        ],
        debug_id="batch_test",
        elapsed_seconds=125,
        completed_at=datetime(2026, 6, 7, 17, 54, tzinfo=timezone.utc),
    )

    assert "Posting complete: 2/3 succeeded" in card
    assert "Completed: 2026-06-07 08:54 PM" in card
    assert "Total time: 2m 05s" in card
    assert POSTING_STATUS_SYNC_TEXT in card
    assert "Succeeded pages: 2" in card
    assert "Failed pages: 1" in card
    assert "Debug ID" not in card
    assert "batch_test" not in card
    assert "Caption page" in card
    assert "Image page" in card
    assert "Video page" in card
    assert "Result:" not in card
    assert "Error:" not in card
    assert "caption accepted" not in card
    assert "image accepted" not in card
    assert "video rejected" not in card


def test_posting_live_status_card_uses_page_sync_text_instead_of_debug_id():
    card = posting_live_status_card(
        "Batch posting...",
        [{"job_id": "job_1", "page_name": "Page A"}],
        {"job_1": {"status": "running", "stage": "Uploading video"}},
        debug_id="batch_live",
    )

    assert POSTING_STATUS_SYNC_TEXT in card
    assert "Debug ID" not in card
    assert "batch_live" not in card
    assert "Page A" in card
    assert "Uploading video" in card


def test_status_detail_line_keeps_status_icon_left_aligned_for_mixed_languages():
    arabic_line = status_detail_line("🟢", "اسماء ضياء", "Facebook session is valid")
    english_line = status_detail_line("🔴", "Mohammed Mohammed", "Session check inconclusive")

    assert arabic_line.startswith(f"{LTR_MARK}🟢 ")
    assert english_line.startswith(f"{LTR_MARK}🔴 ")
    assert "اسماء ضياء" in arabic_line
    assert "Facebook session is valid" in arabic_line
    assert arabic_line.count(POP_DIRECTIONAL_ISOLATE) == 2


def test_posting_complete_card_restores_dashboard_reply_keyboard():
    async def run():
        app = TelegramBotApp.__new__(TelegramBotApp)
        app.dashboard_sessions = {"123:99": {"action": "post"}}
        deleted = []
        sent = []

        async def delete_message(chat_id, message_id):
            deleted.append((chat_id, message_id))

        async def dashboard_reply_markup(user_id):
            return {"keyboard": [["➕ Add Account", "🔁 Switch Account", "👤 My Accounts"]]}

        async def send_message(chat_id, text, reply_to_message_id=0, *, reply_markup=None, parse_mode=""):
            sent.append({"text": text, "reply_markup": reply_markup})
            return {"ok": True, "result": {"message_id": 888}}

        app.delete_message = delete_message
        app.dashboard_reply_markup = dashboard_reply_markup
        app.send_message = send_message

        message_id = await app.send_posting_complete_card(123, 99, "Posting complete", progress_message_id=777)

        assert message_id == 888
        assert deleted == [(123, 777)]
        assert "123:99" not in app.dashboard_sessions
        assert sent[0]["reply_markup"]["keyboard"][0] == ["➕ Add Account", "🔁 Switch Account", "👤 My Accounts"]
        labels = [button for row in sent[0]["reply_markup"]["keyboard"] for button in row]
        assert "✅ Done" not in labels
        assert "❌ Cancel" not in labels
        assert "⬅️ Back to Dashboard" not in labels

    asyncio.run(run())
