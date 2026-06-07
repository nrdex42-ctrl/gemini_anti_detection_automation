from telegram_dashboard import (
    dashboard_action,
    dashboard_markup,
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

    assert "⚡ انشر بالحساب النشط" in labels
    assert "🌐 اللغة" in labels
    assert dashboard_action("🌐 اللغة") == "language"
    assert dashboard_action("📝 منشور نصي سريع") == "quick_text"
    assert dashboard_action("📄 الصفحات المحفوظة") == "list_pages"

    assert post_type_choices("ar") == ("نص", "صورة", "فيديو")
    assert parse_post_type_choice("صورة") == "image"
    assert parse_post_type_choice("فيديو") == "video"
    assert parse_post_type_choice("نص") == "text"

    language_labels = _inline_labels(language_selection_markup("ar"))
    assert "🇪🇬 العربية" in language_labels
    assert all("Egyptian" not in label for label in language_labels)

    assert "كوكيز" in prompt_text("add_account", lang="ar")
