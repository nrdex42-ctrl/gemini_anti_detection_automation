from telegram_dashboard import (
    LTR_MARK,
    admin_dashboard_markup,
    dashboard_action,
    dashboard_markup,
    dashboard_text,
    language_selection_markup,
    parse_image_mode_choice,
    parse_post_type_choice,
    parse_video_mode_choice,
    post_stage_reply_markup,
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

    assert markup["keyboard"][0] == ["➕ إضافة حساب", "🔁 تغيير الحساب", "👤 حساباتي"]
    assert markup["keyboard"][1] == ["⚡ الصفحات", "🧪 فحص الكوكيز", "📊 سجل المنشورات"]
    assert markup["keyboard"][2] == ["🌐 اللغة"]
    assert markup["is_persistent"] is False
    assert "زر المربع" in markup["input_field_placeholder"]
    assert "⚡ الصفحات" in labels
    assert "🌐 اللغة" in labels
    assert "📝 منشور نصي" not in labels
    assert "📸 منشور صورة" not in labels
    assert "🎬 منشور ريلز" not in labels
    assert "📋 انشر لكل الصفحات" not in labels
    assert "📄 الصفحات المحفوظة" not in labels
    assert "🔄 تحديث الصفحات" not in labels
    assert "⚡ حالة البوت" not in labels
    assert dashboard_action("🌐 اللغة") == "language"
    assert dashboard_action("📝 منشور نصي") == "quick_text"
    assert dashboard_action("📄 الصفحات المحفوظة") == "list_pages"

    assert post_type_choices("ar") == ("نص", "صورة", "ريلز")
    assert parse_post_type_choice("صورة") == "image"
    assert parse_post_type_choice("ريلز") == "video"
    assert parse_post_type_choice("فيديو") == "video"
    assert parse_post_type_choice("نص") == "text"

    language_labels = _inline_labels(language_selection_markup("ar"))
    assert "🇪🇬 العربية" in language_labels
    assert all("Egyptian" not in label for label in language_labels)

    assert "كوكيز" in prompt_text("add_account", lang="ar")


def test_admin_dashboard_keyboard_has_language_button():
    markup = admin_dashboard_markup()
    labels = _reply_labels(markup)

    assert "🌐 Language" in labels
    assert "🗑 Delete Users" in labels
    assert "📣 Broadcast" in labels
    assert "⚙️ Posting Mode" in labels
    assert markup["is_persistent"] is False
    assert "📊 System Stats" not in labels
    assert "📈 Post Stats" not in labels
    assert "⚙️ System Config" not in labels
    assert "🧰 Debug Snapshot" not in labels
    assert "🔐 Runtime Locks" not in labels
    assert "🔑 Accounts" not in labels
    assert dashboard_action("🌐 Language") == "language"
    assert dashboard_action("🗑 Delete Users") == "admin_delete_users"
    assert dashboard_action("📣 Broadcast") == "admin_broadcast"
    assert dashboard_action("⚙️ Posting Mode") == "admin_posting_mode"


def test_user_dashboard_admin_row_includes_language_and_admin_dashboard():
    markup = dashboard_markup(has_accounts=True, active_account="123", is_admin=True, lang="en")

    assert markup["keyboard"][0] == ["➕ Add Account", "🔁 Switch Account", "👤 My Accounts"]
    assert markup["keyboard"][1] == ["⚡ Post to Pages", "🧪 Check Cookies", "📊 Post History"]
    assert markup["keyboard"][2] == ["🌐 Language", "🔒 Admin Dashboard"]


def test_arabic_admin_dashboard_keyboard_is_translated():
    labels = _reply_labels(admin_dashboard_markup("ar"))

    assert "🌐 اللغة" in labels
    assert "👥 المستخدمين" in labels
    assert "🗑 حذف مستخدمين" in labels
    assert "📣 إرسال تنبيه" in labels
    assert "⚙️ طريقة النشر" in labels
    assert "📊 إحصائيات النظام" not in labels
    assert "📈 إحصائيات المنشورات" not in labels
    assert "⚙️ إعدادات النظام" not in labels
    assert "🧰 لقطة تصحيح" not in labels
    assert "🔐 أقفال التشغيل" not in labels
    assert "🔑 الحسابات" not in labels
    assert dashboard_action("🗑 حذف مستخدمين") == "admin_delete_users"
    assert dashboard_action("📣 إرسال تنبيه") == "admin_broadcast"
    assert dashboard_action("⚙️ طريقة النشر") == "admin_posting_mode"


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


def test_smart_dashboard_stored_pages_matches_visible_account_page_counts():
    text = dashboard_text(
        accounts=[
            {
                "account_id": "acct_omar",
                "label": "Omar Mohamed",
                "active": True,
                "cookie_status": "valid",
            }
        ],
        summary={
            "page_count": 4,
            "page_counts_by_account": {"acct_omar": 2},
            "job_status_counts": {},
        },
        active_account="acct_omar",
        lang="en",
    )

    assert "Stored pages: 2" in text
    assert "Stored pages: 4" not in text
    assert "Omar Mohamed | pages: 2" in text


def test_smart_dashboard_replaces_recent_posts_with_active_account_pages():
    text = dashboard_text(
        accounts=[
            {
                "account_id": "acct_omar",
                "label": "Omar Mohamed",
                "active": True,
                "cookie_status": "valid",
            }
        ],
        summary={
            "page_count": 2,
            "page_counts_by_account": {"acct_omar": 2},
            "job_status_counts": {},
            "recent_jobs": [
                {
                    "status": "success",
                    "post_type": "video",
                    "page_name": "Old Post Page",
                    "page_id_or_url": "old",
                }
            ],
        },
        active_account="acct_omar",
        active_pages=[
            {"page_id": "p1", "page_name": "Insan", "page_url": "https://facebook.com/insan"},
            {"page_id": "p2", "page_name": "Oppo", "page_url": "https://facebook.com/oppo"},
        ],
        lang="en",
    )

    assert "Available pages:" in text
    assert "- Insan" in text
    assert "- Oppo" in text
    assert "Recent posts:" not in text
    assert "Old Post Page" not in text


def test_post_stage_keyboards_match_current_card_stage():
    page_markup = post_stage_reply_markup("page_select", "ar")
    assert page_markup["keyboard"][0] == ["📝 منشور نصي", "📸 منشور صورة", "🎬 منشور ريلز"]
    assert page_markup["keyboard"][1] == ["📋 انشر لكل الصفحات"]
    assert page_markup["keyboard"][2] == ["⬅️ ارجع للوحة التحكم", "❌ إلغاء"]

    video_markup = post_stage_reply_markup("video_mode", "ar")
    video_labels = _reply_labels(video_markup)
    assert "📄 رفع ريلز واحد" in video_labels
    assert "📚 رفع ريلز متعددة" in video_labels
    assert "🔗 رابط ريلز واحد" in video_labels
    assert "🔗 روابط ريلز متعددة" in video_labels

    image_markup = post_stage_reply_markup("image_mode", "ar")
    image_labels = _reply_labels(image_markup)
    assert "📄 رفع صورة واحدة" in image_labels
    assert "📚 رفع صور متعددة" in image_labels
    assert "🔗 رابط صورة واحد" in image_labels
    assert "🔗 روابط صور متعددة" in image_labels

    assert parse_video_mode_choice("📄 Single Video Upload") == "single_upload"
    assert parse_video_mode_choice("📚 رفع ريلز متعددة") == "multi_upload"
    assert parse_video_mode_choice("🔗 رابط ريلز واحد") == "single_url"
    assert parse_video_mode_choice("🔗 روابط ريلز متعددة") == "multi_url"
    assert parse_image_mode_choice("📄 Single Image Upload") == "single_upload"
    assert parse_image_mode_choice("📚 رفع صور متعددة") == "multi_upload"
    assert parse_image_mode_choice("🔗 رابط صورة واحد") == "single_url"
    assert parse_image_mode_choice("🔗 روابط صور متعددة") == "multi_url"
