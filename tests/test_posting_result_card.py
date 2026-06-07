import sys
import types


if "aiohttp" not in sys.modules:
    aiohttp_stub = types.ModuleType("aiohttp")
    aiohttp_stub.ClientSession = object
    aiohttp_stub.web = types.SimpleNamespace()
    sys.modules["aiohttp"] = aiohttp_stub

from telegram_bot import (
    LTR_MARK,
    POP_DIRECTIONAL_ISOLATE,
    POSTING_STATUS_SYNC_TEXT,
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
            {"page": "Video page", "success": True, "result": "video accepted"},
        ],
        debug_id="batch_test",
        elapsed_seconds=125,
    )

    assert "Posting complete: 3/3 succeeded" in card
    assert "Total time: 2m 05s" in card
    assert POSTING_STATUS_SYNC_TEXT in card
    assert "Debug ID" not in card
    assert "batch_test" not in card
    assert "Caption page" in card
    assert "Image page" in card
    assert "Video page" in card
    assert "Result:" not in card
    assert "Error:" not in card
    assert "caption accepted" not in card
    assert "image accepted" not in card
    assert "video accepted" not in card


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
