"""Telegram dashboard helpers for the raw Telegram Bot API service."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from page_name_utils import clean_facebook_page_name


BUTTON_ADD_ACCOUNT = "➕ Add Account"
BUTTON_POST_ACTIVE = "⚡ Post to Pages"
BUTTON_QUICK_TEXT = "📝 Text Post"
BUTTON_QUICK_IMAGE = "📸 Image Post"
BUTTON_QUICK_VIDEO = "🎬 Video Post"
BUTTON_POST_ALL_PAGES = "📋 Post to All Pages"
BUTTON_SWITCH_ACCOUNT = "🔁 Switch Account"
BUTTON_SELECT_ACCOUNT = "📱 Select Account & Post"
BUTTON_MY_ACCOUNTS = "👤 My Accounts"
BUTTON_CHECK_COOKIES = "🧪 Check Cookies"
BUTTON_POST_HISTORY = "📊 Post History"
BUTTON_STATUS = "⚡ Bot Status"
BUTTON_BACK = "⬅️ Back to Dashboard"
BUTTON_DASHBOARD = "🏠 Dashboard"
BUTTON_LANGUAGE = "🌐 Language"
BUTTON_DISCOVER_PAGES = "🔎 Discover Pages"
BUTTON_REFRESH_PAGES = "🔄 Refresh Pages"
BUTTON_LIST_PAGES = "📄 Stored Pages"
BUTTON_CHECK_THIS_ACCOUNT = "🧪 Check Account"
BUTTON_CONTINUE_TO_PAGES = "➡️ Continue to pages"
BUTTON_DONE = "✅ Done"
BUTTON_CANCEL = "❌ Cancel"
BUTTON_ADMIN = "🔒 Admin Dashboard"
BUTTON_USER_DASHBOARD = "🔁 User Dashboard"
BUTTON_SYSTEM_STATS = "📊 System Stats"
BUTTON_USERS = "👥 Users"
BUTTON_DELETE_USERS = "🗑 Delete Users"
BUTTON_BROADCAST = "📣 Broadcast"
BUTTON_ADMIN_ACCOUNTS = "🔑 Accounts"
BUTTON_POST_STATS = "📈 Post Stats"
BUTTON_RUNTIME_LOCKS = "🔐 Runtime Locks"
BUTTON_SYSTEM_CONFIG = "⚙️ System Config"
BUTTON_DEBUG_SNAPSHOT = "🧰 Debug Snapshot"

AR_BUTTON_ADD_ACCOUNT = "➕ إضافة حساب"
AR_BUTTON_POST_ACTIVE = "⚡ النشر في الصفحات"
AR_BUTTON_QUICK_TEXT = "📝 منشور نصي"
AR_BUTTON_QUICK_IMAGE = "📸 منشور صورة"
AR_BUTTON_QUICK_VIDEO = "🎬 منشور ريلز"
AR_BUTTON_POST_ALL_PAGES = "📋 انشر لكل الصفحات"
AR_BUTTON_SWITCH_ACCOUNT = "🔁 تغيير الحساب"
AR_BUTTON_SELECT_ACCOUNT = "📱 اختار حساب وانشر"
AR_BUTTON_MY_ACCOUNTS = "👤 حساباتي"
AR_BUTTON_CHECK_COOKIES = "🧪 فحص الكوكيز"
AR_BUTTON_POST_HISTORY = "📊 سجل المنشورات"
AR_BUTTON_STATUS = "⚡ حالة البوت"
AR_BUTTON_BACK = "⬅️ ارجع للوحة التحكم"
AR_BUTTON_DASHBOARD = "🏠 لوحة التحكم"
AR_BUTTON_LANGUAGE = "🌐 اللغة"
AR_BUTTON_DISCOVER_PAGES = "🔎 اكتشف الصفحات"
AR_BUTTON_REFRESH_PAGES = "🔄 تحديث الصفحات"
AR_BUTTON_LIST_PAGES = "📄 الصفحات المحفوظة"
AR_BUTTON_CHECK_THIS_ACCOUNT = "🧪 فحص الحساب ده"
AR_BUTTON_CONTINUE_TO_PAGES = "➡️ كمل للصفحات"
AR_BUTTON_DONE = "✅ تم"
AR_BUTTON_CANCEL = "❌ إلغاء"
AR_BUTTON_ADMIN = "🔒 لوحة الأدمن"
AR_BUTTON_USER_DASHBOARD = "🔁 لوحة المستخدم"
AR_BUTTON_SYSTEM_STATS = "📊 إحصائيات النظام"
AR_BUTTON_USERS = "👥 المستخدمين"
AR_BUTTON_DELETE_USERS = "🗑 حذف مستخدمين"
AR_BUTTON_BROADCAST = "📣 إرسال تنبيه"
AR_BUTTON_ADMIN_ACCOUNTS = "🔑 الحسابات"
AR_BUTTON_POST_STATS = "📈 إحصائيات المنشورات"
AR_BUTTON_RUNTIME_LOCKS = "🔐 أقفال التشغيل"
AR_BUTTON_SYSTEM_CONFIG = "⚙️ إعدادات النظام"
AR_BUTTON_DEBUG_SNAPSHOT = "🧰 لقطة تصحيح"

_BUTTONS_EN = {
    "add_account": BUTTON_ADD_ACCOUNT,
    "post_active": BUTTON_POST_ACTIVE,
    "quick_text": BUTTON_QUICK_TEXT,
    "quick_image": BUTTON_QUICK_IMAGE,
    "quick_video": BUTTON_QUICK_VIDEO,
    "post_all_pages": BUTTON_POST_ALL_PAGES,
    "switch_account": BUTTON_SWITCH_ACCOUNT,
    "select_account": BUTTON_SELECT_ACCOUNT,
    "my_accounts": BUTTON_MY_ACCOUNTS,
    "check_cookies": BUTTON_CHECK_COOKIES,
    "post_history": BUTTON_POST_HISTORY,
    "status": BUTTON_STATUS,
    "back": BUTTON_BACK,
    "dashboard": BUTTON_DASHBOARD,
    "language": BUTTON_LANGUAGE,
    "discover_pages": BUTTON_DISCOVER_PAGES,
    "refresh_pages": BUTTON_REFRESH_PAGES,
    "list_pages": BUTTON_LIST_PAGES,
    "check_this_account": BUTTON_CHECK_THIS_ACCOUNT,
    "continue_to_pages": BUTTON_CONTINUE_TO_PAGES,
    "done": BUTTON_DONE,
    "cancel": BUTTON_CANCEL,
    "admin": BUTTON_ADMIN,
    "user_dashboard": BUTTON_USER_DASHBOARD,
    "system_stats": BUTTON_SYSTEM_STATS,
    "users": BUTTON_USERS,
    "delete_users": BUTTON_DELETE_USERS,
    "broadcast": BUTTON_BROADCAST,
    "admin_accounts": BUTTON_ADMIN_ACCOUNTS,
    "post_stats": BUTTON_POST_STATS,
    "runtime_locks": BUTTON_RUNTIME_LOCKS,
    "system_config": BUTTON_SYSTEM_CONFIG,
    "debug_snapshot": BUTTON_DEBUG_SNAPSHOT,
}

_BUTTONS_AR = {
    "add_account": AR_BUTTON_ADD_ACCOUNT,
    "post_active": AR_BUTTON_POST_ACTIVE,
    "quick_text": AR_BUTTON_QUICK_TEXT,
    "quick_image": AR_BUTTON_QUICK_IMAGE,
    "quick_video": AR_BUTTON_QUICK_VIDEO,
    "post_all_pages": AR_BUTTON_POST_ALL_PAGES,
    "switch_account": AR_BUTTON_SWITCH_ACCOUNT,
    "select_account": AR_BUTTON_SELECT_ACCOUNT,
    "my_accounts": AR_BUTTON_MY_ACCOUNTS,
    "check_cookies": AR_BUTTON_CHECK_COOKIES,
    "post_history": AR_BUTTON_POST_HISTORY,
    "status": AR_BUTTON_STATUS,
    "back": AR_BUTTON_BACK,
    "dashboard": AR_BUTTON_DASHBOARD,
    "language": AR_BUTTON_LANGUAGE,
    "discover_pages": AR_BUTTON_DISCOVER_PAGES,
    "refresh_pages": AR_BUTTON_REFRESH_PAGES,
    "list_pages": AR_BUTTON_LIST_PAGES,
    "check_this_account": AR_BUTTON_CHECK_THIS_ACCOUNT,
    "continue_to_pages": AR_BUTTON_CONTINUE_TO_PAGES,
    "done": AR_BUTTON_DONE,
    "cancel": AR_BUTTON_CANCEL,
    "admin": AR_BUTTON_ADMIN,
    "user_dashboard": AR_BUTTON_USER_DASHBOARD,
    "system_stats": AR_BUTTON_SYSTEM_STATS,
    "users": AR_BUTTON_USERS,
    "delete_users": AR_BUTTON_DELETE_USERS,
    "broadcast": AR_BUTTON_BROADCAST,
    "admin_accounts": AR_BUTTON_ADMIN_ACCOUNTS,
    "post_stats": AR_BUTTON_POST_STATS,
    "runtime_locks": AR_BUTTON_RUNTIME_LOCKS,
    "system_config": AR_BUTTON_SYSTEM_CONFIG,
    "debug_snapshot": AR_BUTTON_DEBUG_SNAPSHOT,
}


def normalize_lang(lang: str = "en") -> str:
    return "ar" if str(lang or "").strip().lower() == "ar" else "en"


def tr(lang: str, en: str, ar: str) -> str:
    return ar if normalize_lang(lang) == "ar" else en


LTR_MARK = "\u200e"
FIRST_STRONG_ISOLATE = "\u2068"
POP_DIRECTIONAL_ISOLATE = "\u2069"


def bidi_isolate(value: Any) -> str:
    return f"{FIRST_STRONG_ISOLATE}{str(value or '')}{POP_DIRECTIONAL_ISOLATE}"


def status_detail_line(icon: str, name: Any, detail: Any) -> str:
    return f"{LTR_MARK}{icon} {bidi_isolate(name)} | {bidi_isolate(detail)}"


def button_text(key: str, lang: str = "en") -> str:
    table = _BUTTONS_AR if normalize_lang(lang) == "ar" else _BUTTONS_EN
    return table.get(key, _BUTTONS_EN.get(key, key))


DASHBOARD_ACTIONS = {
    BUTTON_DASHBOARD: "dashboard",
    BUTTON_BACK: "dashboard",
    BUTTON_LANGUAGE: "language",
    "Dashboard": "dashboard",
    "Language": "language",
    "🌐 اللغة": "language",
    "menu": "dashboard",
    "Menu": "dashboard",
    BUTTON_ADD_ACCOUNT: "add_account",
    BUTTON_POST_ACTIVE: "post_active",
    BUTTON_QUICK_TEXT: "quick_text",
    BUTTON_QUICK_IMAGE: "quick_image",
    BUTTON_QUICK_VIDEO: "quick_video",
    BUTTON_POST_ALL_PAGES: "post_all_pages",
    BUTTON_SWITCH_ACCOUNT: "switch_account",
    BUTTON_SELECT_ACCOUNT: "select_account",
    BUTTON_MY_ACCOUNTS: "manage_accounts",
    BUTTON_CHECK_COOKIES: "check_cookies",
    BUTTON_POST_HISTORY: "post_history",
    BUTTON_STATUS: "status",
    BUTTON_DISCOVER_PAGES: "discover_pages",
    BUTTON_REFRESH_PAGES: "refresh_pages",
    BUTTON_LIST_PAGES: "list_pages",
    BUTTON_CHECK_THIS_ACCOUNT: "check_active_account",
    BUTTON_CONTINUE_TO_PAGES: "continue_active_account",
    BUTTON_CANCEL: "cancel",
    BUTTON_ADMIN: "admin_dashboard",
    BUTTON_USER_DASHBOARD: "user_dashboard",
    BUTTON_SYSTEM_STATS: "admin_system_stats",
    BUTTON_USERS: "admin_users",
    BUTTON_DELETE_USERS: "admin_delete_users",
    BUTTON_BROADCAST: "admin_broadcast",
    BUTTON_ADMIN_ACCOUNTS: "admin_accounts",
    BUTTON_POST_STATS: "admin_post_stats",
    BUTTON_RUNTIME_LOCKS: "admin_runtime_locks",
    BUTTON_SYSTEM_CONFIG: "admin_system_config",
    BUTTON_DEBUG_SNAPSHOT: "admin_debug_snapshot",
    # Backward-compatible labels from the first lightweight dashboard.
    "➕ Add Account": "add_account",
    "👥 Accounts": "manage_accounts",
    "📝 Text Post": "quick_text",
    "🖼 Image Post": "quick_image",
    "🎬 Video Post": "quick_video",
    "➕ Add Facebook Account": "add_account",
    "⚡ Post With Active Account": "post_active",
    "📝 Quick Text Post": "quick_text",
    "📸 Quick Image Post": "quick_image",
    "🎬 Quick Video Post": "quick_video",
    "🔁 Switch Active Account": "switch_account",
    "🧪 Check All Cookies": "check_cookies",
    "📊 Bot Status": "status",
    "➕ ضيف حساب فيسبوك": "add_account",
    "⚡ انشر بالحساب النشط": "post_active",
    "📝 منشور نصي سريع": "quick_text",
    "📸 منشور صورة سريع": "quick_image",
    "🎬 منشور فيديو": "quick_video",
    "🎬 منشور فيديو سريع": "quick_video",
    "🎬 منشور ريلز سريع": "quick_video",
    "🔁 غيّر الحساب النشط": "switch_account",
    "🧪 افحص كل الكوكيز": "check_cookies",
    "📊 حالة البوت": "status",
}

DASHBOARD_ACTIONS.update(
    {
        AR_BUTTON_ADD_ACCOUNT: "add_account",
        AR_BUTTON_POST_ACTIVE: "post_active",
        AR_BUTTON_QUICK_TEXT: "quick_text",
        AR_BUTTON_QUICK_IMAGE: "quick_image",
        AR_BUTTON_QUICK_VIDEO: "quick_video",
        AR_BUTTON_POST_ALL_PAGES: "post_all_pages",
        AR_BUTTON_SWITCH_ACCOUNT: "switch_account",
        AR_BUTTON_SELECT_ACCOUNT: "select_account",
        AR_BUTTON_MY_ACCOUNTS: "manage_accounts",
        AR_BUTTON_CHECK_COOKIES: "check_cookies",
        AR_BUTTON_POST_HISTORY: "post_history",
        AR_BUTTON_STATUS: "status",
        AR_BUTTON_BACK: "dashboard",
        AR_BUTTON_DASHBOARD: "dashboard",
        AR_BUTTON_LANGUAGE: "language",
        AR_BUTTON_DISCOVER_PAGES: "discover_pages",
        AR_BUTTON_REFRESH_PAGES: "refresh_pages",
        AR_BUTTON_LIST_PAGES: "list_pages",
        AR_BUTTON_CHECK_THIS_ACCOUNT: "check_active_account",
        AR_BUTTON_CONTINUE_TO_PAGES: "continue_active_account",
        AR_BUTTON_CANCEL: "cancel",
        AR_BUTTON_ADMIN: "admin_dashboard",
        AR_BUTTON_USER_DASHBOARD: "user_dashboard",
        AR_BUTTON_SYSTEM_STATS: "admin_system_stats",
        AR_BUTTON_USERS: "admin_users",
        AR_BUTTON_DELETE_USERS: "admin_delete_users",
        AR_BUTTON_BROADCAST: "admin_broadcast",
        AR_BUTTON_ADMIN_ACCOUNTS: "admin_accounts",
        AR_BUTTON_POST_STATS: "admin_post_stats",
        AR_BUTTON_RUNTIME_LOCKS: "admin_runtime_locks",
        AR_BUTTON_SYSTEM_CONFIG: "admin_system_config",
        AR_BUTTON_DEBUG_SNAPSHOT: "admin_debug_snapshot",
    }
)

POST_ACTION_TYPES = {
    "quick_text": "text",
    "quick_image": "image",
    "quick_video": "video",
    "post_text": "text",
    "post_image": "image",
    "post_video": "video",
}

POST_TYPE_CHOICES = ("Text", "Image", "Video")


def dashboard_action(text: str) -> str:
    return DASHBOARD_ACTIONS.get((text or "").strip(), "")


def parse_post_type_choice(text: str) -> str:
    normalized = (text or "").strip().lower()
    if "image" in normalized or "photo" in normalized or "صورة" in normalized:
        return "image"
    if "video" in normalized or "فيديو" in normalized or "ريلز" in normalized:
        return "video"
    if (
        "text" in normalized
        or "caption" in normalized
        or "نص" in normalized
        or "كابشن" in normalized
        or "منشور نصي" in normalized
    ):
        return "text"
    return ""


def post_type_choices(lang: str = "en") -> Sequence[str]:
    if normalize_lang(lang) == "ar":
        return ("نص", "صورة", "ريلز")
    return POST_TYPE_CHOICES


def reply_keyboard(
    rows: List[List[str]],
    *,
    placeholder: str = "Choose a dashboard action...",
    persistent: bool = True,
) -> Dict[str, Any]:
    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "is_persistent": persistent,
        "input_field_placeholder": placeholder[:64],
    }


def dashboard_markup(
    *,
    has_accounts: bool = False,
    active_account: str = "",
    active_jobs: int = 0,
    posting_blocked: bool = False,
    is_admin: bool = False,
    lang: str = "en",
) -> Dict[str, Any]:
    rows: List[List[str]] = []



    rows.append([button_text("add_account", lang), button_text("switch_account", lang), button_text("my_accounts", lang)])
    rows.append([button_text("post_active", lang), button_text("check_cookies", lang), button_text("post_history", lang)])
    if is_admin:
        rows.append([button_text("language", lang), button_text("admin", lang)])
    else:
        rows.append([button_text("language", lang)])
    return reply_keyboard(rows, placeholder=tr(lang, "Choose a dashboard action...", "اختر إجراء لوحة التحكم..."))


def admin_dashboard_markup(lang: str = "en") -> Dict[str, Any]:
    return reply_keyboard(
        [
            [button_text("users", lang), button_text("delete_users", lang)],
            [button_text("broadcast", lang), button_text("language", lang)],
            [button_text("user_dashboard", lang)],
        ],
        placeholder=tr(lang, "Choose an admin action...", "اختر إجراء للأدمن..."),
    )


def cancel_markup(lang: str = "en") -> Dict[str, Any]:
    return reply_keyboard(
        [[button_text("back", lang), button_text("cancel", lang)]],
        placeholder=tr(lang, "Send the requested value", "ابعت القيمة المطلوبة"),
    )


def skip_cancel_markup(lang: str = "en") -> Dict[str, Any]:
    skip_text = "⏭️ تخطي" if normalize_lang(lang) == "ar" else "⏭️ Skip"
    return reply_keyboard(
        [[skip_text], [button_text("back", lang), button_text("cancel", lang)]],
        placeholder=tr(lang, "Send caption or tap Skip", "ابعت الكابشن أو اضغط تخطي"),
    )


def cookie_input_markup(lang: str = "en") -> Dict[str, Any]:
    return reply_keyboard(
        [[button_text("done", lang)], [button_text("back", lang), button_text("cancel", lang)]],
        placeholder=tr(lang, "Paste cookies or upload JSON", "الصق الكوكيز أو ارفع ملف JSON"),
    )


def done_cancel_markup(lang: str = "en", *, placeholder: str = "") -> Dict[str, Any]:
    return reply_keyboard(
        [[button_text("done", lang)], [button_text("back", lang), button_text("cancel", lang)]],
        placeholder=placeholder or tr(lang, "Send value, then tap Done", "ابعت القيمة، ثم اضغط تم"),
    )


def account_post_action_markup(lang: str = "en") -> Dict[str, Any]:
    return reply_keyboard(
        [
            [button_text("check_this_account", lang)],
            [button_text("continue_to_pages", lang), button_text("refresh_pages", lang)],
            [button_text("back", lang)],
        ],
        placeholder=tr(lang, "Choose account action", "اختار إجراء الحساب"),
        persistent=False,
    )


def choices_markup(choices: Iterable[str], *, placeholder: str = "Choose or type a value", lang: str = "en") -> Dict[str, Any]:
    rows: List[List[str]] = []
    row: List[str] = []
    for choice in choices:
        value = str(choice).strip()
        if not value:
            continue
        row.append(value[:96])
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([button_text("back", lang), button_text("cancel", lang)])
    return reply_keyboard(rows, placeholder=placeholder, persistent=False)


def inline_button(text: str, callback_data: str) -> Dict[str, str]:
    return {"text": text[:64], "callback_data": callback_data[:64]}


def inline_markup(rows: Sequence[Sequence[Dict[str, str]]]) -> Dict[str, Any]:
    return {"inline_keyboard": [list(row) for row in rows]}


def language_selection_markup(lang: str = "en") -> Dict[str, Any]:
    return inline_markup(
        [
            [inline_button(tr(lang, "🇪🇬 Arabic", "🇪🇬 العربية"), "lang:ar")],
            [inline_button("🇬🇧 English", "lang:en")],
            [inline_button(tr(lang, "⬅️ Back", "⬅️ رجوع"), "dash:back")],
        ]
    )


def account_choice_label(account: Dict[str, Any], active_account: str = "") -> str:
    account_id = str(account.get("account_id") or "").strip()
    status = "active" if account.get("active") else "inactive"
    marker = "✅" if active_account and account_id == active_account else "👤"
    return f"{marker} {account_display_name(account)} | {status}"


def parse_choice_id(text: str) -> str:
    value = (text or "").strip()
    for marker in ("✅ ", "👤 "):
        if value.startswith(marker):
            value = value[len(marker):].strip()
    return value.split("|", 1)[0].strip()


def account_display_name(account: Dict[str, Any], fallback_id: str = "", *, include_id: bool = False) -> str:
    account_id = str(account.get("account_id") or fallback_id or "").strip()
    label = str(account.get("label") or "").strip()
    if label and label != account_id:
        display = "Facebook Account" if label == "Facebook Account" or label.startswith("Facebook Account ") else label
        should_show_id = include_id or display == "Facebook Account"
        return f"{display} ({account_id})" if should_show_id and account_id else display
    if account_id:
        return account_id
    return "Facebook Account"


def page_choice_label(page: Dict[str, Any]) -> str:
    page_id = str(page.get("page_id") or "").strip()
    page_url = str(page.get("page_url") or "").strip()
    page_name = str(page.get("page_name") or page_id or page_url).strip()
    identity = page_url or page_id
    if page_name and page_name != identity:
        return f"{identity} | {page_name[:48]}"
    return identity


def page_display_name(page: Dict[str, Any], index: int = 0) -> str:
    page_url = str(page.get("page_url") or page.get("url") or "").strip()
    name = clean_facebook_page_name(page.get("page_name") or page.get("name"), page_url)
    if name:
        return name
    return f"Page {index + 1}" if index >= 0 else "Page"


def _short(value: Any, limit: int = 80) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else f"{text[: max(0, limit - 3)]}..."


def page_selection_card(
    *,
    account_name: str,
    pages: List[Dict[str, Any]],
    selected_indexes: List[int],
    prefix: str = "",
    lang: str = "en",
) -> str:
    selected = set(selected_indexes)
    lines: List[str] = []
    if prefix:
        lines.extend([prefix, ""])
    lines.extend(
        [
            tr(lang, "📄 Select Pages", "📄 اختار الصفحات"),
            "━━━━━━━━━━━━━━━━━━",
            tr(lang, f"Account: {_short(account_name or 'Facebook Account', 70)}", f"الحساب: {_short(account_name or 'Facebook Account', 70)}"),
            tr(lang, f"Available pages: {len(pages)}", f"الصفحات المتاحة: {len(pages)}"),
            tr(lang, f"Selected: {len(selected)}", f"المحدد: {len(selected)}"),
            "",
        ]
    )
    if not pages:
        lines.append(tr(lang, "No cached pages found. Refresh pages first.", "لا توجد صفحات محفوظة. حدّث الصفحات أولاً."))
    else:
        for idx, page in enumerate(pages[:10]):
            marker = "✅" if idx in selected else "⬜"
            lines.append(f"{marker} {page_display_name(page, idx)}")
        remaining = len(pages) - 10
        if remaining > 0:
            lines.append(tr(lang, f"... and {remaining} more", f"... و {remaining} إضافية"))
    lines.extend(["", tr(lang, "Tap page names to toggle, then Confirm.", "اضغط أسماء الصفحات للتحديد، ثم تأكيد.")])
    return "\n".join(lines)


def page_selection_markup(pages: List[Dict[str, Any]], selected_indexes: List[int], lang: str = "en") -> Dict[str, Any]:
    selected = set(selected_indexes)
    rows: List[List[Dict[str, str]]] = []
    for idx, page in enumerate(pages[:24]):
        marker = "✅" if idx in selected else "⬜"
        rows.append([inline_button(f"{marker} {_short(page_display_name(page, idx), 48)}", f"pg:{idx}")])
    if pages:
        rows.append([inline_button(tr(lang, "📋 All", "📋 الكل"), "pg:all"), inline_button(tr(lang, "✅ Confirm", "✅ تأكيد"), "pg:confirm")])
    rows.append([inline_button(button_text("refresh_pages", lang), "pg:refresh")])
    rows.append([inline_button(button_text("dashboard", lang), "dash:back")])
    return inline_markup(rows)


def post_type_card(*, account_name: str, pages: List[Dict[str, Any]], selected_indexes: List[int], lang: str = "en") -> str:
    page_names = [
        page_display_name(pages[idx], idx)
        for idx in selected_indexes
        if isinstance(idx, int) and 0 <= idx < len(pages)
    ]
    preview = ", ".join(_short(name, 32) for name in page_names[:4])
    if len(page_names) > 4:
        preview += f", +{len(page_names) - 4} more"
    return "\n".join(
        [
            tr(lang, "📝 Choose Post Type", "📝 اختار نوع المنشور"),
            "━━━━━━━━━━━━━━━━━━",
            tr(lang, f"Account: {_short(account_name or 'Facebook Account', 70)}", f"الحساب: {_short(account_name or 'Facebook Account', 70)}"),
            tr(lang, f"Pages: {len(page_names)}", f"الصفحات: {len(page_names)}"),
            tr(lang, f"Selected: {preview or 'none'}", f"المحدد: {preview or 'لا يوجد'}"),
            "",
            tr(lang, "Choose what you want to post.", "اختار نوع المحتوى اللي عايز تنشره."),
        ]
    )


def post_type_inline_markup(lang: str = "en") -> Dict[str, Any]:
    return inline_markup(
        [
            [inline_button(tr(lang, "📝 Caption/Text", "📝 نص/كابشن"), "post:type:text")],
            [inline_button(tr(lang, "📸 Image", "📸 صورة"), "post:type:image"), inline_button(tr(lang, "🎬 Video", "🎬 ريلز"), "post:type:video")],
            [inline_button(tr(lang, "⬅️ Pages", "⬅️ الصفحات"), "post:pages"), inline_button(button_text("dashboard", lang), "dash:back")],
        ]
    )


def video_mode_card(*, account_name: str, pages: List[Dict[str, Any]], selected_indexes: List[int], lang: str = "en") -> str:
    page_names = [
        page_display_name(pages[idx], idx)
        for idx in selected_indexes
        if isinstance(idx, int) and 0 <= idx < len(pages)
    ]
    preview = ", ".join(_short(name, 32) for name in page_names[:4])
    if len(page_names) > 4:
        preview += f", +{len(page_names) - 4} more"
    return "\n".join(
        [
            tr(lang, "🎬 Video Posting Mode", "🎬 طريقة نشر الريلز"),
            "━━━━━━━━━━━━━━━━━━",
            tr(lang, f"Account: {_short(account_name or 'Facebook Account', 70)}", f"الحساب: {_short(account_name or 'Facebook Account', 70)}"),
            tr(lang, f"Pages: {len(page_names)}", f"الصفحات: {len(page_names)}"),
            tr(lang, f"Selected: {preview or 'none'}", f"المحدد: {preview or 'لا يوجد'}"),
            "",
            tr(lang, "Choose how videos should be attached.", "اختار طريقة إرفاق الريلز."),
        ]
    )


def video_mode_inline_markup(lang: str = "en") -> Dict[str, Any]:
    return inline_markup(
        [
            [inline_button(tr(lang, "📄 Single video upload → all pages", "📄 ريلز واحد → كل الصفحات"), "video:single_upload")],
            [inline_button(tr(lang, "📚 Multi videos upload → one per page", "📚 ريلز لكل صفحة"), "video:multi_upload")],
            [inline_button(tr(lang, "🔗 Single video URL → all pages", "🔗 رابط ريلز واحد → كل الصفحات"), "video:single_url")],
            [inline_button(tr(lang, "🔗 Multi video URLs → one per page", "🔗 رابط ريلز لكل صفحة"), "video:multi_url")],
            [inline_button(tr(lang, "⬅️ Pages", "⬅️ الصفحات"), "post:pages"), inline_button(button_text("dashboard", lang), "dash:back")],
        ]
    )


def post_input_card(post_type: str, lang: str = "en") -> str:
    if post_type == "text":
        action = tr(lang, "Send the caption/text message now.", "ابعت النص/الكابشن دلوقتي.")
        title = tr(lang, "📝 Caption Post", "📝 منشور نصي")
    elif post_type == "image":
        action = tr(lang, "Send or reply with the image now. Telegram media caption is optional.", "ابعت الصورة أو اعمل رد بصورة دلوقتي. كابشن الميديا اختياري.")
        title = tr(lang, "📸 Image Post", "📸 منشور صورة")
    else:
        action = tr(lang, "Send or reply with the video now. Telegram media caption is optional.", "ابعت الريلز أو اعمل رد بريلز دلوقتي. كابشن الميديا اختياري.")
        title = tr(lang, "🎬 Video Post", "🎬 منشور ريلز")
    return "\n".join([title, "━━━━━━━━━━━━━━━━━━", action, "", tr(lang, "After that, I will show a final review card.", "بعد كده هاعرضلك كارت المراجعة النهائي.")])


def post_review_card(
    *,
    account_name: str,
    pages: List[Dict[str, Any]],
    post_type: str,
    caption: str,
    media_path: str = "",
    multi_media_count: int = 0,
    multi_caption_count: int = 0,
    lang: str = "en",
) -> str:
    if multi_caption_count and not caption:
        caption_value = tr(lang, f"{multi_caption_count} attached (one per page)", f"{multi_caption_count} مرفقين (واحد لكل صفحة)")
    else:
        caption_value = _short(caption, 700) if caption else tr(lang, "(none)", "(لا يوجد)")
    media_value = (
        tr(lang, f"{multi_media_count} attached (one per page)", f"{multi_media_count} مرفقين (واحد لكل صفحة)")
        if multi_media_count
        else (tr(lang, "attached", "مرفق") if media_path else tr(lang, "(none)", "(لا يوجد)"))
    )
    lines = [
        tr(lang, "🧾 Review Post", "🧾 مراجعة المنشور"),
        "━━━━━━━━━━━━━━━━━━",
        tr(lang, f"Type: {post_type}", f"النوع: {post_type}"),
        tr(lang, f"Account: {_short(account_name or 'Facebook Account', 70)}", f"الحساب: {_short(account_name or 'Facebook Account', 70)}"),
        tr(lang, f"Selected pages: {len(pages)}", f"الصفحات المحددة: {len(pages)}"),
    ]
    for idx, page in enumerate(pages[:6]):
        lines.append(f"• {page_display_name(page, idx)}")
    remaining = len(pages) - 6
    if remaining > 0:
        lines.append(tr(lang, f"... and {remaining} more", f"... و {remaining} إضافية"))
    lines.extend(["", tr(lang, f"Caption: {caption_value}", f"الكابشن: {caption_value}")])
    if post_type in {"image", "video"}:
        lines.append(tr(lang, f"Media: {media_value}", f"الميديا: {media_value}"))
    lines.extend(["", tr(lang, "Confirm to start posting, or edit the caption.", "أكد لبدء النشر، أو عدّل الكابشن.")])
    return "\n".join(lines)


def post_confirm_inline_markup(lang: str = "en") -> Dict[str, Any]:
    return inline_markup(
        [
            [inline_button(tr(lang, "✏️ Edit Caption", "✏️ تعديل الكابشن"), "post:edit_caption")],
            [inline_button(tr(lang, "✅ Post Now", "✅ انشر الآن"), "post:confirm")],
            [inline_button(tr(lang, "⬅️ Pages", "⬅️ الصفحات"), "post:pages"), inline_button(button_text("dashboard", lang), "dash:back")],
        ]
    )


def _format_dt(value: Any) -> str:
    if not value:
        return "never"
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    display_tz = timezone(timedelta(hours=3))
    return dt.astimezone(display_tz).strftime("%I:%M %p")


def _account_health_icon(account: Dict[str, Any], active_account: str) -> str:
    if str(account.get("cookie_status") or "unverified").lower() == "valid":
        return "🟢"
    return "🔴"


def _account_cookie_status_label(account: Dict[str, Any], lang: str = "en") -> str:
    status = str(account.get("cookie_status") or "unverified").strip().lower()
    if status == "valid":
        return tr(lang, "valid", "صالحة")
    if status == "invalid":
        return tr(lang, "invalid", "غير صالحة")
    return tr(lang, "not verified", "لم يتم التحقق")


def dashboard_text(
    *,
    accounts: List[Dict[str, Any]],
    summary: Optional[Dict[str, Any]] = None,
    active_account: str = "",
    prefix: str = "",
    lang: str = "en",
) -> str:
    summary = summary or {}
    status_counts = summary.get("job_status_counts") or {}
    active_jobs = int(status_counts.get("queued", 0)) + int(status_counts.get("processing", 0))
    locked_accounts = summary.get("locked_accounts") or []
    recent_jobs = summary.get("recent_jobs") or []
    page_counts = summary.get("page_counts_by_account") or {}

    lines: List[str] = []
    if prefix:
        lines.extend([prefix, ""])

    account_by_id = {str(account.get("account_id") or ""): account for account in accounts}
    active_text = (
        account_display_name(account_by_id.get(active_account, {}), active_account)
        if active_account
        else tr(lang, "not selected", "غير محدد")
    )
    lines.extend(
        [
            tr(lang, "🎛️ Smart Dashboard", "🎛️ لوحة التحكم الذكية"),
            "━━━━━━━━━━━━━━━━━━",
            tr(lang, f"Accounts: {len(accounts)} total", f"الحسابات: {len(accounts)} إجمالي"),
            tr(lang, f"Active account: {active_text}", f"الحساب النشط: {active_text}"),
            tr(lang, f"Stored pages: {int(summary.get('page_count') or 0)}", f"الصفحات المحفوظة: {int(summary.get('page_count') or 0)}"),
            tr(
                lang,
                f"Jobs: {active_jobs} active, {int(status_counts.get('success', 0))} success, {int(status_counts.get('failed', 0))} failed",
                f"المهام: {active_jobs} نشطة، {int(status_counts.get('success', 0))} ناجحة، {int(status_counts.get('failed', 0))} فاشلة",
            ),
        ]
    )

    if not accounts:
        lines.extend(
            [
                "",
                tr(
                    lang,
                    "No accounts yet. Tap Add Facebook Account and paste a raw cookie string or JSON export.",
                    "لا توجد حسابات بعد. اضغط ضيف حساب فيسبوك والصق الكوكيز أو ارفع ملف JSON.",
                ),
            ]
        )
    elif not active_account:
        lines.extend(
            [
                "",
                tr(
                    lang,
                    "No active account selected. Tap Select Account & Post or Switch Active Account.",
                    "لا يوجد حساب نشط. اضغط اختار حساب وانشر أو غيّر الحساب النشط.",
                ),
            ]
        )
    else:
        lines.extend(["", tr(lang, "Available accounts:", "الحسابات المتاحة:")])
        for account in accounts[:6]:
            account_id = str(account.get("account_id") or "")
            icon = _account_health_icon(account, active_account)
            pages = int(page_counts.get(account_id, 0))
            display = account_display_name(account)
            cookie_status = _account_cookie_status_label(account, lang)
            lines.append(
                (
                    f"{icon} {display} | pages: {pages} | cookies: {cookie_status}"
                    if normalize_lang(lang) == "en"
                    else status_detail_line(
                        icon,
                        display,
                        f"صفحات: {pages} | الكوكيز: {cookie_status}",
                    )
                )
            )
        if len(accounts) > 6:
            lines.append(tr(lang, f"... {len(accounts) - 6} more", f"... و {len(accounts) - 6} إضافية"))

    if locked_accounts:
        lines.append("")
        lines.append(tr(lang, "Session safety:", "أمان الجلسة:"))
        for item in locked_accounts[:4]:
            locked_id = str(item.get("account_id") or "")
            locked_name = account_display_name(account_by_id.get(locked_id, {}), locked_id)
            lines.append(
                tr(
                    lang,
                    f"- {locked_name}: locked until {_format_dt(item.get('locked_until'))}",
                    f"- {locked_name}: مقفول لحد {_format_dt(item.get('locked_until'))}",
                )
            )

    if recent_jobs:
        lines.append("")
        lines.append(tr(lang, "Recent posts:", "آخر المنشورات:"))
        for job in recent_jobs[:5]:
            target = clean_facebook_page_name(
                job.get("page_name"),
                str(job.get("page_id_or_url") or ""),
                str(job.get("page_id_or_url") or ""),
            )[:36]
            lines.append(
                f"- {job.get('status')} {job.get('post_type')} -> {target}"
            )

    lines.extend(
        [
            "",
            tr(
                lang,
                "Use the keyboard buttons below. /start refreshes this dashboard.",
                "استخدم أزرار لوحة الكتابة بالأسفل. /start يحدّث لوحة التحكم.",
            ),
        ]
    )
    return "\n".join(lines)


def prompt_text(action: str, step: str = "", lang: str = "en") -> str:
    if action == "add_account":
        return tr(
            lang,
            "Send Facebook session cookies.\n\n"
            "Accepted formats:\n"
            "1. Raw cookie string in one message.\n"
            "2. Exported JSON file or JSON text.\n"
            "3. Long JSON across multiple messages, then tap Done.",
            "ابعت كوكيز جلسة فيسبوك.\n\n"
            "الصيغ المقبولة:\n"
            "1. كوكيز خام في رسالة واحدة.\n"
            "2. ملف JSON أو نص JSON.\n"
            "3. JSON طويل على كذا رسالة، وبعدها اضغط تم.",
        )
    if step == "account":
        return tr(lang, "Choose an account from the keyboard.", "اختار حساب من لوحة الكتابة.")
    if step == "post_type":
        return tr(lang, "Choose the post type.", "اختار نوع المنشور.")
    if step == "page":
        return tr(lang, "Choose a stored page or type a page id / full page URL.", "اختار صفحة محفوظة أو اكتب ID الصفحة / رابطها كامل.")
    if step == "caption":
        return tr(lang, "Send the post caption/text.", "ابعت نص/كابشن المنشور.")
    if step == "caption_all":
        return tr(lang, "Send the caption/text to post to every stored page for this account.", "ابعت النص/الكابشن للنشر على كل الصفحات المحفوظة للحساب ده.")
    if step == "media_image":
        return tr(lang, "Attach or reply with the image now. The media caption will be used as the post caption.", "ارفق الصورة أو اعمل رد بصورة دلوقتي. كابشن الميديا هيتستخدم ككابشن للمنشور.")
    if step == "media_video":
        return tr(lang, "Attach or reply with the video now. The media caption will be used as the post caption.", "ارفق الريلز أو اعمل رد بريلز دلوقتي. كابشن الميديا هيتستخدم ككابشن للمنشور.")
    if step == "media_image_all":
        return tr(lang, "Attach or reply with the image to post to every stored page. The media caption will be used as the caption.", "ارفق الصورة للنشر على كل الصفحات المحفوظة. كابشن الميديا هيتستخدم ككابشن.")
    if step == "media_video_all":
        return tr(lang, "Attach or reply with the video to post to every stored page. The media caption will be used as the caption.", "ارفق الريلز للنشر على كل الصفحات المحفوظة. كابشن الميديا هيتستخدم ككابشن.")
    return tr(lang, "Send the requested value.", "ابعت القيمة المطلوبة.")
