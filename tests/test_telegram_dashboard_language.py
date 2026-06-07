from telegram_dashboard import (
    LTR_MARK,
    admin_dashboard_markup,
    dashboard_action,
    dashboard_markup,
    dashboard_text,
    language_selection_markup,
    parse_post_type_choice,
    post_type_choices,
    prompt_text,
)


def _reply_labels(markup):
    return [str(button) for row in markup["keyboard"] for button in row]


def _inline_labels(markup):
    return [button["text"] for row in markup["inline_keyboard"] for button in row]


def test_arabic_dashboard_keyboard_actions_and_prompts():
    markup = dashboard_markup(has_accounts=True, active_account="123", lang="ar")
    labels = _reply_labels(markup)

    assert "⚡ النشر في الصفحات" in labels
    assert "🌐 اللغة" in labels
    assert dashboard_action("🌐 اللغة") == "language"
    assert dashboard_action("📝 منشور نصي") == "quick_text"
    assert dashboard_action("📄 الصفحات المحفوظة") == "list_pages"

    assert post_type_choices("ar") == ("نص", "صورة", "فيديو")
    assert parse_post_type_choice("صورة") == "image"
    assert parse_post_type_choice("فيديو") == "video"
    assert parse_post_type_choice("نص") == "text"

    language_labels = _inline_labels(language_selection_markup("ar"))
    assert "🇪🇬 العربية" in language_labels
    assert all("Egyptian" not in label for label in language_labels)

    assert "كوكيز" in prompt_text("add_account", lang="ar")


def test_admin_dashboard_keyboard_has_language_button():
    labels = _reply_labels(admin_dashboard_markup())

    assert "🌐 Language" in labels
    assert dashboard_action("🌐 Language") == "language"


def test_arabic_dashboard_account_status_icons_are_left_aligned():
    text = dashboard_text(
        accounts=[
            {
                "account_id": "acct_ar",
                "label": "اسماء ضياء",
                "active": True,
                "cookie_status": "valid",
            },
            {
                "account_id": "acct_en",
                "label": "Mohammed Mohammed",
                "active": True,
                "cookie_status": "invalid",
            },
        ],
        summary={
            "page_count": 11,
            "page_counts_by_account": {"acct_ar": 9, "acct_en": 2},
            "job_status_counts": {},
        },
        active_account="acct_ar",
        lang="ar",
    )

    lines = text.splitlines()
    status_lines = [line for line in lines if "الكوكيز:" in line]

    assert status_lines[0].startswith(f"{LTR_MARK}🟢 ")
    assert status_lines[1].startswith(f"{LTR_MARK}🔴 ")
    assert "اسماء ضياء" in status_lines[0]
    assert "Mohammed Mohammed" in status_lines[1]
