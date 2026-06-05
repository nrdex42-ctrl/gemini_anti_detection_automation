"""Telegram dashboard helpers for the raw Telegram Bot API service."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from page_name_utils import clean_facebook_page_name


BUTTON_ADD_ACCOUNT = "➕ Add Facebook Account"
BUTTON_POST_ACTIVE = "⚡ Post With Active Account"
BUTTON_QUICK_TEXT = "📝 Quick Text Post"
BUTTON_QUICK_IMAGE = "📸 Quick Image Post"
BUTTON_QUICK_VIDEO = "🎬 Quick Video Post"
BUTTON_POST_ALL_PAGES = "📋 Post to All Pages"
BUTTON_SWITCH_ACCOUNT = "🔁 Switch Active Account"
BUTTON_SELECT_ACCOUNT = "📱 Select Account & Post"
BUTTON_MY_ACCOUNTS = "👤 My Accounts"
BUTTON_CHECK_COOKIES = "🧪 Check All Cookies"
BUTTON_POST_HISTORY = "📊 Post History"
BUTTON_STATUS = "📊 Bot Status"
BUTTON_BACK = "⬅️ Back to Dashboard"
BUTTON_DASHBOARD = "🏠 Dashboard"
BUTTON_LANGUAGE = "🌐 Language"
BUTTON_DISCOVER_PAGES = "🔎 Discover Pages"
BUTTON_REFRESH_PAGES = "🔄 Refresh Pages"
BUTTON_LIST_PAGES = "📄 Stored Pages"
BUTTON_CHECK_THIS_ACCOUNT = "🧪 Check this account"
BUTTON_CONTINUE_TO_PAGES = "➡️ Continue to pages"
BUTTON_DONE = "✅ Done"
BUTTON_CANCEL = "❌ Cancel"
BUTTON_ADMIN = "🔒 Admin Dashboard"
BUTTON_USER_DASHBOARD = "🔁 User Dashboard"
BUTTON_SYSTEM_STATS = "📊 System Stats"
BUTTON_USERS = "👥 Users"
BUTTON_ADMIN_ACCOUNTS = "🔑 Accounts"
BUTTON_POST_STATS = "📈 Post Stats"
BUTTON_RUNTIME_LOCKS = "🔐 Runtime Locks"
BUTTON_SYSTEM_CONFIG = "⚙️ System Config"
BUTTON_DEBUG_SNAPSHOT = "🧰 Debug Snapshot"

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
}

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
    if "image" in normalized or "photo" in normalized:
        return "image"
    if "video" in normalized:
        return "video"
    if "text" in normalized or "caption" in normalized:
        return "text"
    return ""


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
) -> Dict[str, Any]:
    rows: List[List[str]] = []

    if active_jobs:
        rows.append(["⏳ Posting in progress..."])

    if posting_blocked and has_accounts and active_account:
        rows.append(["⏳ Posting cooldown active"])
        rows.append([BUTTON_SWITCH_ACCOUNT, BUTTON_REFRESH_PAGES])
    elif not has_accounts:
        rows.append([BUTTON_ADD_ACCOUNT])
    elif active_account:
        rows.append([BUTTON_POST_ACTIVE, BUTTON_SWITCH_ACCOUNT])
        rows.append([BUTTON_QUICK_TEXT, BUTTON_QUICK_IMAGE])
        rows.append([BUTTON_QUICK_VIDEO, BUTTON_POST_ALL_PAGES])
        rows.append([BUTTON_REFRESH_PAGES, BUTTON_LIST_PAGES])
    else:
        rows.append([BUTTON_SELECT_ACCOUNT, BUTTON_ADD_ACCOUNT])

    if has_accounts:
        rows.append([BUTTON_MY_ACCOUNTS, BUTTON_ADD_ACCOUNT])
        rows.append([BUTTON_CHECK_COOKIES, BUTTON_POST_HISTORY])
        if not active_account:
            rows.append([BUTTON_DISCOVER_PAGES, BUTTON_LIST_PAGES])

    rows.append([BUTTON_STATUS, BUTTON_LANGUAGE])
    if is_admin:
        rows.append([BUTTON_ADMIN])
    return reply_keyboard(rows)


def admin_dashboard_markup() -> Dict[str, Any]:
    return reply_keyboard(
        [
            [BUTTON_SYSTEM_STATS, BUTTON_POST_STATS],
            [BUTTON_USERS, BUTTON_ADMIN_ACCOUNTS],
            [BUTTON_RUNTIME_LOCKS, BUTTON_SYSTEM_CONFIG],
            [BUTTON_DEBUG_SNAPSHOT],
            [BUTTON_USER_DASHBOARD],
        ],
        placeholder="Choose an admin action...",
    )


def cancel_markup() -> Dict[str, Any]:
    return reply_keyboard([[BUTTON_BACK, BUTTON_CANCEL]], placeholder="Send the requested value")


def cookie_input_markup() -> Dict[str, Any]:
    return reply_keyboard([[BUTTON_DONE], [BUTTON_BACK, BUTTON_CANCEL]], placeholder="Paste cookies or upload JSON")


def account_post_action_markup() -> Dict[str, Any]:
    return reply_keyboard(
        [
            [BUTTON_CHECK_THIS_ACCOUNT],
            [BUTTON_CONTINUE_TO_PAGES, BUTTON_REFRESH_PAGES],
            [BUTTON_BACK],
        ],
        placeholder="Choose account action",
        persistent=False,
    )


def choices_markup(choices: Iterable[str], *, placeholder: str = "Choose or type a value") -> Dict[str, Any]:
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
    rows.append([BUTTON_BACK, BUTTON_CANCEL])
    return reply_keyboard(rows, placeholder=placeholder, persistent=False)


def inline_button(text: str, callback_data: str) -> Dict[str, str]:
    return {"text": text[:64], "callback_data": callback_data[:64]}


def inline_markup(rows: Sequence[Sequence[Dict[str, str]]]) -> Dict[str, Any]:
    return {"inline_keyboard": [list(row) for row in rows]}


def language_selection_markup() -> Dict[str, Any]:
    return inline_markup(
        [
            [inline_button("🇪🇬 Arabic (Egyptian)", "lang:ar")],
            [inline_button("🇬🇧 English", "lang:en")],
            [inline_button("⬅️ Back", "dash:back")],
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
        display = "Facebook Account" if label.startswith("Facebook Account ") else label
        return f"{display} ({account_id})" if include_id and account_id else display
    if include_id and account_id:
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
) -> str:
    selected = set(selected_indexes)
    lines: List[str] = []
    if prefix:
        lines.extend([prefix, ""])
    lines.extend(
        [
            "📄 Select Pages",
            "━━━━━━━━━━━━━━━━━━",
            f"Account: {_short(account_name or 'Facebook Account', 70)}",
            f"Available pages: {len(pages)}",
            f"Selected: {len(selected)}",
            "",
        ]
    )
    if not pages:
        lines.append("No cached pages found. Refresh pages first.")
    else:
        for idx, page in enumerate(pages[:10]):
            marker = "✅" if idx in selected else "⬜"
            lines.append(f"{marker} {page_display_name(page, idx)}")
        remaining = len(pages) - 10
        if remaining > 0:
            lines.append(f"... and {remaining} more")
    lines.extend(["", "Tap page names to toggle, then Confirm."])
    return "\n".join(lines)


def page_selection_markup(pages: List[Dict[str, Any]], selected_indexes: List[int]) -> Dict[str, Any]:
    selected = set(selected_indexes)
    rows: List[List[Dict[str, str]]] = []
    for idx, page in enumerate(pages[:24]):
        marker = "✅" if idx in selected else "⬜"
        rows.append([inline_button(f"{marker} {_short(page_display_name(page, idx), 48)}", f"pg:{idx}")])
    if pages:
        rows.append([inline_button("📋 All", "pg:all"), inline_button("✅ Confirm", "pg:confirm")])
    rows.append([inline_button("🔄 Refresh Pages", "pg:refresh")])
    rows.append([inline_button("⬅️ Dashboard", "dash:back")])
    return inline_markup(rows)


def post_type_card(*, account_name: str, pages: List[Dict[str, Any]], selected_indexes: List[int]) -> str:
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
            "📝 Choose Post Type",
            "━━━━━━━━━━━━━━━━━━",
            f"Account: {_short(account_name or 'Facebook Account', 70)}",
            f"Pages: {len(page_names)}",
            f"Selected: {preview or 'none'}",
            "",
            "Choose what you want to post.",
        ]
    )


def post_type_inline_markup() -> Dict[str, Any]:
    return inline_markup(
        [
            [inline_button("📝 Caption/Text", "post:type:text")],
            [inline_button("📸 Image", "post:type:image"), inline_button("🎬 Video", "post:type:video")],
            [inline_button("⬅️ Pages", "post:pages"), inline_button("🏠 Dashboard", "dash:back")],
        ]
    )


def video_mode_card(*, account_name: str, pages: List[Dict[str, Any]], selected_indexes: List[int]) -> str:
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
            "🎬 Video Posting Mode",
            "━━━━━━━━━━━━━━━━━━",
            f"Account: {_short(account_name or 'Facebook Account', 70)}",
            f"Pages: {len(page_names)}",
            f"Selected: {preview or 'none'}",
            "",
            "Choose how videos should be attached.",
        ]
    )


def video_mode_inline_markup() -> Dict[str, Any]:
    return inline_markup(
        [
            [inline_button("📄 Single video upload → all pages", "video:single_upload")],
            [inline_button("📚 Multi videos upload → one per page", "video:multi_upload")],
            [inline_button("🔗 Single video URL → all pages", "video:single_url")],
            [inline_button("🔗 Multi video URLs → one per page", "video:multi_url")],
            [inline_button("⬅️ Pages", "post:pages"), inline_button("🏠 Dashboard", "dash:back")],
        ]
    )


def post_input_card(post_type: str) -> str:
    if post_type == "text":
        action = "Send the caption/text message now."
        title = "📝 Caption Post"
    elif post_type == "image":
        action = "Send or reply with the image now. Telegram media caption is optional."
        title = "📸 Image Post"
    else:
        action = "Send or reply with the video now. Telegram media caption is optional."
        title = "🎬 Video Post"
    return "\n".join([title, "━━━━━━━━━━━━━━━━━━", action, "", "After that, I will show a final review card."])


def post_review_card(
    *,
    account_name: str,
    pages: List[Dict[str, Any]],
    post_type: str,
    caption: str,
    media_path: str = "",
    multi_media_count: int = 0,
    multi_caption_count: int = 0,
) -> str:
    if multi_caption_count and not caption:
        caption_value = f"{multi_caption_count} attached (one per page)"
    else:
        caption_value = _short(caption, 700) if caption else "(none)"
    media_value = f"{multi_media_count} attached (one per page)" if multi_media_count else ("attached" if media_path else "(none)")
    lines = [
        "🧾 Review Post",
        "━━━━━━━━━━━━━━━━━━",
        f"Type: {post_type}",
        f"Account: {_short(account_name or 'Facebook Account', 70)}",
        f"Selected pages: {len(pages)}",
    ]
    for idx, page in enumerate(pages[:6]):
        lines.append(f"• {page_display_name(page, idx)}")
    remaining = len(pages) - 6
    if remaining > 0:
        lines.append(f"... and {remaining} more")
    lines.extend(["", f"Caption: {caption_value}"])
    if post_type in {"image", "video"}:
        lines.append(f"Media: {media_value}")
    lines.extend(["", "Confirm to start posting, or edit the caption."])
    return "\n".join(lines)


def post_confirm_inline_markup() -> Dict[str, Any]:
    return inline_markup(
        [
            [inline_button("✏️ Edit Caption", "post:edit_caption")],
            [inline_button("✅ Post Now", "post:confirm")],
            [inline_button("⬅️ Pages", "post:pages"), inline_button("🏠 Dashboard", "dash:back")],
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
    display_tz = timezone(timedelta(hours=3), "UTC+3")
    return dt.astimezone(display_tz).strftime("%Y-%m-%d %H:%M UTC+3")


def _account_health_icon(account: Dict[str, Any], active_account: str) -> str:
    if str(account.get("cookie_status") or "unverified").lower() == "valid":
        return "🟢"
    return "🔴"


def _account_cookie_status_label(account: Dict[str, Any]) -> str:
    status = str(account.get("cookie_status") or "unverified").strip().lower()
    if status == "valid":
        return "valid"
    if status == "invalid":
        return "invalid"
    return "not verified"


def dashboard_text(
    *,
    accounts: List[Dict[str, Any]],
    summary: Optional[Dict[str, Any]] = None,
    active_account: str = "",
    prefix: str = "",
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
    active_text = account_display_name(account_by_id.get(active_account, {}), active_account) if active_account else "not selected"
    lines.extend(
        [
            "🎛️ Smart Dashboard",
            "━━━━━━━━━━━━━━━━━━",
            f"Accounts: {len(accounts)} total",
            f"Active account: {active_text}",
            f"Stored pages: {int(summary.get('page_count') or 0)}",
            f"Jobs: {active_jobs} active, {int(status_counts.get('success', 0))} success, {int(status_counts.get('failed', 0))} failed",
        ]
    )

    if not accounts:
        lines.extend(["", "No accounts yet. Tap Add Facebook Account and paste a raw cookie string or JSON export."])
    elif not active_account:
        lines.extend(["", "No active account selected. Tap Select Account & Post or Switch Active Account."])
    else:
        lines.extend(["", "Available accounts:"])
        for account in accounts[:6]:
            account_id = str(account.get("account_id") or "")
            icon = _account_health_icon(account, active_account)
            pages = int(page_counts.get(account_id, 0))
            lines.append(f"{icon} {account_display_name(account)} | pages: {pages} | cookies: {_account_cookie_status_label(account)}")
        if len(accounts) > 6:
            lines.append(f"... {len(accounts) - 6} more")

    if locked_accounts:
        lines.append("")
        lines.append("Session safety:")
        for item in locked_accounts[:4]:
            locked_id = str(item.get("account_id") or "")
            locked_name = account_display_name(account_by_id.get(locked_id, {}), locked_id)
            lines.append(f"- {locked_name}: locked until {_format_dt(item.get('locked_until'))}")

    if recent_jobs:
        lines.append("")
        lines.append("Recent posts:")
        for job in recent_jobs[:5]:
            target = clean_facebook_page_name(
                job.get("page_name"),
                str(job.get("page_id_or_url") or ""),
                str(job.get("page_id_or_url") or ""),
            )[:36]
            lines.append(
                f"- {job.get('status')} {job.get('post_type')} -> {target}"
            )

    lines.extend(["", "Use the keyboard buttons below. /start refreshes this dashboard."])
    return "\n".join(lines)


def prompt_text(action: str, step: str = "") -> str:
    if action == "add_account":
        return (
            "Send Facebook session cookies.\n\n"
            "Accepted formats:\n"
            "1. Raw cookie string in one message.\n"
            "2. Exported JSON file or JSON text.\n"
            "3. Long JSON across multiple messages, then tap Done."
        )
    if step == "account":
        return "Choose an account from the keyboard."
    if step == "post_type":
        return "Choose the post type."
    if step == "page":
        return "Choose a stored page or type a page id / full page URL."
    if step == "caption":
        return "Send the post caption/text."
    if step == "caption_all":
        return "Send the caption/text to post to every stored page for this account."
    if step == "media_image":
        return "Attach or reply with the image now. The media caption will be used as the post caption."
    if step == "media_video":
        return "Attach or reply with the video now. The media caption will be used as the post caption."
    if step == "media_image_all":
        return "Attach or reply with the image to post to every stored page. The media caption will be used as the caption."
    if step == "media_video_all":
        return "Attach or reply with the video to post to every stored page. The media caption will be used as the caption."
    return "Send the requested value."
