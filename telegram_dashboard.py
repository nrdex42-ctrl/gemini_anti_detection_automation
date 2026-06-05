"""Reply-keyboard dashboard helpers for the raw Telegram Bot API service."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional


BUTTON_DASHBOARD = "🏠 Dashboard"
BUTTON_ADD_ACCOUNT = "➕ Add Account"
BUTTON_ACCOUNTS = "👥 Accounts"
BUTTON_DISCOVER_PAGES = "🔎 Discover Pages"
BUTTON_LIST_PAGES = "📄 Stored Pages"
BUTTON_TEXT_POST = "📝 Text Post"
BUTTON_IMAGE_POST = "🖼 Image Post"
BUTTON_VIDEO_POST = "🎬 Video Post"
BUTTON_STATUS = "📊 Status"
BUTTON_CANCEL = "❌ Cancel"

DASHBOARD_ACTIONS = {
    BUTTON_DASHBOARD: "dashboard",
    "Dashboard": "dashboard",
    "menu": "dashboard",
    "Menu": "dashboard",
    "/dashboard": "dashboard",
    BUTTON_ADD_ACCOUNT: "add_account",
    BUTTON_ACCOUNTS: "accounts",
    BUTTON_DISCOVER_PAGES: "discover_pages",
    BUTTON_LIST_PAGES: "list_pages",
    BUTTON_TEXT_POST: "post_text",
    BUTTON_IMAGE_POST: "post_image",
    BUTTON_VIDEO_POST: "post_video",
    BUTTON_STATUS: "status",
    BUTTON_CANCEL: "cancel",
    "/cancel": "cancel",
}

POST_ACTION_TYPES = {
    "post_text": "text",
    "post_image": "image",
    "post_video": "video",
}


def dashboard_action(text: str) -> str:
    return DASHBOARD_ACTIONS.get((text or "").strip(), "")


def reply_keyboard(
    rows: List[List[str]],
    *,
    placeholder: str = "Choose an action",
    persistent: bool = True,
) -> Dict[str, Any]:
    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "is_persistent": persistent,
        "input_field_placeholder": placeholder[:64],
    }


def dashboard_markup(*, has_accounts: bool = False, active_jobs: int = 0) -> Dict[str, Any]:
    rows = [
        [BUTTON_TEXT_POST, BUTTON_IMAGE_POST],
        [BUTTON_VIDEO_POST, BUTTON_DISCOVER_PAGES],
        [BUTTON_ADD_ACCOUNT, BUTTON_ACCOUNTS],
        [BUTTON_LIST_PAGES, BUTTON_STATUS],
        [BUTTON_DASHBOARD],
    ]
    if not has_accounts:
        rows = [
            [BUTTON_ADD_ACCOUNT, BUTTON_ACCOUNTS],
            [BUTTON_STATUS, BUTTON_DASHBOARD],
        ]
    return reply_keyboard(rows, placeholder="Dashboard panel")


def cancel_markup() -> Dict[str, Any]:
    return reply_keyboard([[BUTTON_DASHBOARD, BUTTON_CANCEL]], placeholder="Send the requested value")


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
    rows.append([BUTTON_DASHBOARD, BUTTON_CANCEL])
    return reply_keyboard(rows, placeholder=placeholder, persistent=False)


def account_choice_label(account: Dict[str, Any]) -> str:
    account_id = str(account.get("account_id") or "").strip()
    label = str(account.get("label") or "").strip()
    status = "active" if account.get("active") else "inactive"
    if label and label != account_id:
        return f"{account_id} | {label} | {status}"
    return f"{account_id} | {status}"


def parse_choice_id(text: str) -> str:
    return (text or "").split("|", 1)[0].strip()


def page_choice_label(page: Dict[str, Any]) -> str:
    page_id = str(page.get("page_id") or "").strip()
    page_url = str(page.get("page_url") or "").strip()
    page_name = str(page.get("page_name") or page_id or page_url).strip()
    identity = page_id or page_url
    if page_name and page_name != identity:
        return f"{identity} | {page_name[:48]}"
    return identity


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
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def dashboard_text(
    *,
    accounts: List[Dict[str, Any]],
    summary: Optional[Dict[str, Any]] = None,
    prefix: str = "",
) -> str:
    summary = summary or {}
    status_counts = summary.get("job_status_counts") or {}
    active_jobs = int(status_counts.get("queued", 0)) + int(status_counts.get("processing", 0))
    locked_accounts = summary.get("locked_accounts") or []
    recent_jobs = summary.get("recent_jobs") or []

    lines: List[str] = []
    if prefix:
        lines.extend([prefix, ""])
    lines.extend(
        [
            "🎛 Smart Bot Dashboard",
            "━━━━━━━━━━━━━━━━━━",
            f"Accounts: {len(accounts)} total, {sum(1 for item in accounts if item.get('active'))} active",
            f"Stored pages: {int(summary.get('page_count') or 0)}",
            f"Jobs: {active_jobs} active, {int(status_counts.get('success', 0))} success, {int(status_counts.get('failed', 0))} failed",
        ]
    )

    if locked_accounts:
        lines.append("")
        lines.append("Account isolation:")
        for item in locked_accounts[:4]:
            lines.append(f"- {item.get('account_id')}: locked until {_format_dt(item.get('locked_until'))}")

    lines.append("")
    if accounts:
        lines.append("Accounts:")
        for item in accounts[:6]:
            status = "active" if item.get("active") else "inactive"
            lines.append(f"- {item.get('account_id')} ({status})")
        if len(accounts) > 6:
            lines.append(f"- ... {len(accounts) - 6} more")
    else:
        lines.append("No accounts yet. Use Add Account to start.")

    if recent_jobs:
        lines.append("")
        lines.append("Recent jobs:")
        for job in recent_jobs[:5]:
            lines.append(
                f"- {job.get('status')} {job.get('post_type')} -> {str(job.get('page_id_or_url') or '')[:36]}"
            )

    lines.extend(["", "Use the panel buttons below. You can still use slash commands anytime."])
    return "\n".join(lines)


def prompt_text(action: str, step: str = "") -> str:
    if action == "add_account":
        return (
            "Send the account cookie in one message.\n\n"
            "Accepted formats:\n"
            "- auto <raw_cookie>\n"
            "- <account_id> <raw_cookie>\n"
            "- <raw_cookie>  (account id will be read from c_user)"
        )
    if step == "account":
        return "Choose an account from the panel or type its account_id."
    if step == "page":
        return "Choose a stored page from the panel or type a page id / full page URL."
    if step == "caption":
        return "Send the post caption/text."
    if step == "media_image":
        return "Attach or reply with the image now. The media caption will be used as the post caption."
    if step == "media_video":
        return "Attach or reply with the video now. The media caption will be used as the post caption."
    return "Send the requested value."
