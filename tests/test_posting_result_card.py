import sys
import types


if "aiohttp" not in sys.modules:
    aiohttp_stub = types.ModuleType("aiohttp")
    aiohttp_stub.ClientSession = object
    aiohttp_stub.web = types.SimpleNamespace()
    sys.modules["aiohttp"] = aiohttp_stub

from telegram_bot import format_elapsed_seconds, posting_result_card


def test_format_elapsed_seconds_is_compact():
    assert format_elapsed_seconds(42.2) == "42s"
    assert format_elapsed_seconds(192.4) == "3m 12s"
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
    assert "Debug ID: batch_test" in card
    assert "Caption page" in card
    assert "Image page" in card
    assert "Video page" in card
