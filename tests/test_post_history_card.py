from datetime import datetime, timezone

from telegram_bot import post_history_card


def test_post_history_card_groups_jobs_with_page_account_time_and_detail():
    text = post_history_card(
        [
            {
                "account_id": "acct_1",
                "page_id_or_url": "https://facebook.com/insan",
                "page_name": "Insan",
                "post_type": "video",
                "status": "success",
                "created_at": datetime(2026, 6, 10, 12, 5, tzinfo=timezone.utc),
                "completed_at": datetime(2026, 6, 10, 12, 7, tzinfo=timezone.utc),
            },
            {
                "account_id": "acct_1",
                "page_id_or_url": "https://facebook.com/oppo",
                "page_name": "Oppo",
                "post_type": "text",
                "status": "failed",
                "error": "Session expired while posting",
                "created_at": datetime(2026, 6, 10, 12, 10, tzinfo=timezone.utc),
            },
        ],
        [{"account_id": "acct_1", "label": "Omar Mohamed"}],
        lang="en",
    )

    assert "📊 Post History" in text
    assert "Showing latest 2 | ✅ 1 success | ❌ 1 failed | ⏳ 0 active" in text
    assert "1. ✅ Video • success" in text
    assert "Page: Insan" in text
    assert "Account: Omar Mohamed" in text
    assert "Time: 2026-06-10 03:07 PM" in text
    assert "2. ❌ Text • failed" in text
    assert "Detail: Session expired while posting" in text
    assert "success | acct_1 | video" not in text


def test_post_history_card_empty_state_keeps_card_header():
    text = post_history_card([], [], lang="en")

    assert text.splitlines() == [
        "📊 Post History",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "No post jobs yet.",
    ]
