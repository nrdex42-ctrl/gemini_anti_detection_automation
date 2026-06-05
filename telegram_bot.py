#!/usr/bin/env python3
"""Telegram webhook service for Render deployment."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import platform
import re
import sys
import time
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from aiohttp import ClientSession, web

from bot_storage import BotStorage
from facebook_cookie_parser import parse_account_cookie_payload
from run_live_image_test import parse_cookies
from run_live_matrix_test import cookies_json, discover_pages_from_browser
from telegram_dashboard import (
    POST_TYPE_CHOICES,
    POST_ACTION_TYPES,
    BUTTON_DONE,
    account_display_name,
    account_choice_label,
    account_post_action_markup,
    admin_dashboard_markup,
    cancel_markup,
    choices_markup,
    cookie_input_markup,
    dashboard_action,
    dashboard_markup,
    dashboard_text,
    page_display_name,
    page_selection_card,
    page_selection_markup,
    parse_choice_id,
    parse_post_type_choice,
    post_confirm_inline_markup,
    post_input_card,
    post_review_card,
    post_type_card,
    post_type_inline_markup,
    prompt_text,
    video_mode_card,
    video_mode_inline_markup,
)

logger = logging.getLogger("telegram_bot")

POST_TYPES = {"text", "image", "video"}
UPLOAD_DIR = Path(os.getenv("TELEGRAM_UPLOAD_DIR", "artifacts/telegram_uploads"))


def progress_bar(done: int, total: int, width: int = 10) -> str:
    total = max(1, total)
    filled = min(width, max(0, round((done / total) * width)))
    return "█" * filled + "░" * (width - filled)


def progress_card(title: str, done: int, total: int, status: str) -> str:
    percent = int((done / max(1, total)) * 100)
    return f"{title}\n{progress_bar(done, total)} {done}/{total} ({percent}%)\n{status}"


def compact_text(value: Any, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[:max(0, limit - 1)].rstrip()}…"


def page_status_bar(status: str, width: int = 5) -> str:
    normalized = str(status or "pending").lower()
    if normalized in {"success", "failed", "skipped"}:
        done = width
    elif normalized in {"running", "processing"}:
        done = max(1, width // 2)
    else:
        done = 0
    return "█" * done + "░" * (width - done)


def posting_result_card(
    results: List[Dict[str, Any]],
    *,
    title: str = "",
    debug_id: str = "",
    max_length: int = 3800,
) -> str:
    total = max(1, len(results))
    success_count = sum(1 for item in results if bool(item.get("success")))
    header = title or f"Posting complete: {success_count}/{len(results)} succeeded"
    lines = [header, f"{progress_bar(success_count, total)} {success_count}/{len(results)} succeeded"]
    if debug_id:
        lines.append(f"Debug ID: {debug_id}")
    lines.append("")

    omitted = 0
    for index, item in enumerate(results):
        success = bool(item.get("success"))
        page = compact_text(item.get("page") or "Unknown page", 42)
        detail = compact_text(item.get("result") or item.get("error") or "", 260)
        prefix = "✅" if success else "❌"
        label = "Result" if success else "Error"
        status = "success" if success else "failed"
        line = f"{prefix} {page} {page_status_bar(status)} {label}: {detail}"
        projected = "\n".join([*lines, line])
        reserve = 80 if index < len(results) - 1 else 0
        if len(projected) + reserve > max_length:
            omitted = len(results) - index
            break
        lines.append(line)

    if omitted:
        lines.append(f"… {omitted} more result(s) omitted to keep the Telegram message deliverable.")
    return "\n".join(lines)


def posting_live_status_card(
    title: str,
    jobs: List[Dict[str, str]],
    statuses: Dict[str, Dict[str, Any]],
    *,
    debug_id: str = "",
    active_detail: str = "",
    max_rows: int = 12,
) -> str:
    total = max(1, len(jobs))
    done = sum(1 for state in statuses.values() if str(state.get("status") or "") in {"success", "failed", "skipped"})
    lines = [progress_card(title, done, total, active_detail or "Posting pages...")]
    if debug_id:
        lines.append(f"Debug ID: {debug_id}")
    lines.append("")
    for index, job in enumerate(jobs[:max_rows]):
        page = compact_text(job.get("page_name") or job.get("page_id_or_url") or f"Page {index + 1}", 34)
        state = statuses.get(str(job.get("job_id") or "")) or {}
        status = str(state.get("status") or "pending")
        stage = compact_text(state.get("stage") or status.title(), 72)
        icon = {
            "success": "✅",
            "failed": "❌",
            "skipped": "⏭",
            "running": "⏳",
            "processing": "⏳",
            "pending": "⬜",
        }.get(status, "⏳")
        lines.append(f"{icon} {page} {page_status_bar(status)} {stage}")
    if len(jobs) > max_rows:
        lines.append(f"… {len(jobs) - max_rows} more page(s)")
    return "\n".join(lines)


def new_debug_id(prefix: str) -> str:
    safe_prefix = re.sub(r"[^a-z0-9_-]+", "", (prefix or "dbg").lower())[:10] or "dbg"
    return f"{safe_prefix}_{uuid.uuid4().hex[:10]}"


def _debug_safe(value: Any, limit: int = 220) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [_debug_safe(item, limit) for item in list(value)[:10]]
    if isinstance(value, dict):
        return {
            str(key)[:60]: _debug_safe(item, limit)
            for key, item in list(value.items())[:20]
            if str(key).lower() not in {"cookie", "cookies", "cookie_header", "xs", "fr", "datr", "sb", "media_path"}
        }
    text = " ".join(str(value or "").split())
    return text[:limit]


def diagnostic_path_from_text(text: Any) -> str:
    raw = str(text or "")
    marker = "Diagnostic:"
    if marker not in raw:
        return ""
    return raw.split(marker, 1)[1].strip().splitlines()[0].strip()[:220]


def cookie_validation_summary(session_ok: bool, detail: str, max_length: int = 180) -> Tuple[str, str]:
    compact_detail = " ".join(str(detail or "").split())
    if session_ok:
        return "🟢", "Facebook session is valid"
    detail_lower = compact_detail.lower()
    icon = "🟡" if "inconclusive" in detail_lower or "skipped" in detail_lower else "🔴"
    return icon, compact_detail[:max_length] or "Facebook session is not valid"


def compact_error(exc: BaseException, max_length: int = 700) -> str:
    detail = " ".join(str(exc or "").split())
    if not detail:
        detail = exc.__class__.__name__
    return detail[:max_length]


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        logger.warning("Invalid %s=%r; using %d", name, raw, default)
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        logger.warning("Invalid %s=%r; using %.2f", name, raw, default)
        return default


def parse_video_media_url(text: str) -> Tuple[bool, str]:
    raw = (text or "").strip()
    if not raw.startswith(("http://", "https://")):
        return False, "Send a direct video link starting with https:// or http://."
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False, "That does not look like a valid video URL."
    if len(raw) > 2048:
        return False, "Video URL is too long."
    return True, raw


def probe_video_media_url(url: str) -> Tuple[bool, str, Optional[int]]:
    import requests

    timeout = _env_int("BOT_VIDEO_URL_PROBE_TIMEOUT_SECONDS", 12, minimum=3)
    max_bytes = _env_int("MAX_MEDIA_BYTES", 50 * 1024 * 1024, minimum=1024 * 1024)
    try:
        response = requests.head(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; FBAutomationBot/1.0)"},
        )
        if response.status_code >= 400:
            return False, f"URL returned HTTP {response.status_code}. Use a direct video file link.", None
        content_length = response.headers.get("Content-Length")
        size = int(content_length) if content_length and content_length.isdigit() else None
        if size and size > max_bytes:
            return False, f"The video URL is about {size} bytes; bot limit is {max_bytes} bytes.", size
        return True, "", size
    except Exception:
        return True, "", None


def _csv_ints(name: str) -> set[int]:
    values = set()
    for part in os.getenv(name, "").split(","):
        part = part.strip()
        if part.isdigit():
            values.add(int(part))
    return values


def _placeholder(value: str) -> bool:
    normalized = (value or "").strip().lower()
    return (
        not normalized
        or "replace_me" in normalized
        or "replace-me" in normalized
        or normalized.startswith("your-")
    )


class HealthzAccessLogFilter(logging.Filter):
    """Suppress noisy Render health-check access log lines while keeping other access logs."""

    _healthz_pattern = re.compile(r'"(?:GET|HEAD) /healthz(?:\?[^ ]*)? HTTP/')

    def filter(self, record: logging.LogRecord) -> bool:
        return not self._healthz_pattern.search(record.getMessage())


def configure_access_log_filters() -> None:
    if not _env_bool("SUPPRESS_HEALTHZ_ACCESS_LOGS", True):
        return
    access_logger = logging.getLogger("aiohttp.access")
    if any(isinstance(item, HealthzAccessLogFilter) for item in access_logger.filters):
        return
    access_logger.addFilter(HealthzAccessLogFilter())


def telegram_safe_webhook_secret(secret: str) -> str:
    raw = (secret or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{1,256}", raw):
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def split_command(text: str) -> Tuple[str, List[str]]:
    parts = (text or "").strip().split()
    if not parts:
        return "", []
    command = parts[0].split("@", 1)[0].lower()
    return command, parts[1:]


def help_text() -> str:
    return (
        "Use /start to open the dashboard.\n\n"
        "All account, page, posting, and admin actions are available from the typing-area dashboard buttons."
    )


def seconds_since(value: Any) -> float:
    if not value:
        return float("inf")
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - value.astimezone(timezone.utc)).total_seconds())
    return float("inf")


class TelegramBotApp:
    def __init__(self) -> None:
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        raw_webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
        self.webhook_secret = telegram_safe_webhook_secret(raw_webhook_secret)
        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
        if not raw_webhook_secret:
            raise RuntimeError("TELEGRAM_WEBHOOK_SECRET is required")
        if self.webhook_secret != raw_webhook_secret:
            logger.info("Telegram webhook secret normalized to a Bot API-safe token")
        self.admin_ids = _csv_ints("BOT_ADMIN_IDS")
        self.storage = BotStorage.from_env()
        self.session: Optional[ClientSession] = None
        self.api_base = f"https://api.telegram.org/bot{self.token}"
        self.dashboard_sessions: Dict[str, Dict[str, Any]] = {}
        self.account_name_lookup_tasks: set[str] = set()
        self.background_tasks: set[asyncio.Task[Any]] = set()
        self.background_task_labels: Dict[asyncio.Task[Any], str] = {}
        self.started_at = time.monotonic()
        self.started_wall_at = datetime.now(timezone.utc)

    def is_admin_user(self, user_id: int) -> bool:
        return bool(self.admin_ids and int(user_id or 0) in self.admin_ids)

    def account_owner_scope(self, user_id: int) -> Optional[int]:
        if self.is_admin_user(user_id):
            return None
        return int(user_id or 0)

    def debug_event(self, event: str, trace_id: str = "", **fields: Any) -> None:
        safe_fields = {key: _debug_safe(value) for key, value in fields.items()}
        logger.info(
            "BOT_DEBUG event=%s trace_id=%s fields=%s",
            event,
            trace_id or "-",
            json.dumps(safe_fields, ensure_ascii=False, sort_keys=True),
        )

    async def startup(self, app: web.Application) -> None:
        self.session = ClientSession()
        if _env_bool("AUTO_INIT_DB", True):
            await asyncio.to_thread(self.storage.ensure_schema)
            logger.info("Supabase/Postgres schema ready")
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        if _env_bool("AUTO_SET_TELEGRAM_WEBHOOK", True):
            await self.configure_telegram_webhook()
        if _env_bool("RESTART_BROADCAST_ENABLED", True):
            self.start_background_task(self.notify_restart_dashboard(), "restart dashboard broadcast")

    async def cleanup(self, app: web.Application) -> None:
        for task in list(self.background_tasks):
            task.cancel()
        if self.background_tasks:
            await asyncio.gather(*self.background_tasks, return_exceptions=True)
        self.background_task_labels.clear()
        if self.session is not None:
            await self.session.close()

    def authorized(self, update: Dict[str, Any]) -> bool:
        return True

    async def telegram_api(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self.session is None:
            raise RuntimeError("HTTP session is not ready")
        timeout_seconds = _env_int("BOT_TELEGRAM_API_TIMEOUT_SECONDS", 30, minimum=5)
        async with self.session.post(f"{self.api_base}/{method}", json=payload, timeout=timeout_seconds) as resp:
            data = await resp.json(content_type=None)
            if not data.get("ok"):
                logger.warning(
                    "Telegram API %s failed chat_id=%s message_id=%s reply_to=%s has_markup=%s: %s",
                    method,
                    payload.get("chat_id", "-"),
                    payload.get("message_id", "-"),
                    payload.get("reply_to_message_id", "-"),
                    bool(payload.get("reply_markup")),
                    data,
                )
            return data

    async def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        payload: Dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:200]
            payload["show_alert"] = False
        await self.telegram_api("answerCallbackQuery", payload)

    async def configure_telegram_webhook(self) -> None:
        public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
        if _placeholder(public_base_url):
            logger.warning("Skipping Telegram webhook setup because PUBLIC_BASE_URL is not configured")
            return
        if _placeholder(self.token):
            logger.warning("Skipping Telegram webhook setup because TELEGRAM_BOT_TOKEN is not configured")
            return
        if not self.webhook_secret:
            logger.warning("Skipping Telegram webhook setup because TELEGRAM_WEBHOOK_SECRET is not configured")
            return

        webhook_url = f"{public_base_url}/telegram/webhook"
        payload = {
            "url": webhook_url,
            "secret_token": self.webhook_secret,
            "drop_pending_updates": _env_bool("TELEGRAM_DROP_PENDING_UPDATES", False),
            "allowed_updates": ["message", "edited_message", "callback_query"],
        }
        data = await self.telegram_api("setWebhook", payload)
        if data.get("ok"):
            logger.info("Telegram webhook configured for %s", webhook_url)
        else:
            logger.error("Telegram webhook setup failed: %s", data)

    def deploy_revision(self) -> str:
        for name in ("RENDER_DEPLOY_ID", "RENDER_DEPLOYMENT_ID", "BOT_RESTART_BROADCAST_REVISION", "RENDER_GIT_COMMIT"):
            value = os.getenv(name, "").strip()
            if value:
                return f"{name}:{value}"
        return ""

    async def notify_restart_dashboard(self) -> None:
        revision = self.deploy_revision()
        if not revision:
            logger.info("Restart dashboard broadcast skipped; no deploy revision env var found")
            return
        marker_key = "last_restart_broadcast_revision"
        try:
            previous = await asyncio.to_thread(self.storage.get_meta, marker_key)
            if previous == revision:
                logger.info("Restart dashboard broadcast skipped; already sent for %s", revision)
                return
            targets = await asyncio.to_thread(self.storage.list_restart_targets)
            if not targets:
                await asyncio.to_thread(self.storage.set_meta, marker_key, revision)
                logger.info("Restart dashboard broadcast skipped; no known users")
                return
            sent = 0
            for target in targets:
                user_id = int(target.get("telegram_user_id") or 0)
                chat_id = int(target.get("chat_id") or user_id or 0)
                if not chat_id or not user_id:
                    continue
                owner_scope = self.account_owner_scope(user_id)
                summary = await asyncio.to_thread(self.storage.dashboard_summary, owner_scope)
                accounts = await asyncio.to_thread(self.storage.list_accounts, owner_scope)
                active_account = await asyncio.to_thread(self.storage.get_active_account, user_id, owner_scope)
                status_counts = summary.get("job_status_counts") or {}
                active_jobs = int(status_counts.get("queued", 0)) + int(status_counts.get("processing", 0))
                text = dashboard_text(
                    accounts=accounts,
                    summary=summary,
                    active_account=active_account,
                    prefix="🔄 Bot updated after a new deploy. Dashboard refreshed.",
                )
                await self.send_message(
                    chat_id,
                    text,
                    reply_markup=dashboard_markup(
                        has_accounts=bool(accounts),
                        active_account=active_account,
                        active_jobs=active_jobs,
                        is_admin=self.is_admin_user(user_id),
                    ),
                )
                sent += 1
            await asyncio.to_thread(self.storage.set_meta, marker_key, revision)
            logger.info("Restart dashboard broadcast sent to %d user(s)", sent)
        except Exception:
            logger.exception("Restart dashboard broadcast failed")

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int = 0,
        *,
        reply_markup: Optional[Dict[str, Any]] = None,
        parse_mode: str = "",
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:3900],
            "disable_web_page_preview": True,
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return await self.telegram_api("sendMessage", payload)

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: Optional[Dict[str, Any]] = None,
        parse_mode: str = "",
    ) -> None:
        if not message_id:
            return
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:3900],
            "disable_web_page_preview": True,
        }
        if reply_markup and "inline_keyboard" in reply_markup:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        data = await self.telegram_api("editMessageText", payload)
        if not data.get("ok"):
            description = str(data.get("description") or data)
            raise RuntimeError(description)

    async def try_edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: Optional[Dict[str, Any]] = None,
        parse_mode: str = "",
        timeout_seconds: int = 12,
    ) -> bool:
        try:
            await asyncio.wait_for(
                self.edit_message(
                    chat_id,
                    message_id,
                    text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                ),
                timeout=max(3, timeout_seconds),
            )
            return True
        except Exception as exc:
            logger.warning(
                "Telegram editMessageText failed or timed out chat_id=%s message_id=%s: %s",
                chat_id,
                message_id,
                compact_error(exc, 300),
            )
            return False

    async def edit_or_send_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_to_message_id: int = 0,
        reply_markup: Optional[Dict[str, Any]] = None,
        parse_mode: str = "",
        timeout_seconds: int = 12,
    ) -> int:
        if message_id and await self.try_edit_message(
            chat_id,
            message_id,
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            timeout_seconds=timeout_seconds,
        ):
            return message_id
        sent = await self.send_message(
            chat_id,
            text,
            reply_to_message_id,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
        return int((sent.get("result") or {}).get("message_id") or 0)

    def start_background_task(self, coro: Awaitable[Any], label: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self.background_tasks.add(task)
        self.background_task_labels[task] = label
        self.debug_event(
            "background_task_scheduled",
            new_debug_id("task"),
            label=label,
            active_background_tasks=len(self.background_tasks),
        )

        def _log_done(done_task: asyncio.Task[Any]) -> None:
            self.background_tasks.discard(done_task)
            self.background_task_labels.pop(done_task, None)
            try:
                exc = done_task.exception()
            except asyncio.CancelledError:
                self.debug_event(
                    "background_task_cancelled",
                    new_debug_id("task"),
                    label=label,
                    active_background_tasks=len(self.background_tasks),
                )
                return
            if exc:
                self.debug_event(
                    "background_task_failed",
                    new_debug_id("task"),
                    label=label,
                    error=compact_error(exc),
                    active_background_tasks=len(self.background_tasks),
                )
                logger.error("Background task failed: %s", label, exc_info=(type(exc), exc, exc.__traceback__))
            else:
                self.debug_event(
                    "background_task_complete",
                    new_debug_id("task"),
                    label=label,
                    active_background_tasks=len(self.background_tasks),
                )

        task.add_done_callback(_log_done)
        return task

    async def wait_for_task_with_heartbeat(
        self,
        task: asyncio.Task[Any],
        *,
        timeout_seconds: int,
        heartbeat_seconds: int,
        on_tick: Callable[[int], Awaitable[None]],
        timeout_message: str,
    ) -> Any:
        started = time.monotonic()
        while True:
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                raise TimeoutError(timeout_message)
            try:
                return await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=min(max(1, heartbeat_seconds), remaining),
                )
            except asyncio.TimeoutError:
                await on_tick(int(time.monotonic() - started))

    def browser_progress_text(self, title: str, event: Dict[str, Any], fallback_total: int, base_done: int = 0) -> str:
        page_total = int(event.get("total") or fallback_total or 1)
        page_done = int(event.get("done") or 0)
        if event.get("completed") and page_done <= 0:
            page_done = 1
        total = max(1, base_done + max(page_total, 1))
        done = min(max(base_done + page_done, 0), total)
        stage = str(event.get("stage") or "Working").strip()
        page = str(event.get("page") or "").strip()
        detail = str(event.get("detail") or "").strip()
        result = str(event.get("result") or event.get("status") or event.get("error") or "").strip()
        post_type = str(event.get("post_type") or "").strip()

        lines = [stage]
        if page:
            lines.append(f"Page: {page[:120]}")
        if post_type:
            lines.append(f"Type: {post_type}")
        if detail:
            lines.append(detail[:600])
        elif result:
            lines.append(result[:600])
        return progress_card(title, done, total, "\n".join(lines))

    def browser_progress_callback(
        self,
        chat_id: int,
        message_id: int,
        user_id: int,
        title: str,
        fallback_total: int,
        reply_markup: Dict[str, Any],
        base_done: int = 0,
    ) -> Callable[[Dict[str, Any]], Awaitable[None]]:
        current_message_id = message_id
        last_edit_at = 0.0
        last_text = ""
        edit_lock = asyncio.Lock()
        try:
            min_interval = max(0.5, float(os.getenv("BOT_PROGRESS_EDIT_MIN_SECONDS", "1.5") or "1.5"))
        except ValueError:
            min_interval = 1.5

        async def callback(event: Dict[str, Any]) -> None:
            nonlocal current_message_id, last_edit_at, last_text
            if not current_message_id:
                return
            text = self.browser_progress_text(title, event, fallback_total, base_done)
            completed = bool(event.get("completed"))
            now = time.monotonic()
            if text == last_text or (not completed and now - last_edit_at < min_interval):
                return
            async with edit_lock:
                now = time.monotonic()
                if text == last_text or (not completed and now - last_edit_at < min_interval):
                    return
                current_message_id = await self.edit_or_send_message(
                    chat_id,
                    current_message_id,
                    text,
                    reply_markup=reply_markup,
                )
                last_text = text
                last_edit_at = now

        return callback

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        await self.telegram_api("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    async def download_file(self, file_id: str, account_id: str) -> str:
        if self.session is None:
            raise RuntimeError("HTTP session is not ready")
        file_info = await self.telegram_api("getFile", {"file_id": file_id})
        file_path = str((file_info.get("result") or {}).get("file_path") or "")
        if not file_path:
            raise RuntimeError("Telegram did not return a downloadable file path")
        suffix = Path(file_path).suffix or ".bin"
        safe_account = re.sub(r"[^A-Za-z0-9_.-]+", "_", account_id)[:80]
        target_dir = UPLOAD_DIR / safe_account
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{Path(file_path).stem}_{uuid.uuid4().hex[:12]}{suffix}"
        async with self.session.get(f"https://api.telegram.org/file/bot{self.token}/{file_path}", timeout=180) as resp:
            resp.raise_for_status()
            target_path.write_bytes(await resp.read())
        return str(target_path)

    async def download_file_bytes(self, file_id: str, max_bytes: int) -> bytes:
        if self.session is None:
            raise RuntimeError("HTTP session is not ready")
        file_info = await self.telegram_api("getFile", {"file_id": file_id})
        file_path = str((file_info.get("result") or {}).get("file_path") or "")
        file_size = int((file_info.get("result") or {}).get("file_size") or 0)
        if not file_path:
            raise RuntimeError("Telegram did not return a downloadable file path")
        if file_size and file_size > max_bytes:
            raise RuntimeError(f"Cookie file is too large: {file_size} bytes")
        async with self.session.get(f"https://api.telegram.org/file/bot{self.token}/{file_path}", timeout=60) as resp:
            resp.raise_for_status()
            data = await resp.read()
        if len(data) > max_bytes:
            raise RuntimeError(f"Cookie file is too large: {len(data)} bytes")
        return data

    def extract_media_file_id(self, message: Dict[str, Any], post_type: str) -> str:
        source = message
        if message.get("reply_to_message"):
            source = message["reply_to_message"]
        if post_type == "image":
            photos = source.get("photo") or []
            if photos:
                return str(photos[-1]["file_id"])
            document = source.get("document") or {}
            mime = str(document.get("mime_type") or "")
            if mime.startswith("image/"):
                return str(document.get("file_id") or "")
        if post_type == "video":
            video = source.get("video") or {}
            if video.get("file_id"):
                return str(video["file_id"])
            document = source.get("document") or {}
            mime = str(document.get("mime_type") or "")
            if mime.startswith("video/"):
                return str(document.get("file_id") or "")
        return ""

    def extract_document_file_id(self, message: Dict[str, Any]) -> str:
        source = message
        if message.get("reply_to_message"):
            source = message["reply_to_message"]
        document = source.get("document") or {}
        return str(document.get("file_id") or "")

    async def cookie_payload_from_message(self, message: Dict[str, Any], text: str) -> Tuple[str, List[int]]:
        message_ids = [int(message.get("message_id") or 0)]
        document_file_id = self.extract_document_file_id(message)
        if document_file_id:
            max_bytes = _env_int("BOT_COOKIE_FILE_MAX_BYTES", 2 * 1024 * 1024, minimum=1024)
            payload = (await self.download_file_bytes(document_file_id, max_bytes)).decode("utf-8")
            reply = message.get("reply_to_message") or {}
            reply_message_id = int(reply.get("message_id") or 0)
            if reply_message_id:
                message_ids.append(reply_message_id)
            return payload, [mid for mid in message_ids if mid]

        payload = (text or "").strip()
        if payload:
            return payload, [mid for mid in message_ids if mid]

        reply = message.get("reply_to_message") or {}
        reply_payload = str(reply.get("text") or reply.get("caption") or "").strip()
        reply_message_id = int(reply.get("message_id") or 0)
        if reply_message_id:
            message_ids.append(reply_message_id)
        return reply_payload, [mid for mid in message_ids if mid]

    def dashboard_session_key(self, chat_id: int, user_id: int) -> str:
        return f"{chat_id}:{user_id}"

    def set_dashboard_session(self, chat_id: int, user_id: int, data: Dict[str, Any]) -> None:
        data["updated_at"] = time.time()
        self.dashboard_sessions[self.dashboard_session_key(chat_id, user_id)] = data

    def get_dashboard_session(self, chat_id: int, user_id: int) -> Dict[str, Any]:
        key = self.dashboard_session_key(chat_id, user_id)
        session = self.dashboard_sessions.get(key) or {}
        max_age = _env_int("BOT_DASHBOARD_SESSION_TTL_SECONDS", 1800, minimum=60)
        if session and time.time() - float(session.get("updated_at") or 0) > max_age:
            self.dashboard_sessions.pop(key, None)
            return {}
        return session

    def clear_dashboard_session(self, chat_id: int, user_id: int) -> None:
        self.dashboard_sessions.pop(self.dashboard_session_key(chat_id, user_id), None)

    async def touch_user_seen(self, user_id: int, chat_id: int) -> None:
        try:
            await asyncio.to_thread(self.storage.touch_user, user_id, chat_id)
        except Exception:
            logger.warning("Could not update Telegram user state", exc_info=True)

    async def dashboard_accounts(self, user_id: int = 0) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.storage.list_accounts, self.account_owner_scope(user_id))

    async def dashboard_summary(self, user_id: int = 0) -> Dict[str, Any]:
        return await asyncio.to_thread(self.storage.dashboard_summary, self.account_owner_scope(user_id))

    async def active_account_id(self, user_id: int) -> str:
        return await asyncio.to_thread(self.storage.get_active_account, user_id, self.account_owner_scope(user_id))

    async def dashboard_state(self, user_id: int = 0) -> Tuple[List[Dict[str, Any]], Dict[str, Any], str]:
        owner_scope = self.account_owner_scope(user_id)
        tasks: List[Awaitable[Any]] = [
            asyncio.to_thread(self.storage.list_accounts, owner_scope),
            asyncio.to_thread(self.storage.dashboard_summary, owner_scope),
        ]
        if user_id:
            tasks.append(asyncio.to_thread(self.storage.get_active_account, user_id, owner_scope))
        results = await asyncio.gather(*tasks)
        accounts = results[0]
        summary = results[1]
        active_account = str(results[2] or "") if user_id and len(results) > 2 else ""
        return accounts, summary, active_account

    def account_label_needs_refresh(self, account: Dict[str, Any]) -> bool:
        account_id = str(account.get("account_id") or "").strip()
        label = str(account.get("label") or "").strip()
        if not account_id:
            return False
        return not label or label == account_id or label == "Facebook Account" or label == f"Facebook Account {account_id}"

    def schedule_account_name_refresh(self, user_id: int, accounts: List[Dict[str, Any]], chat_id: int = 0) -> None:
        owner_scope = self.account_owner_scope(user_id)
        for account in accounts[:5]:
            account_id = str(account.get("account_id") or "").strip()
            if not account_id or not self.account_label_needs_refresh(account):
                continue
            task_key = f"{owner_scope or 'admin'}:{account_id}"
            if task_key in self.account_name_lookup_tasks:
                continue
            self.account_name_lookup_tasks.add(task_key)
            self.start_background_task(
                self.refresh_account_name_task(account_id, owner_scope, task_key, chat_id, user_id),
                f"account name lookup {account_id}",
            )

    async def refresh_account_name_task(
        self,
        account_id: str,
        owner_id: Optional[int],
        task_key: str,
        chat_id: int = 0,
        user_id: int = 0,
    ) -> None:
        try:
            cookie_string = await asyncio.to_thread(self.storage.get_account_cookie, account_id, owner_id)
            from playwright_engine import get_facebook_account_name

            resolved, resolved_name, error = await get_facebook_account_name(cookies_json(parse_cookies(cookie_string)))
            if resolved and resolved_name:
                changed = await asyncio.to_thread(self.storage.update_account_label, account_id, resolved_name, owner_id)
                logger.info("Updated Facebook account label for %s to %s", account_id, resolved_name)
                if changed and chat_id and user_id:
                    await self.show_dashboard(
                        chat_id,
                        prefix=f"Account name updated: {resolved_name}",
                        user_id=user_id,
                    )
            elif error:
                logger.info("Facebook account label refresh did not resolve %s: %s", account_id, error[:200])
        except Exception:
            logger.warning("Facebook account label refresh failed for %s", account_id, exc_info=True)
        finally:
            self.account_name_lookup_tasks.discard(task_key)

    async def dashboard_reply_markup(self, user_id: int = 0) -> Dict[str, Any]:
        accounts, summary, active_account = await self.dashboard_state(user_id)
        status_counts = summary.get("job_status_counts") or {}
        active_jobs = int(status_counts.get("queued", 0)) + int(status_counts.get("processing", 0))
        return dashboard_markup(
            has_accounts=bool(accounts),
            active_account=active_account,
            active_jobs=active_jobs,
            is_admin=self.is_admin_user(user_id),
        )

    async def show_dashboard(self, chat_id: int, message_id: int = 0, prefix: str = "", user_id: int = 0) -> None:
        try:
            accounts, summary, active_account = await self.dashboard_state(user_id)
            if user_id:
                self.schedule_account_name_refresh(user_id, accounts, chat_id)
            text = dashboard_text(accounts=accounts, summary=summary, active_account=active_account, prefix=prefix)
            status_counts = summary.get("job_status_counts") or {}
            active_jobs = int(status_counts.get("queued", 0)) + int(status_counts.get("processing", 0))
            reply_markup = dashboard_markup(
                has_accounts=bool(accounts),
                active_account=active_account,
                active_jobs=active_jobs,
                is_admin=self.is_admin_user(user_id),
            )
        except Exception as exc:
            logger.exception("Dashboard rendering failed")
            text = f"{prefix + chr(10) + chr(10) if prefix else ''}Dashboard is available, but database status could not be loaded: {exc}"
            reply_markup = dashboard_markup(has_accounts=False)
        await self.send_message(chat_id, text, message_id, reply_markup=reply_markup)

    def admin_overview_text(self, summary: Dict[str, Any], prefix: str = "") -> str:
        status_counts = summary.get("job_status_counts") or {}
        active_jobs = int(status_counts.get("queued", 0)) + int(status_counts.get("processing", 0))
        lines: List[str] = []
        if prefix:
            lines.extend([prefix, ""])
        lines.extend(
            [
                "🔒 Admin Dashboard",
                "━━━━━━━━━━━━━━━━━━",
                f"Users: {summary.get('user_count', 0)}",
                f"Accounts: {summary.get('total_accounts', 0)} total, {summary.get('active_accounts', 0)} active, {summary.get('inactive_accounts', 0)} inactive",
                f"Stored pages: {summary.get('page_count', 0)}",
                f"Jobs: {active_jobs} active, {int(status_counts.get('success', 0))} success, {int(status_counts.get('failed', 0))} failed",
                f"Active locks: {len(summary.get('active_locks') or [])}",
                "",
                "Use the admin keyboard below.",
            ]
        )
        return "\n".join(lines)

    def _format_dt(self, value: Any) -> str:
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

    async def show_admin_dashboard(self, chat_id: int, message_id: int, user_id: int, prefix: str = "") -> None:
        if not self.is_admin_user(user_id):
            await self.send_message(chat_id, "Admin dashboard is restricted.", message_id, reply_markup=await self.dashboard_reply_markup(user_id))
            return
        summary = await asyncio.to_thread(self.storage.admin_summary)
        await self.send_message(chat_id, self.admin_overview_text(summary, prefix), message_id, reply_markup=admin_dashboard_markup())

    async def show_admin_users(self, chat_id: int, message_id: int, user_id: int) -> None:
        rows = await asyncio.to_thread(self.storage.admin_users)
        lines = ["👥 Users", "━━━━━━━━━━━━━━━━━━"]
        if not rows:
            lines.append("No users recorded yet.")
        for row in rows:
            lines.append(
                f"- {row.get('telegram_user_id')} | active={row.get('active_account_id') or 'none'} | "
                f"accounts={row.get('account_count', 0)} | jobs={row.get('job_count', 0)} | "
                f"last={self._format_dt(row.get('last_seen'))}"
            )
        await self.send_message(chat_id, "\n".join(lines), message_id, reply_markup=admin_dashboard_markup())

    async def show_admin_accounts(self, chat_id: int, message_id: int, user_id: int) -> None:
        rows = await asyncio.to_thread(self.storage.admin_accounts)
        lines = ["🔑 Accounts", "━━━━━━━━━━━━━━━━━━"]
        if not rows:
            lines.append("No accounts stored yet.")
        for row in rows:
            status = "active" if row.get("active") else "inactive"
            lines.append(
                f"- {account_display_name(row, str(row.get('account_id') or ''), include_id=True)} ({status}) | pages={row.get('page_count', 0)} | "
                f"jobs={row.get('job_count', 0)} | owner={row.get('created_by') or 'unknown'}"
            )
        await self.send_message(chat_id, "\n".join(lines), message_id, reply_markup=admin_dashboard_markup())

    async def show_admin_post_stats(self, chat_id: int, message_id: int, user_id: int) -> None:
        summary = await asyncio.to_thread(self.storage.admin_summary)
        status_counts = summary.get("job_status_counts") or {}
        post_type_counts = summary.get("post_type_counts") or {}
        lines = ["📈 Post Stats", "━━━━━━━━━━━━━━━━━━", "Status:"]
        if status_counts:
            for status, count in sorted(status_counts.items()):
                lines.append(f"- {status}: {count}")
        else:
            lines.append("- no jobs yet")
        lines.append("")
        lines.append("Types:")
        if post_type_counts:
            for post_type, count in sorted(post_type_counts.items()):
                lines.append(f"- {post_type}: {count}")
        else:
            lines.append("- no jobs yet")
        recent_jobs = summary.get("recent_jobs") or []
        if recent_jobs:
            lines.extend(["", "Recent jobs:"])
            for job in recent_jobs[:8]:
                lines.append(f"- {job.get('status')} | {job.get('account_id')} | {job.get('post_type')} | {str(job.get('page_id_or_url') or '')[:32]}")
        await self.send_message(chat_id, "\n".join(lines), message_id, reply_markup=admin_dashboard_markup())

    async def show_admin_runtime_locks(self, chat_id: int, message_id: int, user_id: int) -> None:
        summary = await asyncio.to_thread(self.storage.admin_summary)
        locks = summary.get("active_locks") or []
        lines = ["🔐 Runtime Locks", "━━━━━━━━━━━━━━━━━━"]
        if not locks:
            lines.append("No active account locks.")
        for lock in locks:
            lines.append(
                f"- {lock.get('account_id')} | until={self._format_dt(lock.get('locked_until'))} | "
                f"by={str(lock.get('locked_by') or '')[:48]}"
            )
        await self.send_message(chat_id, "\n".join(lines), message_id, reply_markup=admin_dashboard_markup())

    async def show_admin_config(self, chat_id: int, message_id: int, user_id: int) -> None:
        keys = [
            "AUTO_INIT_DB",
            "AUTO_SET_TELEGRAM_WEBHOOK",
            "HEADLESS",
            "DELETE_COOKIE_MESSAGES",
            "BOT_ACCOUNT_COOKIE_COOLDOWN_SECONDS",
            "BOT_ACCOUNT_LOCK_LEASE_SECONDS",
            "BOT_ACCOUNT_LOCK_HEARTBEAT_SECONDS",
            "BOT_DASHBOARD_SESSION_TTL_SECONDS",
            "TELEGRAM_UPLOAD_DIR",
        ]
        lines = ["⚙️ System Config", "━━━━━━━━━━━━━━━━━━"]
        for key in keys:
            value = os.getenv(key, "")
            lines.append(f"- {key}={value or '<default>'}")
        lines.append(f"- admins_configured={bool(self.admin_ids)}")
        await self.send_message(chat_id, "\n".join(lines), message_id, reply_markup=admin_dashboard_markup())

    async def show_admin_debug_snapshot(self, chat_id: int, message_id: int, user_id: int) -> None:
        if not self.is_admin_user(user_id):
            await self.send_message(chat_id, "Admin dashboard is restricted.", message_id, reply_markup=await self.dashboard_reply_markup(user_id))
            return
        trace_id = new_debug_id("snapshot")
        started = time.monotonic()
        self.debug_event("admin_debug_snapshot_start", trace_id, user_id=user_id, chat_id=chat_id)
        try:
            summary = await asyncio.to_thread(self.storage.admin_summary)
            status_counts = summary.get("job_status_counts") or {}
            active_jobs = int(status_counts.get("queued", 0)) + int(status_counts.get("processing", 0))
            diagnostics_dir = Path(os.getenv("DIAGNOSTICS_DIR", "diagnostics"))
            diagnostic_count = 0
            latest_diagnostic = "none"
            try:
                if diagnostics_dir.exists():
                    diagnostic_items = sorted(
                        [path for path in diagnostics_dir.iterdir() if path.is_dir()],
                        key=lambda path: path.stat().st_mtime,
                        reverse=True,
                    )
                    diagnostic_count = len(diagnostic_items)
                    if diagnostic_items:
                        latest_diagnostic = diagnostic_items[0].name
            except Exception as exc:
                latest_diagnostic = f"unavailable: {compact_error(exc, 120)}"

            config_keys = [
                "LOG_LEVEL",
                "HEADLESS",
                "PLAYWRIGHT_BROWSERS_PATH",
                "FACEBOOK_BROWSER_EXECUTABLE",
                "BOT_ACCOUNT_COOKIE_COOLDOWN_SECONDS",
                "BOT_PROGRESS_HEARTBEAT_SECONDS",
                "BOT_POSTING_ENGINE_TIMEOUT_SECONDS",
                "BOT_BATCH_POSTING_ENGINE_TIMEOUT_SECONDS",
                "POST_PAGES_PORTAL_FIRST",
                "POST_PAGES_PORTAL_FIRST_TEXT_ENABLED",
                "POST_ENABLE_PAGES_PORTAL_FALLBACK",
                "POST_PAGES_PORTAL_FALLBACK_TEXT_ENABLED",
                "FACEBOOK_IMAGE_FORCE_SANITIZE_UPLOAD",
            ]
            lines = [
                "🧰 Debug Snapshot",
                "━━━━━━━━━━━━━━━━━━",
                f"Debug ID: {trace_id}",
                f"Uptime: {int(time.monotonic() - self.started_at)}s",
                f"Started: {self.started_wall_at.strftime('%Y-%m-%d %H:%M UTC')}",
                f"Revision: {self.deploy_revision() or 'unknown'}",
                f"Python: {platform.python_version()} ({sys.version_info.major}.{sys.version_info.minor})",
                f"Platform: {platform.system()} {platform.release()}",
                f"PID: {os.getpid()}",
                f"Background tasks: {len(self.background_tasks)}",
                "",
                "Database:",
                f"- users={summary.get('user_count', 0)} accounts={summary.get('total_accounts', 0)} active_accounts={summary.get('active_accounts', 0)} pages={summary.get('page_count', 0)}",
                f"- jobs active={active_jobs} success={int(status_counts.get('success', 0))} failed={int(status_counts.get('failed', 0))}",
                "",
                "Diagnostics:",
                f"- folder={diagnostics_dir}",
                f"- count={diagnostic_count}",
                f"- latest={latest_diagnostic}",
                "",
                "Config:",
            ]
            for key in config_keys:
                value = os.getenv(key, "")
                if key in {"FACEBOOK_BROWSER_EXECUTABLE", "PLAYWRIGHT_BROWSERS_PATH"}:
                    value = value or "<default>"
                lines.append(f"- {key}={_debug_safe(value or '<default>', 100)}")

            locks = summary.get("active_locks") or []
            lines.extend(["", "Active locks:"])
            if locks:
                for lock in locks[:5]:
                    lines.append(
                        f"- {lock.get('account_id')} until={self._format_dt(lock.get('locked_until'))} by={str(lock.get('locked_by') or '')[:42]}"
                    )
            else:
                lines.append("- none")

            lines.extend(["", "Active background task labels:"])
            task_labels = []
            for task in list(self.background_tasks)[:10]:
                label = self.background_task_labels.get(task, "")
                if not label:
                    coro = task.get_coro()
                    label = getattr(coro, "__qualname__", coro.__class__.__name__)
                task_labels.append(label)
            if task_labels:
                for label in task_labels:
                    lines.append(f"- {label}")
            else:
                lines.append("- none")

            recent_jobs = summary.get("recent_jobs") or []
            failed_jobs = [job for job in recent_jobs if str(job.get("status") or "") == "failed"]
            lines.extend(["", "Recent failed jobs:"])
            if failed_jobs:
                for job in failed_jobs[:5]:
                    error = str(job.get("error") or "").strip()
                    diagnostic = diagnostic_path_from_text(error)
                    suffix = f" | diag={Path(diagnostic).name}" if diagnostic else ""
                    lines.append(
                        f"- {str(job.get('id') or '')[:8]} {job.get('post_type')} {job.get('account_id')} "
                        f"{self._format_dt(job.get('completed_at'))}: {compact_error(Exception(error), 140)}{suffix}"
                    )
            else:
                lines.append("- none in latest 12 jobs")

            elapsed = time.monotonic() - started
            lines.extend(["", f"Snapshot generated in {elapsed:.2f}s. Check Render logs for `BOT_DEBUG` with this Debug ID."])
            self.debug_event(
                "admin_debug_snapshot_complete",
                trace_id,
                elapsed_seconds=round(elapsed, 3),
                active_jobs=active_jobs,
                failed_recent=len(failed_jobs),
                diagnostics=diagnostic_count,
            )
            await self.send_message(chat_id, "\n".join(lines), message_id, reply_markup=admin_dashboard_markup())
        except Exception as exc:
            self.debug_event("admin_debug_snapshot_failed", trace_id, error=compact_error(exc))
            logger.exception("Admin debug snapshot failed trace_id=%s", trace_id)
            await self.send_message(
                chat_id,
                "\n".join(["Debug snapshot failed.", f"Debug ID: {trace_id}", compact_error(exc)]),
                message_id,
                reply_markup=admin_dashboard_markup(),
            )

    async def prompt_for_account(self, chat_id: int, message_id: int, prompt: str, user_id: int = 0) -> bool:
        accounts = await self.dashboard_accounts(user_id)
        if not accounts:
            await self.send_message(
                chat_id,
                "No accounts are stored yet. Use Add Account first.",
                message_id,
                reply_markup=dashboard_markup(has_accounts=False),
            )
            return False
        active_account = ""
        if user_id:
            active_account = await self.active_account_id(user_id)
        choice_labels: List[str] = []
        choice_map: Dict[str, str] = {}
        seen_labels: Dict[str, int] = {}
        for item in accounts:
            base_label = account_choice_label(item, active_account)
            count = seen_labels.get(base_label, 0) + 1
            seen_labels[base_label] = count
            label = base_label if count == 1 else f"{base_label} ({count})"
            choice_labels.append(label)
            choice_map[label] = str(item.get("account_id") or "")
        session = self.get_dashboard_session(chat_id, user_id)
        if session:
            session["account_choices"] = choice_map
            self.set_dashboard_session(chat_id, user_id, session)
        await self.send_message(
            chat_id,
            prompt,
            message_id,
            reply_markup=choices_markup(choice_labels, placeholder="Choose account"),
        )
        return True

    async def prompt_for_page(self, chat_id: int, message_id: int, account_id: str, user_id: int = 0) -> None:
        pages = await asyncio.to_thread(self.storage.list_pages, account_id, self.account_owner_scope(user_id))
        account = await asyncio.to_thread(self.storage.get_account, account_id, self.account_owner_scope(user_id))
        account_name = account_display_name(account or {}, account_id)
        session = self.get_dashboard_session(chat_id, user_id)
        select_all = bool(session.get("select_all_pages"))
        selected_pages = list(range(len(pages))) if select_all else list(session.get("selected_pages") or [])
        selected_pages = [idx for idx in selected_pages if isinstance(idx, int) and 0 <= idx < len(pages)]
        if session:
            session["step"] = "page_select"
            session["pages"] = pages
            session["selected_pages"] = selected_pages
            self.set_dashboard_session(chat_id, user_id, session)
        if pages:
            await self.send_message(
                chat_id,
                page_selection_card(account_name=account_name, pages=pages, selected_indexes=selected_pages),
                message_id,
                reply_markup=page_selection_markup(pages, selected_pages),
            )
            return
        await self.send_message(
            chat_id,
            page_selection_card(
                account_name=account_name,
                pages=[],
                selected_indexes=[],
                prefix="No stored pages for this account yet.",
            ),
            message_id,
            reply_markup=page_selection_markup([], []),
        )

    def selected_pages_from_session(self, session: Dict[str, Any]) -> List[Dict[str, Any]]:
        pages = session.get("pages") if isinstance(session.get("pages"), list) else []
        selected = session.get("selected_pages") if isinstance(session.get("selected_pages"), list) else []
        return [
            pages[idx]
            for idx in selected
            if isinstance(idx, int) and 0 <= idx < len(pages) and isinstance(pages[idx], dict)
        ]

    def multi_video_prompt(self, session: Dict[str, Any]) -> str:
        selected_pages = self.selected_pages_from_session(session)
        received = len(session.get("multi_media_paths") or []) if isinstance(session.get("multi_media_paths"), list) else 0
        next_index = min(received, max(0, len(selected_pages) - 1))
        page_name = page_display_name(selected_pages[next_index], next_index) if selected_pages else "Page"
        return "\n".join(
            [
                "📚 Multi Video Upload",
                "━━━━━━━━━━━━━━━━━━",
                f"Received: {received}/{len(selected_pages)}",
                f"Next page: {page_name}",
                "",
                f"Send video {received + 1} of {len(selected_pages)} now.",
            ]
        )

    def multi_video_url_prompt(self, session: Dict[str, Any]) -> str:
        selected_pages = self.selected_pages_from_session(session)
        received = len(session.get("multi_media_paths") or []) if isinstance(session.get("multi_media_paths"), list) else 0
        next_index = min(received, max(0, len(selected_pages) - 1))
        page_name = page_display_name(selected_pages[next_index], next_index) if selected_pages else "Page"
        return "\n".join(
            [
                "🔗 Multi Video URLs",
                "━━━━━━━━━━━━━━━━━━",
                f"Received: {received}/{len(selected_pages)}",
                f"Next page: {page_name}",
                "",
                f"Paste direct video URL {received + 1} of {len(selected_pages)} now.",
            ]
        )

    async def validate_video_url_or_reply(self, chat_id: int, message_id: int, text: str) -> str:
        ok, value = parse_video_media_url(text)
        if not ok:
            await self.send_message(chat_id, value, message_id, reply_markup=cancel_markup())
            return ""
        progress = await self.send_message(
            chat_id,
            progress_card("Checking video URL...", 0, 1, "Validating direct URL..."),
            message_id,
            reply_markup=cancel_markup(),
        )
        progress_message_id = int((progress.get("result") or {}).get("message_id") or 0)
        reachable, error, size = await asyncio.to_thread(probe_video_media_url, value)
        if not reachable:
            await self.edit_or_send_message(
                chat_id,
                progress_message_id,
                progress_card("Checking video URL...", 1, 1, error or "Video URL is not reachable."),
                reply_to_message_id=message_id,
                reply_markup=cancel_markup(),
            )
            return ""
        size_detail = f" Accepted. Size: about {size} bytes." if size else " Accepted. Size unknown."
        await self.edit_or_send_message(
            chat_id,
            progress_message_id,
            progress_card("Checking video URL...", 1, 1, size_detail),
            reply_to_message_id=message_id,
            reply_markup=cancel_markup(),
        )
        return value

    async def edit_page_selection_card(
        self,
        chat_id: int,
        message_id: int,
        user_id: int,
        session: Dict[str, Any],
        prefix: str = "",
    ) -> None:
        account_id = str(session.get("account_id") or "")
        account = await asyncio.to_thread(self.storage.get_account, account_id, self.account_owner_scope(user_id))
        account_name = account_display_name(account or {}, account_id)
        pages = session.get("pages") if isinstance(session.get("pages"), list) else []
        selected = session.get("selected_pages") if isinstance(session.get("selected_pages"), list) else []
        await self.edit_message(
            chat_id,
            message_id,
            page_selection_card(account_name=account_name, pages=pages, selected_indexes=selected, prefix=prefix),
            reply_markup=page_selection_markup(pages, selected),
        )

    async def show_post_type_card(
        self,
        chat_id: int,
        message_id: int,
        user_id: int,
        session: Dict[str, Any],
    ) -> None:
        account_id = str(session.get("account_id") or "")
        account = await asyncio.to_thread(self.storage.get_account, account_id, self.account_owner_scope(user_id))
        account_name = account_display_name(account or {}, account_id)
        pages = session.get("pages") if isinstance(session.get("pages"), list) else []
        selected = session.get("selected_pages") if isinstance(session.get("selected_pages"), list) else []
        session["step"] = "post_type_inline"
        self.set_dashboard_session(chat_id, user_id, session)
        await self.edit_message(
            chat_id,
            message_id,
            post_type_card(account_name=account_name, pages=pages, selected_indexes=selected),
            reply_markup=post_type_inline_markup(),
        )

    async def show_video_mode_card(
        self,
        chat_id: int,
        message_id: int,
        user_id: int,
        session: Dict[str, Any],
    ) -> None:
        account_id = str(session.get("account_id") or "")
        account = await asyncio.to_thread(self.storage.get_account, account_id, self.account_owner_scope(user_id))
        account_name = account_display_name(account or {}, account_id)
        pages = session.get("pages") if isinstance(session.get("pages"), list) else []
        selected = session.get("selected_pages") if isinstance(session.get("selected_pages"), list) else []
        session["post_type"] = "video"
        session["step"] = "video_mode"
        session.pop("media_path", None)
        session.pop("multi_media_paths", None)
        self.set_dashboard_session(chat_id, user_id, session)
        await self.edit_message(
            chat_id,
            message_id,
            video_mode_card(account_name=account_name, pages=pages, selected_indexes=selected),
            reply_markup=video_mode_inline_markup(),
        )

    async def show_post_input_card(
        self,
        chat_id: int,
        message_id: int,
        user_id: int,
        session: Dict[str, Any],
    ) -> None:
        post_type = str(session.get("post_type") or "text")
        if post_type == "video":
            await self.show_video_mode_card(chat_id, message_id, user_id, session)
            return
        session["step"] = "caption" if post_type == "text" else f"media_{post_type}"
        self.set_dashboard_session(chat_id, user_id, session)
        await self.edit_message(chat_id, message_id, post_input_card(post_type), reply_markup={"inline_keyboard": []})

    async def show_post_review(
        self,
        chat_id: int,
        message_id: int,
        user_id: int,
        session: Dict[str, Any],
        *,
        edit: bool = False,
    ) -> None:
        account_id = str(session.get("account_id") or "")
        account = await asyncio.to_thread(self.storage.get_account, account_id, self.account_owner_scope(user_id))
        account_name = account_display_name(account or {}, account_id)
        selected_pages = self.selected_pages_from_session(session)
        text = post_review_card(
            account_name=account_name,
            pages=selected_pages,
            post_type=str(session.get("post_type") or "text"),
            caption=str(session.get("caption") or ""),
            media_path=str(session.get("media_path") or ""),
            multi_media_count=len(session.get("multi_media_paths") or []) if isinstance(session.get("multi_media_paths"), list) else 0,
        )
        session["step"] = "review"
        self.set_dashboard_session(chat_id, user_id, session)
        if edit:
            await self.edit_message(chat_id, message_id, text, reply_markup=post_confirm_inline_markup())
            return
        await self.send_message(chat_id, text, message_id, reply_markup=post_confirm_inline_markup())

    async def queue_reviewed_post(self, chat_id: int, user_id: int, session: Dict[str, Any]) -> None:
        account_id = str(session.get("account_id") or "")
        post_type = str(session.get("post_type") or "text")
        caption = str(session.get("caption") or "")
        media_path = str(session.get("media_path") or "")
        selected_pages = self.selected_pages_from_session(session)
        if not selected_pages:
            await self.send_message(chat_id, "No pages selected.", reply_markup=await self.dashboard_reply_markup(user_id))
            return
        multi_media_paths = session.get("multi_media_paths") if isinstance(session.get("multi_media_paths"), list) else []
        if multi_media_paths:
            await self.queue_paired_post_jobs_or_report(
                chat_id,
                user_id,
                account_id,
                selected_pages,
                post_type,
                caption,
                [str(path) for path in multi_media_paths],
            )
            return
        if len(selected_pages) == 1:
            page = selected_pages[0]
            page_id_or_url = str(page.get("page_url") or page.get("page_id") or "").strip()
            page_name = page_display_name(page, 0)
            await self.queue_post_job_or_report(
                chat_id,
                user_id,
                account_id,
                page_id_or_url,
                post_type,
                caption,
                media_path,
                page_name=page_name,
            )
            return
        await self.queue_bulk_post_jobs_or_report(chat_id, user_id, account_id, selected_pages, post_type, caption, media_path)

    def account_action_text(self, account: Dict[str, Any], pages: List[Dict[str, Any]]) -> str:
        display = account_display_name(account, str(account.get("account_id") or ""))
        updated = self._format_dt(account.get("updated_at"))
        if pages:
            newest_page_update = max((page.get("updated_at") for page in pages if page.get("updated_at")), default="")
            pages_line = f"Cached pages: {len(pages)} | refreshed: {self._format_dt(newest_page_update)}"
        else:
            pages_line = "Cached pages: 0 | tap Refresh Pages before all-page posting"
        return "\n".join(
            [
                f"Account selected: {display}",
                "━━━━━━━━━━━━━━━━━━",
                f"Status: {'active' if account.get('active') else 'inactive'}",
                f"Updated: {updated}",
                pages_line,
                "",
                "You can check this account cookie shape before choosing pages, continue with cached pages, or refresh the page cache.",
            ]
        )

    async def show_active_account_actions(
        self,
        chat_id: int,
        message_id: int,
        user_id: int,
        account_id: str,
    ) -> None:
        account = await asyncio.to_thread(self.storage.get_account, account_id, self.account_owner_scope(user_id))
        if not account:
            await self.send_message(chat_id, f"Account not found: {account_id}", message_id, reply_markup=await self.dashboard_reply_markup(user_id))
            return
        pages = await asyncio.to_thread(self.storage.list_pages, account_id, self.account_owner_scope(user_id))
        await self.send_message(
            chat_id,
            self.account_action_text(account, pages),
            message_id,
            reply_markup=account_post_action_markup(),
        )

    async def active_account_or_warn(self, chat_id: int, message_id: int, user_id: int) -> str:
        active_account = await self.active_account_id(user_id)
        if active_account:
            return active_account
        await self.send_message(
            chat_id,
            "No active account selected. Use Switch Active Account or Add Facebook Account first.",
            message_id,
            reply_markup=await self.dashboard_reply_markup(user_id),
        )
        return ""

    async def handle_dashboard_button(
        self,
        chat_id: int,
        user_id: int,
        message_id: int,
        action: str,
    ) -> None:
        if action in {"dashboard", "cancel"}:
            self.clear_dashboard_session(chat_id, user_id)
            prefix = "Current operation cancelled." if action == "cancel" else ""
            await self.show_dashboard(chat_id, message_id, prefix=prefix, user_id=user_id)
            return
        if action == "user_dashboard":
            self.clear_dashboard_session(chat_id, user_id)
            await self.show_dashboard(chat_id, message_id, user_id=user_id)
            return
        if action in {
            "admin_dashboard",
            "admin_system_stats",
            "admin_users",
            "admin_accounts",
            "admin_post_stats",
            "admin_runtime_locks",
            "admin_system_config",
            "admin_debug_snapshot",
        }:
            self.clear_dashboard_session(chat_id, user_id)
            if not self.is_admin_user(user_id):
                await self.send_message(chat_id, "Admin dashboard is restricted.", message_id, reply_markup=await self.dashboard_reply_markup(user_id))
                return
            if action in {"admin_dashboard", "admin_system_stats"}:
                await self.show_admin_dashboard(chat_id, message_id, user_id)
            elif action == "admin_users":
                await self.show_admin_users(chat_id, message_id, user_id)
            elif action == "admin_accounts":
                await self.show_admin_accounts(chat_id, message_id, user_id)
            elif action == "admin_post_stats":
                await self.show_admin_post_stats(chat_id, message_id, user_id)
            elif action == "admin_runtime_locks":
                await self.show_admin_runtime_locks(chat_id, message_id, user_id)
            elif action == "admin_system_config":
                await self.show_admin_config(chat_id, message_id, user_id)
            elif action == "admin_debug_snapshot":
                await self.show_admin_debug_snapshot(chat_id, message_id, user_id)
            return
        if action in {"accounts", "manage_accounts"}:
            self.clear_dashboard_session(chat_id, user_id)
            await self.command_accounts(chat_id, message_id, user_id)
            return
        if action == "status":
            self.clear_dashboard_session(chat_id, user_id)
            await self.show_dashboard(chat_id, message_id, user_id=user_id)
            return
        if action == "post_history":
            self.clear_dashboard_session(chat_id, user_id)
            await self.command_post_history(chat_id, message_id, user_id)
            return
        if action == "check_cookies":
            self.clear_dashboard_session(chat_id, user_id)
            await self.command_check_cookies(chat_id, message_id, user_id)
            return
        if action == "add_account":
            self.set_dashboard_session(chat_id, user_id, {"action": "add_account", "step": "cookie", "cookie_chunks": []})
            await self.send_message(chat_id, prompt_text("add_account"), message_id, reply_markup=cookie_input_markup())
            return
        if action == "switch_account":
            self.set_dashboard_session(chat_id, user_id, {"action": "switch_account", "step": "account"})
            if not await self.prompt_for_account(chat_id, message_id, "Select the account to make active.", user_id):
                self.clear_dashboard_session(chat_id, user_id)
            return
        if action == "refresh_pages":
            active_account = await self.active_account_id(user_id)
            if active_account:
                self.clear_dashboard_session(chat_id, user_id)
                await self.command_discover_pages(chat_id, message_id, [active_account], user_id, refresh=True)
                return
            self.set_dashboard_session(chat_id, user_id, {"action": "refresh_pages", "step": "account"})
            if not await self.prompt_for_account(chat_id, message_id, "Select the account to refresh managed pages for.", user_id):
                self.clear_dashboard_session(chat_id, user_id)
            return
        if action == "check_active_account":
            active_account = await self.active_account_or_warn(chat_id, message_id, user_id)
            if active_account:
                await self.command_check_account(chat_id, message_id, user_id, active_account)
            return
        if action == "continue_active_account":
            active_account = await self.active_account_or_warn(chat_id, message_id, user_id)
            if active_account:
                self.set_dashboard_session(
                    chat_id,
                    user_id,
                    {"action": "post", "account_id": active_account, "step": "page_then_type"},
                )
                await self.prompt_for_page(chat_id, message_id, active_account, user_id)
            return
        if action == "select_account":
            self.set_dashboard_session(chat_id, user_id, {"action": "post", "step": "account"})
            if not await self.prompt_for_account(chat_id, message_id, prompt_text("post", "account"), user_id):
                self.clear_dashboard_session(chat_id, user_id)
            return
        if action == "post_active":
            active_account = await self.active_account_or_warn(chat_id, message_id, user_id)
            if active_account:
                await self.show_active_account_actions(chat_id, message_id, user_id, active_account)
            return
        if action == "post_all_pages":
            active_account = await self.active_account_or_warn(chat_id, message_id, user_id)
            if not active_account:
                return
            self.set_dashboard_session(
                chat_id,
                user_id,
                {
                    "action": "post",
                    "account_id": active_account,
                    "step": "page_select",
                    "select_all_pages": True,
                },
            )
            await self.prompt_for_page(chat_id, message_id, active_account, user_id)
            return
        if action in {"discover_pages", "list_pages"}:
            self.set_dashboard_session(chat_id, user_id, {"action": action, "step": "account"})
            if not await self.prompt_for_account(chat_id, message_id, prompt_text(action, "account"), user_id):
                self.clear_dashboard_session(chat_id, user_id)
            return
        if action in POST_ACTION_TYPES:
            post_type = POST_ACTION_TYPES[action]
            active_account = await self.active_account_id(user_id)
            if active_account:
                self.set_dashboard_session(
                    chat_id,
                    user_id,
                    {"action": "post", "post_type": post_type, "account_id": active_account, "step": "page"},
                )
                await self.prompt_for_page(chat_id, message_id, active_account, user_id)
                return
            self.set_dashboard_session(
                chat_id,
                user_id,
                {"action": "post", "post_type": post_type, "step": "account"},
            )
            if not await self.prompt_for_account(chat_id, message_id, f"{post_type.title()} post: {prompt_text('post', 'account')}", user_id):
                self.clear_dashboard_session(chat_id, user_id)
            return
        await self.show_dashboard(chat_id, message_id, user_id=user_id)

    async def save_account_cookie_payload(
        self,
        chat_id: int,
        user_id: int,
        message_id: int,
        payload: str,
        *,
        account_hint: str = "auto",
        cookie_message_ids: Optional[List[int]] = None,
    ) -> bool:
        try:
            parsed = parse_account_cookie_payload(payload, account_hint)
        except Exception as exc:
            await self.send_message(chat_id, f"Could not parse cookies: {exc}", message_id, reply_markup=cookie_input_markup())
            return False

        progress = await self.send_message(
            chat_id,
            progress_card("Adding Facebook account...", 1, 3, "Cookies parsed."),
            message_id,
        )
        progress_message_id = int((progress.get("result") or {}).get("message_id") or 0)

        label = "Facebook Account"
        name_source = "Lookup queued"
        progress_message_id = await self.edit_or_send_message(
            chat_id,
            progress_message_id,
            progress_card("Adding Facebook account...", 2, 3, "Saving account. Name lookup will continue in background..."),
            reply_to_message_id=message_id,
        )
        try:
            await asyncio.to_thread(
                self.storage.upsert_account,
                parsed.account_id,
                parsed.cookie_header,
                label,
                user_id,
            )
            await asyncio.to_thread(self.storage.set_active_account, user_id, parsed.account_id)
            self.schedule_account_name_refresh(
                user_id,
                [{"account_id": parsed.account_id, "label": label, "active": True}],
                chat_id,
            )
        except Exception as exc:
            await self.edit_or_send_message(
                chat_id,
                progress_message_id,
                progress_card("Adding Facebook account...", 3, 3, f"Account was not saved: {str(exc)[:500]}"),
                reply_to_message_id=message_id,
                reply_markup=await self.dashboard_reply_markup(user_id),
            )
            return False

        if _env_bool("DELETE_COOKIE_MESSAGES", True):
            for cookie_message_id in dict.fromkeys(cookie_message_ids or [message_id]):
                if cookie_message_id:
                    await self.delete_message(chat_id, cookie_message_id)

        final_card = "\n".join(
            [
                "✅ Account Added Successfully",
                "━━━━━━━━━━━━━━━━━━━━━━",
                f"👤 {label}",
                f"🆔 {parsed.account_id}",
                f"📋 {name_source}",
                "🟢 Selected: Active",
                "🟡 Cookies: Not verified against Facebook posting yet",
            ]
        )
        await self.edit_or_send_message(
            chat_id,
            progress_message_id,
            progress_card("Adding Facebook account...", 3, 3, "Account saved."),
            reply_to_message_id=message_id,
            reply_markup=await self.dashboard_reply_markup(user_id),
        )
        await self.show_dashboard(
            chat_id,
            prefix=final_card,
            user_id=user_id,
        )
        return True

    async def handle_dashboard_session(
        self,
        chat_id: int,
        user_id: int,
        message_id: int,
        text: str,
        message: Dict[str, Any],
    ) -> bool:
        session = self.get_dashboard_session(chat_id, user_id)
        if not session:
            return False
        action = str(session.get("action") or "")
        step = str(session.get("step") or "")

        if action == "add_account":
            cookie_message_ids: List[int] = []
            if text.strip() in {"Done", BUTTON_DONE}:
                payload = "\n".join(session.get("cookie_chunks") or []).strip()
                cookie_message_ids = [message_id]
            else:
                try:
                    payload, cookie_message_ids = await self.cookie_payload_from_message(message, text)
                except Exception as exc:
                    await self.send_message(chat_id, f"Could not read cookie file: {exc}", message_id, reply_markup=cancel_markup())
                    return True
                if payload and not self.extract_document_file_id(message):
                    chunks = list(session.get("cookie_chunks") or [])
                    chunks.append(payload)
                    session["cookie_chunks"] = chunks
                    payload = "\n".join(chunks).strip()

            if not payload:
                await self.send_message(chat_id, prompt_text("add_account"), message_id, reply_markup=cookie_input_markup())
                return True

            try:
                parse_account_cookie_payload(payload, "auto")
            except Exception:
                if text.strip() not in {"Done", BUTTON_DONE} and not self.extract_document_file_id(message):
                    self.set_dashboard_session(chat_id, user_id, session)
                    await self.send_message(
                        chat_id,
                        "I got that cookie chunk. Keep pasting the remaining JSON/cookie text, or tap Done when complete.",
                        message_id,
                        reply_markup=cookie_input_markup(),
                    )
                    return True
                await self.send_message(
                    chat_id,
                    "I could not parse the full cookie payload. Send a raw cookie string or upload the exported JSON file.",
                    message_id,
                    reply_markup=cancel_markup(),
                )
                return True

            self.clear_dashboard_session(chat_id, user_id)
            await self.save_account_cookie_payload(
                chat_id,
                user_id,
                message_id,
                payload,
                account_hint="auto",
                cookie_message_ids=cookie_message_ids,
            )
            return True

        if step == "account":
            account_choices = session.get("account_choices") if isinstance(session.get("account_choices"), dict) else {}
            account_id = str(account_choices.get(text.strip()) or parse_choice_id(text))
            if not account_id:
                if not await self.prompt_for_account(chat_id, message_id, prompt_text(action, "account"), user_id):
                    self.clear_dashboard_session(chat_id, user_id)
                return True
            if not await asyncio.to_thread(self.storage.account_exists, account_id, True, self.account_owner_scope(user_id)):
                await self.send_message(
                    chat_id,
                    f"Account not found or inactive: {account_id}",
                    message_id,
                    reply_markup=await self.dashboard_reply_markup(user_id),
                )
                return True
            session["account_id"] = account_id
            if action == "switch_account":
                await asyncio.to_thread(self.storage.set_active_account, user_id, account_id)
                account = await asyncio.to_thread(self.storage.get_account, account_id, self.account_owner_scope(user_id))
                display = account_display_name(account or {}, account_id)
                self.clear_dashboard_session(chat_id, user_id)
                await self.show_dashboard(chat_id, message_id, prefix=f"Active account switched to {display}.", user_id=user_id)
                return True
            if action == "discover_pages":
                self.clear_dashboard_session(chat_id, user_id)
                await self.command_discover_pages(chat_id, message_id, [account_id], user_id)
                return True
            if action == "refresh_pages":
                self.clear_dashboard_session(chat_id, user_id)
                await self.command_discover_pages(chat_id, message_id, [account_id], user_id, refresh=True)
                return True
            if action == "list_pages":
                self.clear_dashboard_session(chat_id, user_id)
                await self.command_list_pages(chat_id, message_id, [account_id], user_id)
                return True
            session["step"] = "page_select"
            self.set_dashboard_session(chat_id, user_id, session)
            await self.prompt_for_page(chat_id, message_id, account_id, user_id)
            return True

        if step == "post_type":
            post_type = parse_post_type_choice(text)
            if not post_type:
                await self.send_message(
                    chat_id,
                    prompt_text("post", "post_type"),
                    message_id,
                    reply_markup=choices_markup(POST_TYPE_CHOICES, placeholder="Choose post type"),
                )
                return True
            session["post_type"] = post_type
            if action == "post_all_pages":
                session["step"] = "caption_all" if post_type == "text" else f"media_{post_type}_all"
                self.set_dashboard_session(chat_id, user_id, session)
                await self.send_message(chat_id, prompt_text("post", session["step"]), message_id, reply_markup=cancel_markup())
                return True
            session["step"] = "page"
            self.set_dashboard_session(chat_id, user_id, session)
            await self.prompt_for_page(chat_id, message_id, str(session.get("account_id") or ""), user_id)
            return True

        if action == "post" and step == "page_select":
            await self.send_message(
                chat_id,
                "Use the page buttons on the selection card, then tap Confirm.",
                message_id,
                reply_markup=await self.dashboard_reply_markup(user_id),
            )
            return True

        if action == "post" and step == "post_type_inline":
            post_type = parse_post_type_choice(text)
            if not post_type:
                await self.send_message(
                    chat_id,
                    "Use the post-type buttons, or type text, image, or video.",
                    message_id,
                    reply_markup=await self.dashboard_reply_markup(user_id),
                )
                return True
            session["post_type"] = post_type
            if post_type == "video":
                self.set_dashboard_session(chat_id, user_id, session)
                account_id = str(session.get("account_id") or "")
                account = await asyncio.to_thread(self.storage.get_account, account_id, self.account_owner_scope(user_id))
                account_name = account_display_name(account or {}, account_id)
                pages = session.get("pages") if isinstance(session.get("pages"), list) else []
                selected = session.get("selected_pages") if isinstance(session.get("selected_pages"), list) else []
                session["step"] = "video_mode"
                self.set_dashboard_session(chat_id, user_id, session)
                await self.send_message(
                    chat_id,
                    video_mode_card(account_name=account_name, pages=pages, selected_indexes=selected),
                    message_id,
                    reply_markup=video_mode_inline_markup(),
                )
                return True
            session["step"] = "caption" if post_type == "text" else f"media_{post_type}"
            self.set_dashboard_session(chat_id, user_id, session)
            await self.send_message(chat_id, post_input_card(post_type), message_id, reply_markup=cancel_markup())
            return True

        if action == "post" and step == "page_then_type":
            page_id_or_url = parse_choice_id(text)
            if not page_id_or_url:
                await self.prompt_for_page(chat_id, message_id, str(session.get("account_id") or ""), user_id)
                return True
            session["page_id_or_url"] = page_id_or_url
            session["step"] = "post_type_after_page"
            self.set_dashboard_session(chat_id, user_id, session)
            await self.send_message(
                chat_id,
                prompt_text("post", "post_type"),
                message_id,
                reply_markup=choices_markup(POST_TYPE_CHOICES, placeholder="Choose post type"),
            )
            return True

        if action == "post" and step == "post_type_after_page":
            post_type = parse_post_type_choice(text)
            if not post_type:
                await self.send_message(
                    chat_id,
                    prompt_text("post", "post_type"),
                    message_id,
                    reply_markup=choices_markup(POST_TYPE_CHOICES, placeholder="Choose post type"),
                )
                return True
            session["post_type"] = post_type
            if post_type == "video":
                page_id_or_url = str(session.get("page_id_or_url") or "").strip()
                session["pages"] = [{"page_id": page_id_or_url, "page_url": page_id_or_url, "page_name": page_id_or_url}]
                session["selected_pages"] = [0]
                session["step"] = "video_mode"
                self.set_dashboard_session(chat_id, user_id, session)
                await self.send_message(
                    chat_id,
                    video_mode_card(account_name="", pages=session["pages"], selected_indexes=[0]),
                    message_id,
                    reply_markup=video_mode_inline_markup(),
                )
                return True
            session["step"] = "caption" if post_type == "text" else f"media_{post_type}"
            self.set_dashboard_session(chat_id, user_id, session)
            await self.send_message(chat_id, prompt_text("post", session["step"]), message_id, reply_markup=cancel_markup())
            return True

        if action == "post" and step == "page":
            page_id_or_url = parse_choice_id(text)
            if not page_id_or_url:
                await self.prompt_for_page(chat_id, message_id, str(session.get("account_id") or ""), user_id)
                return True
            post_type = str(session.get("post_type") or "text")
            session["page_id_or_url"] = page_id_or_url
            if post_type == "video":
                session["pages"] = [{"page_id": page_id_or_url, "page_url": page_id_or_url, "page_name": page_id_or_url}]
                session["selected_pages"] = [0]
                session["step"] = "video_mode"
                self.set_dashboard_session(chat_id, user_id, session)
                await self.send_message(
                    chat_id,
                    video_mode_card(account_name="", pages=session["pages"], selected_indexes=[0]),
                    message_id,
                    reply_markup=video_mode_inline_markup(),
                )
                return True
            session["step"] = "caption" if post_type == "text" else f"media_{post_type}"
            self.set_dashboard_session(chat_id, user_id, session)
            await self.send_message(chat_id, prompt_text("post", session["step"]), message_id, reply_markup=cancel_markup())
            return True

        if action == "post" and step == "caption":
            caption = text.strip()
            if not caption:
                await self.send_message(chat_id, "Caption cannot be empty. Send the post text.", message_id, reply_markup=cancel_markup())
                return True
            session["post_type"] = "text"
            session["caption"] = caption
            session["media_path"] = ""
            self.set_dashboard_session(chat_id, user_id, session)
            await self.show_post_review(chat_id, message_id, user_id, session)
            return True

        if action == "post" and step == "caption_edit":
            session["caption"] = "" if text == " " else text.strip()
            self.set_dashboard_session(chat_id, user_id, session)
            await self.show_post_review(chat_id, message_id, user_id, session)
            return True

        if action == "post_all_pages" and step == "caption_all":
            caption = text.strip()
            if not caption:
                await self.send_message(chat_id, "Caption cannot be empty. Send the post text.", message_id, reply_markup=cancel_markup())
                return True
            pages = await asyncio.to_thread(
                self.storage.list_pages,
                str(session.get("account_id") or ""),
                self.account_owner_scope(user_id),
            )
            if not pages:
                self.clear_dashboard_session(chat_id, user_id)
                await self.send_message(
                    chat_id,
                    "No stored pages for this account. Run Discover Pages first.",
                    message_id,
                    reply_markup=await self.dashboard_reply_markup(user_id),
                )
                return True
            self.clear_dashboard_session(chat_id, user_id)
            await self.queue_bulk_post_jobs_or_report(
                chat_id,
                user_id,
                str(session.get("account_id") or ""),
                pages,
                "text",
                caption,
                "",
            )
            return True

        if action == "post_all_pages" and step in {"media_image_all", "media_video_all"}:
            post_type = str(session.get("post_type") or "").strip()
            file_id = self.extract_media_file_id(message, post_type)
            if not file_id:
                await self.send_message(chat_id, prompt_text("post", step), message_id, reply_markup=cancel_markup())
                return True
            pages = await asyncio.to_thread(
                self.storage.list_pages,
                str(session.get("account_id") or ""),
                self.account_owner_scope(user_id),
            )
            if not pages:
                self.clear_dashboard_session(chat_id, user_id)
                await self.send_message(
                    chat_id,
                    "No stored pages for this account. Run Discover Pages first.",
                    message_id,
                    reply_markup=await self.dashboard_reply_markup(user_id),
                )
                return True
            media_path = await self.download_file(file_id, str(session.get("account_id") or ""))
            self.clear_dashboard_session(chat_id, user_id)
            await self.queue_bulk_post_jobs_or_report(
                chat_id,
                user_id,
                str(session.get("account_id") or ""),
                pages,
                post_type,
                text.strip(),
                media_path,
            )
            return True

        if action == "post" and step == "media_video_url":
            video_url = await self.validate_video_url_or_reply(chat_id, message_id, text.strip())
            if not video_url:
                return True
            session["post_type"] = "video"
            session["caption"] = ""
            session["media_path"] = video_url
            self.set_dashboard_session(chat_id, user_id, session)
            await self.show_post_review(chat_id, message_id, user_id, session)
            return True

        if action == "post" and step == "multi_video_upload":
            file_id = self.extract_media_file_id(message, "video")
            if not file_id:
                await self.send_message(chat_id, self.multi_video_prompt(session), message_id, reply_markup=cancel_markup())
                return True
            selected_pages = self.selected_pages_from_session(session)
            if not selected_pages:
                self.clear_dashboard_session(chat_id, user_id)
                await self.send_message(chat_id, "No pages selected. Start again from the dashboard.", message_id, reply_markup=await self.dashboard_reply_markup(user_id))
                return True
            media_path = await self.download_file(file_id, str(session.get("account_id") or ""))
            paths = list(session.get("multi_media_paths") or [])
            paths.append(media_path)
            session["post_type"] = "video"
            session["multi_media_paths"] = paths
            session["media_path"] = ""
            if len(paths) < len(selected_pages):
                self.set_dashboard_session(chat_id, user_id, session)
                await self.send_message(
                    chat_id,
                    "\n".join([f"✅ Video {len(paths)}/{len(selected_pages)} received.", "", self.multi_video_prompt(session)]),
                    message_id,
                    reply_markup=cancel_markup(),
                )
                return True
            session["step"] = "multi_caption"
            self.set_dashboard_session(chat_id, user_id, session)
            await self.send_message(
                chat_id,
                "\n".join(
                    [
                        f"✅ All {len(selected_pages)} videos received.",
                        "",
                        "Send one shared caption for all posts.",
                        "Send a single space to post without caption.",
                    ]
                ),
                message_id,
                reply_markup=cancel_markup(),
            )
            return True

        if action == "post" and step == "multi_video_url":
            video_url = await self.validate_video_url_or_reply(chat_id, message_id, text.strip())
            if not video_url:
                return True
            selected_pages = self.selected_pages_from_session(session)
            if not selected_pages:
                self.clear_dashboard_session(chat_id, user_id)
                await self.send_message(chat_id, "No pages selected. Start again from the dashboard.", message_id, reply_markup=await self.dashboard_reply_markup(user_id))
                return True
            paths = list(session.get("multi_media_paths") or [])
            paths.append(video_url)
            session["post_type"] = "video"
            session["multi_media_paths"] = paths
            session["media_path"] = ""
            if len(paths) < len(selected_pages):
                self.set_dashboard_session(chat_id, user_id, session)
                await self.send_message(
                    chat_id,
                    "\n".join([f"✅ Video URL {len(paths)}/{len(selected_pages)} saved.", "", self.multi_video_url_prompt(session)]),
                    message_id,
                    reply_markup=cancel_markup(),
                )
                return True
            session["step"] = "multi_caption"
            self.set_dashboard_session(chat_id, user_id, session)
            await self.send_message(
                chat_id,
                "\n".join(
                    [
                        f"✅ All {len(selected_pages)} video URLs received.",
                        "",
                        "Send one shared caption for all posts.",
                        "Send a single space to post without caption.",
                    ]
                ),
                message_id,
                reply_markup=cancel_markup(),
            )
            return True

        if action == "post" and step == "multi_caption":
            session["caption"] = "" if text == " " else text.strip()
            self.set_dashboard_session(chat_id, user_id, session)
            await self.show_post_review(chat_id, message_id, user_id, session)
            return True

        if action == "post" and step in {"media_image", "media_video"}:
            post_type = str(session.get("post_type") or "").strip()
            file_id = self.extract_media_file_id(message, post_type)
            if not file_id:
                await self.send_message(chat_id, prompt_text("post", step), message_id, reply_markup=cancel_markup())
                return True
            media_path = await self.download_file(file_id, str(session.get("account_id") or ""))
            session["post_type"] = post_type
            session["caption"] = text.strip()
            session["media_path"] = media_path
            self.set_dashboard_session(chat_id, user_id, session)
            await self.show_post_review(chat_id, message_id, user_id, session)
            return True

        self.clear_dashboard_session(chat_id, user_id)
        await self.show_dashboard(chat_id, message_id, prefix="The previous dashboard flow expired.", user_id=user_id)
        return True

    async def handle_callback_query(self, update: Dict[str, Any]) -> None:
        query = update.get("callback_query") or {}
        callback_query_id = str(query.get("id") or "")
        data = str(query.get("data") or "")
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        user = query.get("from") or {}
        chat_id = int(chat.get("id") or 0)
        user_id = int(user.get("id") or 0)
        message_id = int(message.get("message_id") or 0)
        if callback_query_id:
            await self.answer_callback_query(callback_query_id)
        if user_id and chat_id:
            self.start_background_task(self.touch_user_seen(user_id, chat_id), f"touch user {user_id}")

        if data == "dash:back":
            self.clear_dashboard_session(chat_id, user_id)
            await self.show_dashboard(chat_id, message_id, user_id=user_id)
            return

        session = self.get_dashboard_session(chat_id, user_id)
        if not session:
            await self.show_dashboard(chat_id, message_id, prefix="This card expired. Dashboard refreshed.", user_id=user_id)
            return

        if data.startswith("pg:"):
            pages = session.get("pages") if isinstance(session.get("pages"), list) else []
            selected = session.get("selected_pages") if isinstance(session.get("selected_pages"), list) else []
            if data == "pg:all":
                selected = list(range(len(pages)))
                session["selected_pages"] = selected
                self.set_dashboard_session(chat_id, user_id, session)
                await self.edit_page_selection_card(chat_id, message_id, user_id, session)
                return
            if data == "pg:confirm":
                if not selected:
                    await self.edit_page_selection_card(chat_id, message_id, user_id, session, prefix="Select at least one page first.")
                    return
                if session.get("post_type"):
                    await self.show_post_input_card(chat_id, message_id, user_id, session)
                    return
                await self.show_post_type_card(chat_id, message_id, user_id, session)
                return
            if data == "pg:refresh":
                account_id = str(session.get("account_id") or "")
                self.clear_dashboard_session(chat_id, user_id)
                await self.command_discover_pages(chat_id, message_id, [account_id], user_id, refresh=True)
                return
            try:
                idx = int(data.split(":", 1)[1])
            except ValueError:
                return
            if 0 <= idx < len(pages):
                selected_set = set(selected)
                if idx in selected_set:
                    selected_set.remove(idx)
                else:
                    selected_set.add(idx)
                session["selected_pages"] = sorted(selected_set)
                self.set_dashboard_session(chat_id, user_id, session)
                await self.edit_page_selection_card(chat_id, message_id, user_id, session)
            return

        if data == "post:pages":
            session["step"] = "page_select"
            self.set_dashboard_session(chat_id, user_id, session)
            await self.edit_page_selection_card(chat_id, message_id, user_id, session)
            return

        if data.startswith("post:type:"):
            post_type = data.rsplit(":", 1)[-1]
            if post_type not in POST_TYPES:
                return
            session["post_type"] = post_type
            self.set_dashboard_session(chat_id, user_id, session)
            await self.show_post_input_card(chat_id, message_id, user_id, session)
            return

        if data.startswith("video:"):
            mode = data.split(":", 1)[1]
            selected_pages = self.selected_pages_from_session(session)
            if not selected_pages:
                await self.edit_page_selection_card(chat_id, message_id, user_id, session, prefix="Select at least one page first.")
                return
            session["post_type"] = "video"
            session["video_mode"] = mode
            session["caption"] = ""
            session.pop("media_path", None)
            session.pop("multi_media_paths", None)
            if mode == "single_upload":
                session["step"] = "media_video"
                self.set_dashboard_session(chat_id, user_id, session)
                await self.edit_message(chat_id, message_id, post_input_card("video"), reply_markup={"inline_keyboard": []})
                return
            if mode == "single_url":
                session["step"] = "media_video_url"
                self.set_dashboard_session(chat_id, user_id, session)
                await self.edit_message(
                    chat_id,
                    message_id,
                    "\n".join(
                        [
                            "🔗 Single Video URL",
                            "━━━━━━━━━━━━━━━━━━",
                            "Paste a direct http(s) video file URL.",
                            "The same video will be posted to all selected pages.",
                            "",
                            "After that, I will show a final review card.",
                        ]
                    ),
                    reply_markup={"inline_keyboard": []},
                )
                return
            if mode == "multi_upload":
                session["step"] = "multi_video_upload"
                session["multi_media_paths"] = []
                self.set_dashboard_session(chat_id, user_id, session)
                await self.edit_message(
                    chat_id,
                    message_id,
                    self.multi_video_prompt(session),
                    reply_markup={"inline_keyboard": []},
                )
                return
            if mode == "multi_url":
                session["step"] = "multi_video_url"
                session["multi_media_paths"] = []
                self.set_dashboard_session(chat_id, user_id, session)
                await self.edit_message(
                    chat_id,
                    message_id,
                    self.multi_video_url_prompt(session),
                    reply_markup={"inline_keyboard": []},
                )
                return
            await self.show_video_mode_card(chat_id, message_id, user_id, session)
            return

        if data == "post:edit_caption":
            session["step"] = "caption_edit"
            self.set_dashboard_session(chat_id, user_id, session)
            await self.edit_message(
                chat_id,
                message_id,
                "Send the new caption/text. Send a single space to clear it.",
                reply_markup={"inline_keyboard": []},
            )
            return

        if data == "post:confirm":
            draft = dict(session)
            self.clear_dashboard_session(chat_id, user_id)
            await self.edit_message(
                chat_id,
                message_id,
                progress_card("Posting...", 0, 1, "Queueing posting job..."),
                reply_markup={"inline_keyboard": []},
            )
            await self.queue_reviewed_post(chat_id, user_id, draft)
            return

    def update_chat_context(self, update: Dict[str, Any]) -> Tuple[int, int, int]:
        if update.get("callback_query"):
            query = update.get("callback_query") or {}
            message = query.get("message") or {}
            chat = message.get("chat") or {}
            user = query.get("from") or {}
        else:
            message = update.get("message") or update.get("edited_message") or {}
            chat = message.get("chat") or {}
            user = message.get("from") or {}
        return (
            int(chat.get("id") or 0),
            int(user.get("id") or 0),
            int(message.get("message_id") or 0),
        )

    async def handle_update_safe(self, update: Dict[str, Any]) -> None:
        trace_id = new_debug_id("upd")
        started = time.monotonic()
        chat_id, user_id, message_id = self.update_chat_context(update)
        update_kind = "callback" if update.get("callback_query") else "message"
        command = ""
        action = ""
        callback_data = ""
        if update.get("callback_query"):
            callback_data = str((update.get("callback_query") or {}).get("data") or "")[:80]
        else:
            message = update.get("message") or update.get("edited_message") or {}
            text = str(message.get("text") or message.get("caption") or "").strip()
            command, _args = split_command(text)
            action = dashboard_action(text)
        self.debug_event(
            "telegram_update_start",
            trace_id,
            kind=update_kind,
            chat_id=chat_id,
            user_id=user_id,
            message_id=message_id,
            command=command,
            action=action,
            callback_data=callback_data,
        )
        try:
            await self.handle_update(update)
            self.debug_event(
                "telegram_update_complete",
                trace_id,
                elapsed_seconds=round(time.monotonic() - started, 3),
                kind=update_kind,
                user_id=user_id,
            )
        except Exception as exc:
            logger.exception("Telegram dashboard update failed trace_id=%s", trace_id)
            self.debug_event(
                "telegram_update_failed",
                trace_id,
                elapsed_seconds=round(time.monotonic() - started, 3),
                kind=update_kind,
                user_id=user_id,
                error=compact_error(exc),
            )
            if not chat_id:
                return
            if user_id:
                self.clear_dashboard_session(chat_id, user_id)
            try:
                reply_markup = await self.dashboard_reply_markup(user_id)
            except Exception:
                reply_markup = dashboard_markup(has_accounts=False)
            with suppress(Exception):
                await self.send_message(
                    chat_id,
                    "\n".join(
                        [
                            "Dashboard action failed.",
                            "━━━━━━━━━━━━━━━━━━",
                            f"Debug ID: {trace_id}",
                            compact_error(exc),
                            "",
                            "The current flow was reset. Use /start or the dashboard buttons to continue.",
                        ]
                    ),
                    message_id,
                    reply_markup=reply_markup,
                )

    async def handle_update(self, update: Dict[str, Any]) -> None:
        if update.get("callback_query"):
            await self.handle_callback_query(update)
            return

        if not self.authorized(update):
            message = update.get("message") or {}
            chat_id = int((message.get("chat") or {}).get("id") or 0)
            if chat_id:
                await self.send_message(chat_id, "Unauthorized.")
            return

        message = update.get("message") or update.get("edited_message") or {}
        text = str(message.get("text") or message.get("caption") or "").strip()
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        chat_id = int(chat.get("id") or 0)
        user_id = int(user.get("id") or 0)
        message_id = int(message.get("message_id") or 0)
        command, args = split_command(text)
        action = dashboard_action(text)
        if user_id and chat_id:
            self.start_background_task(self.touch_user_seen(user_id, chat_id), f"touch user {user_id}")

        if command == "/start":
            self.clear_dashboard_session(chat_id, user_id)
            await self.show_dashboard(chat_id, message_id, user_id=user_id)
            return
        if action:
            await self.handle_dashboard_button(chat_id, user_id, message_id, action)
            return
        if await self.handle_dashboard_session(chat_id, user_id, message_id, text, message):
            return
        if command == "":
            await self.show_dashboard(chat_id, message_id, user_id=user_id)
            return
        await self.send_message(
            chat_id,
            "Only /start is supported. Use the dashboard buttons for all actions.",
            message_id,
            reply_markup=await self.dashboard_reply_markup(user_id),
        )

    async def command_add_account(
        self,
        chat_id: int,
        user_id: int,
        message_id: int,
        args: List[str],
        message: Dict[str, Any],
    ) -> None:
        account_hint = "auto"
        joined_args = " ".join(args).strip()
        if args and args[0] in {"auto", "-"}:
            account_hint = args[0]
            joined_args = " ".join(args[1:]).strip()
        elif args and "=" not in args[0] and not args[0].startswith(("{", "[")):
            account_hint = args[0]
            joined_args = " ".join(args[1:]).strip()

        cookie_payload = joined_args
        cookie_message_ids = [message_id]
        if not cookie_payload:
            try:
                cookie_payload, cookie_message_ids = await self.cookie_payload_from_message(message, "")
            except Exception as exc:
                await self.send_message(chat_id, f"Could not read cookie file: {exc}", message_id, reply_markup=cancel_markup())
                return

        if not cookie_payload:
            await self.send_message(
                chat_id,
                "Missing cookie payload. Use Add Facebook Account, reply to a cookie message, or upload a JSON file.",
                message_id,
                reply_markup=cancel_markup(),
            )
            return
        await self.save_account_cookie_payload(
            chat_id,
            user_id,
            message_id,
            cookie_payload,
            account_hint=account_hint,
            cookie_message_ids=cookie_message_ids,
        )

    async def command_accounts(self, chat_id: int, message_id: int, user_id: int = 0) -> None:
        accounts = await asyncio.to_thread(self.storage.list_accounts, self.account_owner_scope(user_id))
        if not accounts:
            await self.send_message(chat_id, "No accounts stored.", message_id, reply_markup=dashboard_markup(has_accounts=False))
            return
        active_account = ""
        if user_id:
            active_account = await self.active_account_id(user_id)
        lines = ["👤 Stored accounts:"]
        for item in accounts:
            status = "active" if item.get("active") else "inactive"
            marker = "✅" if item["account_id"] == active_account else "-"
            lines.append(f"{marker} {account_display_name(item)} ({status})")
        await self.send_message(chat_id, "\n".join(lines), message_id, reply_markup=await self.dashboard_reply_markup(user_id))

    async def command_check_cookies(self, chat_id: int, message_id: int, user_id: int = 0) -> None:
        accounts = await asyncio.to_thread(self.storage.list_accounts, self.account_owner_scope(user_id))
        if not accounts:
            await self.send_message(chat_id, "No accounts stored.", message_id, reply_markup=dashboard_markup(has_accounts=False))
            return

        total = len(accounts)
        progress = await self.send_message(
            chat_id,
            progress_card("Checking all cookies...", 0, total, "Preparing cookie validation..."),
            message_id,
        )
        progress_message_id = int((progress.get("result") or {}).get("message_id") or 0)

        lines = ["🧪 Cookie Validation Report", "━━━━━━━━━━━━━━━━━━━━━━"]
        valid_count = 0
        from playwright_engine import validate_facebook_session

        for index, account in enumerate(accounts, start=1):
            account_id = str(account.get("account_id") or "")
            display = account_display_name(account, account_id)
            progress_message_id = await self.edit_or_send_message(
                chat_id,
                progress_message_id,
                progress_card("Checking all cookies...", index - 1, total, f"Checking {display}..."),
            )
            try:
                cookie_header = await asyncio.to_thread(self.storage.get_account_cookie, account_id, self.account_owner_scope(user_id))
                parsed = parse_account_cookie_payload(cookie_header, account_id)
                session_ok, detail = await validate_facebook_session(cookies_json(parse_cookies(parsed.cookie_header)))
                icon, status_text = cookie_validation_summary(session_ok, detail)
                if session_ok:
                    valid_count += 1
                lines.append(f"{icon} {display}: {status_text}")
            except Exception as exc:
                lines.append(f"🔴 {display}: {str(exc)[:120]}")
            progress_message_id = await self.edit_or_send_message(
                chat_id,
                progress_message_id,
                progress_card("Checking all cookies...", index, total, f"Finished {display}."),
            )
        lines.extend(
            [
                "━━━━━━━━━━━━━━━━━━━━━━",
                f"Valid Facebook sessions: {valid_count}/{total}",
                f"Stored accounts checked: {len(accounts)}",
            ]
        )
        await self.edit_or_send_message(
            chat_id,
            progress_message_id,
            "\n".join(
                [
                    progress_card(
                        "Checking all cookies...",
                        total,
                        total,
                        f"Validation complete: {valid_count}/{total} session(s) valid.",
                    ),
                    "",
                    *lines,
                ]
            ),
            reply_to_message_id=message_id,
            reply_markup=await self.dashboard_reply_markup(user_id),
        )

    async def command_check_account(self, chat_id: int, message_id: int, user_id: int, account_id: str) -> None:
        account = await asyncio.to_thread(self.storage.get_account, account_id, self.account_owner_scope(user_id))
        if not account:
            await self.send_message(chat_id, f"Account not found: {account_id}", message_id, reply_markup=await self.dashboard_reply_markup(user_id))
            return
        display = account_display_name(account, account_id)
        progress = await self.send_message(
            chat_id,
            progress_card("Checking account cookie...", 0, 1, f"Validating {display}..."),
            message_id,
        )
        progress_message_id = int((progress.get("result") or {}).get("message_id") or 0)
        try:
            from playwright_engine import validate_facebook_session

            cookie_header = await asyncio.to_thread(self.storage.get_account_cookie, account_id, self.account_owner_scope(user_id))
            parsed = parse_account_cookie_payload(cookie_header, account_id)
            session_ok, detail = await validate_facebook_session(cookies_json(parse_cookies(parsed.cookie_header)))
            icon, status_text = cookie_validation_summary(session_ok, detail)
            status_line = f"{icon} {status_text}"
            hint = (
                "Continue to cached pages or refresh pages if Facebook page access changed."
                if session_ok
                else "Add/update this account again if Facebook reports the session as invalid."
            )
        except Exception as exc:
            status_line = f"🔴 Cookie payload is not usable: {str(exc)[:160]}"
            hint = "Add/update this account again."
        lines = [
            "🧪 Cookie Check",
            "━━━━━━━━━━━━━━━━━━",
            f"Account: {display}",
            status_line,
            "",
            hint,
        ]
        await self.edit_or_send_message(
            chat_id,
            progress_message_id,
            "\n".join([progress_card("Checking account cookie...", 1, 1, "Validation complete."), "", *lines]),
            reply_to_message_id=message_id,
            reply_markup=account_post_action_markup(),
        )

    async def command_post_history(self, chat_id: int, message_id: int, user_id: int = 0) -> None:
        summary = await self.dashboard_summary(user_id)
        recent_jobs = summary.get("recent_jobs") or []
        if not recent_jobs:
            await self.send_message(chat_id, "No post jobs yet.", message_id, reply_markup=await self.dashboard_reply_markup(user_id))
            return
        lines = ["📊 Post History", "━━━━━━━━━━━━━━━━━━━━━━"]
        for job in recent_jobs:
            page = page_display_name(
                {
                    "page_name": job.get("page_name"),
                    "page_url": job.get("page_id_or_url"),
                },
                -1,
            )[:42]
            lines.append(f"- {job.get('status')} | {job.get('account_id')} | {job.get('post_type')} | {page}")
        await self.send_message(chat_id, "\n".join(lines), message_id, reply_markup=await self.dashboard_reply_markup(user_id))

    async def command_remove_account(self, chat_id: int, message_id: int, args: List[str], user_id: int = 0) -> None:
        if len(args) != 1:
            await self.send_message(chat_id, "Choose an account from My Accounts to remove it.", message_id, reply_markup=await self.dashboard_reply_markup(user_id))
            return
        changed = await asyncio.to_thread(self.storage.deactivate_account, args[0], self.account_owner_scope(user_id))
        if changed and user_id:
            await asyncio.to_thread(self.storage.clear_active_account, user_id, args[0])
        await self.send_message(
            chat_id,
            "Account deactivated." if changed else "Account not found.",
            message_id,
            reply_markup=await self.dashboard_reply_markup(user_id),
        )

    async def command_discover_pages(
        self,
        chat_id: int,
        message_id: int,
        args: List[str],
        user_id: int = 0,
        *,
        refresh: bool = False,
    ) -> None:
        if len(args) != 1:
            await self.send_message(chat_id, "Choose an account from the dashboard first.", message_id, reply_markup=await self.dashboard_reply_markup(user_id))
            return
        account_id = args[0]
        verb = "Refreshing cached pages" if refresh else "Discovering pages"
        trace_id = new_debug_id("pages")
        self.debug_event(
            "page_discovery_start",
            trace_id,
            account_id=account_id,
            user_id=user_id,
            refresh=refresh,
        )
        progress = await self.send_message(
            chat_id,
            progress_card(f"{verb}...", 0, 3, f"Debug ID: {trace_id}\nPreparing Facebook session..."),
            message_id,
            reply_markup=cancel_markup(),
        )
        progress_message_id = int((progress.get("result") or {}).get("message_id") or 0)
        try:
            progress_message_id = await self.edit_or_send_message(
                chat_id,
                progress_message_id,
                progress_card(f"{verb}...", 1, 3, f"Debug ID: {trace_id}\nOpening pages manager..."),
                reply_to_message_id=message_id,
                reply_markup=cancel_markup(),
            )
            discovery_timeout = _env_int("BOT_PAGE_DISCOVERY_TIMEOUT_SECONDS", 150, minimum=30)
            heartbeat_seconds = _env_int("BOT_PROGRESS_HEARTBEAT_SECONDS", 8, minimum=2)
            started = time.monotonic()
            pages_task = asyncio.create_task(
                self.discover_pages(account_id, self.account_owner_scope(user_id))
            )
            tick = 0
            pages: List[Dict[str, str]] = []
            while True:
                remaining = discovery_timeout - (time.monotonic() - started)
                if remaining <= 0:
                    pages_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await pages_task
                    raise TimeoutError(
                        f"Page discovery timed out after {discovery_timeout}s. "
                        "Facebook or the Render browser worker did not respond in time."
                    )
                try:
                    pages = await asyncio.wait_for(
                        asyncio.shield(pages_task),
                        timeout=min(heartbeat_seconds, remaining),
                    )
                    break
                except asyncio.TimeoutError:
                    tick += 1
                    elapsed = int(time.monotonic() - started)
                    detail = (
                        f"Debug ID: {trace_id}\nDiscovering managed pages... {elapsed}s elapsed. "
                        "Still waiting for Facebook/browser response."
                    )
                    if tick % 3 == 0:
                        detail += " If this repeats, check Render logs for Playwright launch or Facebook login errors."
                    progress_message_id = await self.edit_or_send_message(
                        chat_id,
                        progress_message_id,
                        progress_card(f"{verb}...", 2, 3, detail),
                        reply_to_message_id=message_id,
                        reply_markup=cancel_markup(),
                    )
            if pages:
                progress_message_id = await self.edit_or_send_message(
                    chat_id,
                    progress_message_id,
                    progress_card(f"{verb}...", 2, 3, f"Debug ID: {trace_id}\nSaving {len(pages)} discovered page(s) to cache..."),
                    reply_to_message_id=message_id,
                    reply_markup=cancel_markup(),
                )
                await asyncio.to_thread(self.storage.upsert_pages, account_id, pages)
            if not pages:
                progress_message_id = await self.edit_or_send_message(
                    chat_id,
                    progress_message_id,
                    progress_card(f"{verb}...", 3, 3, f"Debug ID: {trace_id}\nNo managed pages discovered."),
                    reply_to_message_id=message_id,
                    reply_markup=await self.dashboard_reply_markup(user_id),
                )
                self.debug_event("page_discovery_empty", trace_id, account_id=account_id, elapsed_seconds=round(time.monotonic() - started, 3))
                await self.send_message(
                    chat_id,
                    "Dashboard keyboard restored.",
                    reply_markup=await self.dashboard_reply_markup(user_id),
                )
                return
            lines = [f"{'Refreshed' if refresh else 'Discovered'} and cached {len(pages)} page(s):"]
            for index, page in enumerate(pages):
                lines.append(f"- {page_display_name(page, index)}")
            progress_message_id = await self.edit_or_send_message(
                chat_id,
                progress_message_id,
                "\n".join([progress_card(f"{verb}...", 3, 3, f"Debug ID: {trace_id}\nPages saved to cache."), "", *lines]),
                reply_to_message_id=message_id,
                reply_markup=await self.dashboard_reply_markup(user_id),
            )
            self.debug_event(
                "page_discovery_complete",
                trace_id,
                account_id=account_id,
                page_count=len(pages),
                elapsed_seconds=round(time.monotonic() - started, 3),
            )
            await self.send_message(
                chat_id,
                "Dashboard keyboard restored.",
                reply_markup=await self.dashboard_reply_markup(user_id),
            )
        except Exception as exc:
            logger.exception("Page discovery failed")
            self.debug_event("page_discovery_failed", trace_id, account_id=account_id, error=compact_error(exc))
            await self.edit_or_send_message(
                chat_id,
                progress_message_id,
                "\n".join(
                    [
                        progress_card(f"{verb}...", 3, 3, "Page discovery failed."),
                        "",
                        f"Debug ID: {trace_id}",
                        compact_error(exc),
                        "",
                        "Use /start to refresh the dashboard, or try Stored Pages if pages were already cached.",
                    ]
                ),
                reply_to_message_id=message_id,
                reply_markup=await self.dashboard_reply_markup(user_id),
            )
            await self.send_message(
                chat_id,
                "Dashboard keyboard restored.",
                reply_markup=await self.dashboard_reply_markup(user_id),
            )

    async def discover_pages(self, account_id: str, owner_id: Optional[int] = None) -> List[Dict[str, str]]:
        from playwright.async_api import async_playwright

        cookie_string = await asyncio.to_thread(self.storage.get_account_cookie, account_id, owner_id)
        try:
            from playwright_engine import discover_facebook_pages

            ok, pages, detail = await discover_facebook_pages(cookies_json(parse_cookies(cookie_string)))
            if ok and pages:
                return pages
            if detail:
                logger.warning("Engine page discovery did not return pages: %s", detail[:500])
        except Exception as exc:
            logger.warning("Engine page discovery failed; falling back to lightweight discovery: %s", exc)

        cookies = parse_cookies(cookie_string)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=_env_bool("HEADLESS", True),
                args=["--disable-dev-shm-usage", "--no-sandbox"],
            )
            try:
                return await discover_pages_from_browser(browser, cookies)
            finally:
                await browser.close()

    async def command_list_pages(self, chat_id: int, message_id: int, args: List[str], user_id: int = 0) -> None:
        if len(args) != 1:
            await self.send_message(chat_id, "Choose Stored Pages from the dashboard.", message_id, reply_markup=await self.dashboard_reply_markup(user_id))
            return
        pages = await asyncio.to_thread(self.storage.list_pages, args[0], self.account_owner_scope(user_id))
        account = await asyncio.to_thread(self.storage.get_account, args[0], self.account_owner_scope(user_id))
        account_name = account_display_name(account or {}, args[0])
        if not pages:
            await self.send_message(
                chat_id,
                f"No pages stored for {account_name}. Tap Refresh Pages first.",
                message_id,
                reply_markup=await self.dashboard_reply_markup(user_id),
            )
            return
        lines = [f"Stored pages for {account_name}:"]
        for index, page in enumerate(pages):
            lines.append(f"- {page_display_name(page, index)}")
        await self.send_message(chat_id, "\n".join(lines), message_id, reply_markup=await self.dashboard_reply_markup(user_id))

    async def queue_post_job(
        self,
        chat_id: int,
        user_id: int,
        account_id: str,
        page_id_or_url: str,
        post_type: str,
        caption: str,
        media_path: str,
        page_name: str = "",
    ) -> str:
        if post_type not in POST_TYPES:
            raise ValueError(f"post_type must be one of {sorted(POST_TYPES)}")
        if not account_id:
            raise ValueError("account_id is required")
        if not page_id_or_url:
            raise ValueError("page_id_or_url is required")
        if post_type in {"image", "video"} and not media_path:
            raise ValueError(f"{post_type} media is required")
        if not await asyncio.to_thread(self.storage.account_exists, account_id, True, self.account_owner_scope(user_id)):
            raise ValueError("Active account not found for this Telegram user")

        job_id = await asyncio.to_thread(
            self.storage.create_post_job,
            telegram_chat_id=chat_id,
            telegram_user_id=user_id,
            account_id=account_id,
            page_id_or_url=page_id_or_url,
            page_name=page_name,
            post_type=post_type,
            caption=caption,
            media_path=media_path,
        )
        await self.send_message(
            chat_id,
            f"Queued post job {job_id}.",
            reply_markup=await self.dashboard_reply_markup(user_id),
        )
        self.start_background_task(
            self.run_post_job(job_id, chat_id, user_id, account_id, page_id_or_url, post_type, caption, media_path, page_name),
            f"post job {job_id}",
        )
        return job_id

    async def queue_bulk_post_jobs(
        self,
        chat_id: int,
        user_id: int,
        account_id: str,
        pages: List[Dict[str, Any]],
        post_type: str,
        caption: str,
        media_path: str,
    ) -> List[str]:
        if post_type not in POST_TYPES:
            raise ValueError(f"post_type must be one of {sorted(POST_TYPES)}")
        if post_type in {"image", "video"} and not media_path:
            raise ValueError(f"{post_type} media is required")
        if not pages:
            raise ValueError("At least one stored page is required")
        if not await asyncio.to_thread(self.storage.account_exists, account_id, True, self.account_owner_scope(user_id)):
            raise ValueError("Active account not found for this Telegram user")

        started = time.monotonic()
        jobs_to_create: List[Dict[str, Any]] = []
        job_payloads: List[Dict[str, str]] = []
        for page in pages:
            page_id_or_url = str(page.get("page_url") or page.get("page_id") or "").strip()
            page_name = page_display_name(page, len(job_payloads))
            if not page_id_or_url:
                continue
            jobs_to_create.append(
                {
                    "telegram_chat_id": chat_id,
                    "telegram_user_id": user_id,
                    "account_id": account_id,
                    "page_id_or_url": page_id_or_url,
                    "page_name": page_name,
                    "post_type": post_type,
                    "caption": caption,
                    "media_path": media_path,
                }
            )
            job_payloads.append(
                {
                    "job_id": "",
                    "page_id_or_url": page_id_or_url,
                    "page_name": page_name,
                    "post_type": post_type,
                    "caption": caption,
                    "media_path": media_path,
                }
            )

        job_ids = await asyncio.to_thread(self.storage.create_post_jobs, jobs_to_create)
        if len(job_ids) != len(job_payloads):
            raise RuntimeError(f"Created {len(job_ids)} job id(s) for {len(job_payloads)} queued post(s)")
        for index, job_id in enumerate(job_ids):
            job_payloads[index]["job_id"] = job_id

        if not job_ids:
            raise ValueError("No usable stored pages were found")
        self.debug_event(
            "queue_bulk_jobs_created",
            new_debug_id("queue"),
            account_id=account_id,
            job_count=len(job_ids),
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
        await self.send_message(
            chat_id,
            f"Queued {len(job_ids)} post job(s) for all stored pages.",
            reply_markup=await self.dashboard_reply_markup(user_id),
        )
        self.start_background_task(
            self.run_post_jobs_batch(chat_id, user_id, account_id, job_payloads),
            f"batch post {len(job_ids)} job(s)",
        )
        return job_ids

    async def queue_paired_post_jobs(
        self,
        chat_id: int,
        user_id: int,
        account_id: str,
        pages: List[Dict[str, Any]],
        post_type: str,
        caption: str,
        media_paths: List[str],
    ) -> List[str]:
        if post_type not in POST_TYPES:
            raise ValueError(f"post_type must be one of {sorted(POST_TYPES)}")
        if post_type not in {"image", "video"}:
            raise ValueError("paired media mode requires image or video post type")
        if not pages:
            raise ValueError("At least one selected page is required")
        if len(media_paths) != len(pages):
            raise ValueError(f"Media count ({len(media_paths)}) does not match selected pages ({len(pages)})")
        if not await asyncio.to_thread(self.storage.account_exists, account_id, True, self.account_owner_scope(user_id)):
            raise ValueError("Active account not found for this Telegram user")

        started = time.monotonic()
        jobs_to_create: List[Dict[str, Any]] = []
        job_payloads: List[Dict[str, str]] = []
        for index, page in enumerate(pages):
            page_id_or_url = str(page.get("page_url") or page.get("page_id") or "").strip()
            page_name = page_display_name(page, len(job_payloads))
            media_path = str(media_paths[index] or "").strip()
            if not page_id_or_url or not media_path:
                continue
            jobs_to_create.append(
                {
                    "telegram_chat_id": chat_id,
                    "telegram_user_id": user_id,
                    "account_id": account_id,
                    "page_id_or_url": page_id_or_url,
                    "page_name": page_name,
                    "post_type": post_type,
                    "caption": caption,
                    "media_path": media_path,
                }
            )
            job_payloads.append(
                {
                    "job_id": "",
                    "page_id_or_url": page_id_or_url,
                    "page_name": page_name,
                    "post_type": post_type,
                    "caption": caption,
                    "media_path": media_path,
                }
            )

        job_ids = await asyncio.to_thread(self.storage.create_post_jobs, jobs_to_create)
        if len(job_ids) != len(job_payloads):
            raise RuntimeError(f"Created {len(job_ids)} job id(s) for {len(job_payloads)} queued post(s)")
        for index, job_id in enumerate(job_ids):
            job_payloads[index]["job_id"] = job_id

        if not job_ids:
            raise ValueError("No usable selected pages/media were found")
        self.debug_event(
            "queue_paired_jobs_created",
            new_debug_id("queue"),
            account_id=account_id,
            job_count=len(job_ids),
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
        await self.send_message(
            chat_id,
            f"Queued {len(job_ids)} paired {post_type} post job(s).",
            reply_markup=await self.dashboard_reply_markup(user_id),
        )
        self.start_background_task(
            self.run_post_jobs_batch(chat_id, user_id, account_id, job_payloads),
            f"paired batch post {len(job_ids)} job(s)",
        )
        return job_ids

    async def queue_post_job_or_report(
        self,
        chat_id: int,
        user_id: int,
        account_id: str,
        page_id_or_url: str,
        post_type: str,
        caption: str,
        media_path: str,
        page_name: str = "",
    ) -> Optional[str]:
        try:
            return await self.queue_post_job(chat_id, user_id, account_id, page_id_or_url, post_type, caption, media_path, page_name)
        except Exception as exc:
            logger.exception("Could not queue post job")
            await self.send_message(
                chat_id,
                f"Could not queue post job: {str(exc)[:500]}",
                reply_markup=await self.dashboard_reply_markup(user_id),
            )
            return None

    async def queue_paired_post_jobs_or_report(
        self,
        chat_id: int,
        user_id: int,
        account_id: str,
        pages: List[Dict[str, Any]],
        post_type: str,
        caption: str,
        media_paths: List[str],
    ) -> List[str]:
        try:
            return await self.queue_paired_post_jobs(chat_id, user_id, account_id, pages, post_type, caption, media_paths)
        except Exception as exc:
            logger.exception("Could not queue paired post jobs")
            await self.send_message(
                chat_id,
                f"Could not queue paired post jobs: {str(exc)[:500]}",
                reply_markup=await self.dashboard_reply_markup(user_id),
            )
            return []

    async def queue_bulk_post_jobs_or_report(
        self,
        chat_id: int,
        user_id: int,
        account_id: str,
        pages: List[Dict[str, Any]],
        post_type: str,
        caption: str,
        media_path: str,
    ) -> List[str]:
        try:
            return await self.queue_bulk_post_jobs(chat_id, user_id, account_id, pages, post_type, caption, media_path)
        except Exception as exc:
            logger.exception("Could not queue bulk post jobs")
            await self.send_message(
                chat_id,
                f"Could not queue post jobs: {str(exc)[:500]}",
                reply_markup=await self.dashboard_reply_markup(user_id),
            )
            return []

    async def command_post(
        self,
        chat_id: int,
        user_id: int,
        message_id: int,
        args: List[str],
        message: Dict[str, Any],
    ) -> None:
        if len(args) < 3:
            await self.send_message(
                chat_id,
                "Use the dashboard post buttons to queue a post.",
                message_id,
                reply_markup=await self.dashboard_reply_markup(user_id),
            )
            return
        account_id, page_id_or_url, post_type = args[:3]
        post_type = post_type.lower()
        caption = " ".join(args[3:]).strip()
        if post_type not in POST_TYPES:
            await self.send_message(
                chat_id,
                f"post_type must be one of {sorted(POST_TYPES)}",
                message_id,
                reply_markup=await self.dashboard_reply_markup(user_id),
            )
            return

        media_path = ""
        file_id = self.extract_media_file_id(message, post_type)
        if post_type in {"image", "video"}:
            if file_id:
                media_path = await self.download_file(file_id, account_id)
            else:
                await self.send_message(
                    chat_id,
                    f"Attach or reply to a {post_type} file for this post.",
                    message_id,
                    reply_markup=await self.dashboard_reply_markup(user_id),
                )
                return

        await self.queue_post_job_or_report(
            chat_id,
            user_id,
            account_id,
            page_id_or_url,
            post_type,
            caption,
            media_path,
        )

    def engine_post_payload(
        self,
        page_id_or_url: str,
        page_name: str,
        post_type: str,
        caption: str,
        media_path: str,
    ) -> Dict[str, str]:
        return {
            "page_id_or_url": page_id_or_url,
            "page_name": page_name or page_id_or_url,
            "caption": caption,
            "post_type": post_type if post_type in {"image", "video"} else "post",
            "media_url": media_path,
        }

    async def run_post_job(
        self,
        job_id: str,
        chat_id: int,
        user_id: int,
        account_id: str,
        page_id_or_url: str,
        post_type: str,
        caption: str,
        media_path: str,
        page_name: str = "",
    ) -> None:
        lock_owner = f"telegram:{os.getpid()}:{job_id}:{uuid.uuid4().hex[:12]}"
        trace_id = new_debug_id("post")
        lock_acquired = False
        cookie_session_attempted = False
        heartbeat_task: Optional[asyncio.Task[None]] = None
        progress_message_id = 0
        total_units = 3
        try:
            self.debug_event(
                "post_job_start",
                trace_id,
                job_id=job_id,
                account_id=account_id,
                post_type=post_type,
                page=page_name or page_id_or_url,
            )
            progress = await self.send_message(
                chat_id,
                progress_card("Posting...", 0, total_units, f"Debug ID: {trace_id}\nQueued and waiting for account isolation slot."),
            )
            progress_message_id = int((progress.get("result") or {}).get("message_id") or 0)
            progress_message_id = await self.edit_or_send_message(
                chat_id,
                progress_message_id,
                progress_card("Posting...", 0, total_units, f"Debug ID: {trace_id}\nMarking job as processing..."),
            )
            storage_timeout_seconds = _env_int("BOT_STORAGE_OPERATION_TIMEOUT_SECONDS", 45, minimum=5)
            await asyncio.wait_for(
                asyncio.to_thread(self.storage.mark_job_started, job_id),
                timeout=storage_timeout_seconds,
            )

            async def lock_progress(detail: str) -> None:
                nonlocal progress_message_id
                progress_message_id = await self.edit_or_send_message(
                    chat_id,
                    progress_message_id,
                    progress_card("Posting...", 0, total_units, f"Debug ID: {trace_id}\n{detail}"),
                )

            lock_acquired = await self.wait_for_account_slot(account_id, lock_owner, chat_id, lock_progress, trace_id=trace_id)
            self.debug_event("post_job_lock_acquired", trace_id, job_id=job_id, account_id=account_id)
            progress_message_id = await self.edit_or_send_message(
                chat_id,
                progress_message_id,
                progress_card("Posting...", 1, total_units, f"Debug ID: {trace_id}\nAccount slot acquired."),
            )
            heartbeat_task = asyncio.create_task(self.account_lock_heartbeat(account_id, lock_owner))
            self.debug_event("post_job_cookie_load_start", trace_id, job_id=job_id, account_id=account_id)
            cookie_string = await asyncio.to_thread(self.storage.get_account_cookie, account_id, self.account_owner_scope(user_id))
            self.debug_event("post_job_cookie_loaded", trace_id, job_id=job_id, account_id=account_id)
            progress_message_id = await self.edit_or_send_message(
                chat_id,
                progress_message_id,
                progress_card("Posting...", 2, total_units, f"Debug ID: {trace_id}\nCookie loaded. Opening Facebook composer..."),
            )
            post = self.engine_post_payload(page_id_or_url, page_name or page_id_or_url, post_type, caption, media_path)
            self.debug_event("post_job_engine_import_start", trace_id, job_id=job_id)
            from playwright_engine import create_facebook_posts
            self.debug_event("post_job_engine_import_complete", trace_id, job_id=job_id)

            cookie_session_attempted = True
            progress_markup = await self.dashboard_reply_markup(user_id)
            progress_callback = self.browser_progress_callback(
                chat_id,
                progress_message_id,
                user_id,
                "Posting...",
                1,
                progress_markup,
                base_done=2,
            )
            engine_timeout = _env_int("BOT_POSTING_ENGINE_TIMEOUT_SECONDS", 900, minimum=60)
            heartbeat_seconds = _env_int("BOT_PROGRESS_HEARTBEAT_SECONDS", 8, minimum=2)

            async def engine_tick(elapsed: int) -> None:
                nonlocal progress_message_id
                progress_message_id = await self.edit_or_send_message(
                    chat_id,
                    progress_message_id,
                    progress_card(
                        "Posting...",
                        2,
                        total_units,
                        f"Debug ID: {trace_id}\nBrowser posting is still running... {elapsed}s elapsed. Waiting for Facebook result.",
                    ),
                )

            results_task = asyncio.create_task(
                create_facebook_posts(cookies_json(parse_cookies(cookie_string)), [post], progress_callback=progress_callback)
            )
            self.debug_event("post_job_engine_start", trace_id, job_id=job_id, timeout_seconds=engine_timeout)
            results = await self.wait_for_task_with_heartbeat(
                results_task,
                timeout_seconds=engine_timeout,
                heartbeat_seconds=heartbeat_seconds,
                on_tick=engine_tick,
                timeout_message=f"Posting timed out after {engine_timeout}s while waiting for Facebook.",
            )
            progress_message_id = await self.edit_or_send_message(
                chat_id,
                progress_message_id,
                progress_card("Posting...", total_units, total_units, f"Debug ID: {trace_id}\nFacebook returned a posting result."),
            )
            result = results[0] if results else {"success": False, "status": "no_result"}
            success = bool(result.get("success"))
            error = "" if success else str(result.get("result") or result.get("status") or result.get("error") or "posting failed")
            await asyncio.to_thread(self.storage.mark_job_completed, job_id, success, result, error)
            self.debug_event("post_job_complete", trace_id, job_id=job_id, success=success, error=error[:300])
            if success:
                progress_message_id = await self.edit_or_send_message(
                    chat_id,
                    progress_message_id,
                    progress_card("Posting...", total_units, total_units, f"Debug ID: {trace_id}\nPost job {job_id} succeeded."),
                    reply_markup=await self.dashboard_reply_markup(user_id),
                )
            else:
                progress_message_id = await self.edit_or_send_message(
                    chat_id,
                    progress_message_id,
                    progress_card("Posting...", total_units, total_units, f"Debug ID: {trace_id}\nPost job {job_id} failed: {error[:500]}"),
                    reply_markup=await self.dashboard_reply_markup(user_id),
                )
        except Exception as exc:
            logger.exception("Post job failed")
            self.debug_event("post_job_failed", trace_id, job_id=job_id, error=compact_error(exc))
            await asyncio.to_thread(self.storage.mark_job_completed, job_id, False, {"exception": str(exc)}, str(exc))
            if progress_message_id:
                await self.edit_or_send_message(
                    chat_id,
                    progress_message_id,
                    "\n".join([progress_card("Posting...", total_units, total_units, "Post job failed."), "", f"Debug ID: {trace_id}", compact_error(exc)]),
                    reply_markup=await self.dashboard_reply_markup(user_id),
                )
            else:
                await self.send_message(chat_id, f"Post job {job_id} failed: {exc}", reply_markup=await self.dashboard_reply_markup(user_id))
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
            if lock_acquired:
                await asyncio.to_thread(
                    self.storage.release_account_runtime,
                    account_id,
                    lock_owner,
                    cookie_session_attempted,
                )

    async def run_post_jobs_batch(
        self,
        chat_id: int,
        user_id: int,
        account_id: str,
        jobs: List[Dict[str, str]],
    ) -> None:
        batch_id = uuid.uuid4().hex[:12]
        lock_owner = f"telegram:{os.getpid()}:batch:{batch_id}"
        trace_id = new_debug_id("batch")
        lock_acquired = False
        cookie_session_attempted = False
        heartbeat_task: Optional[asyncio.Task[None]] = None
        progress_message_id = 0
        job_ids = [str(job["job_id"]) for job in jobs]
        page_statuses: Dict[str, Dict[str, Any]] = {
            str(job["job_id"]): {"status": "pending", "stage": "Pending"}
            for job in jobs
        }
        try:
            self.debug_event(
                "batch_post_start",
                trace_id,
                batch_id=batch_id,
                account_id=account_id,
                job_count=len(jobs),
                post_types=sorted({str(job.get("post_type") or "") for job in jobs}),
            )
            progress = await self.send_message(
                chat_id,
                posting_live_status_card(
                    "Batch posting...",
                    jobs,
                    page_statuses,
                    debug_id=trace_id,
                    active_detail=f"Queued {len(jobs)} page job(s).",
                ),
            )
            progress_message_id = int((progress.get("result") or {}).get("message_id") or 0)
            progress_message_id = await self.edit_or_send_message(
                chat_id,
                progress_message_id,
                posting_live_status_card(
                    "Batch posting...",
                    jobs,
                    page_statuses,
                    debug_id=trace_id,
                    active_detail=f"Marking {len(job_ids)} job(s) as processing...",
                ),
            )
            storage_timeout_seconds = _env_int("BOT_STORAGE_OPERATION_TIMEOUT_SECONDS", 45, minimum=5)
            await asyncio.wait_for(
                asyncio.to_thread(self.storage.mark_jobs_started, job_ids),
                timeout=storage_timeout_seconds,
            )
            self.debug_event("batch_post_jobs_marked_started", trace_id, batch_id=batch_id, job_count=len(job_ids))

            async def lock_progress(detail: str) -> None:
                nonlocal progress_message_id
                progress_message_id = await self.edit_or_send_message(
                    chat_id,
                    progress_message_id,
                    posting_live_status_card("Batch posting...", jobs, page_statuses, debug_id=trace_id, active_detail=detail),
                )

            lock_acquired = await self.wait_for_account_slot(account_id, lock_owner, chat_id, lock_progress, trace_id=trace_id)
            self.debug_event("batch_post_lock_acquired", trace_id, batch_id=batch_id, account_id=account_id)
            progress_message_id = await self.edit_or_send_message(
                chat_id,
                progress_message_id,
                posting_live_status_card("Batch posting...", jobs, page_statuses, debug_id=trace_id, active_detail="Account slot acquired."),
            )
            heartbeat_task = asyncio.create_task(self.account_lock_heartbeat(account_id, lock_owner))
            self.debug_event("batch_post_cookie_load_start", trace_id, batch_id=batch_id, account_id=account_id)
            cookie_string = await asyncio.to_thread(self.storage.get_account_cookie, account_id, self.account_owner_scope(user_id))
            self.debug_event("batch_post_cookie_loaded", trace_id, batch_id=batch_id, account_id=account_id)
            progress_message_id = await self.edit_or_send_message(
                chat_id,
                progress_message_id,
                posting_live_status_card(
                    "Batch posting...",
                    jobs,
                    page_statuses,
                    debug_id=trace_id,
                    active_detail="Cookie loaded. Posting cached pages...",
                ),
            )
            posts = [
                self.engine_post_payload(
                    str(job.get("page_id_or_url") or ""),
                    str(job.get("page_name") or ""),
                    str(job.get("post_type") or "text"),
                    str(job.get("caption") or ""),
                    str(job.get("media_path") or ""),
                )
                for job in jobs
            ]
            self.debug_event("batch_post_engine_import_start", trace_id, batch_id=batch_id)
            from playwright_engine import create_facebook_posts
            self.debug_event("batch_post_engine_import_complete", trace_id, batch_id=batch_id)

            cookie_session_attempted = True
            progress_markup = await self.dashboard_reply_markup(user_id)
            progress_lock = asyncio.Lock()
            last_progress_edit_at = 0.0
            last_progress_text = ""
            min_progress_edit_seconds = _env_float("BOT_PROGRESS_EDIT_MIN_SECONDS", 1.5, minimum=0.5)

            def status_key_for_event(event: Dict[str, Any]) -> str:
                raw_job_id = str(event.get("job_id") or "").strip()
                if raw_job_id in page_statuses:
                    return raw_job_id
                try:
                    event_index = int(event.get("index"))
                except Exception:
                    event_index = -1
                if 0 <= event_index < len(jobs):
                    return str(jobs[event_index].get("job_id") or "")
                event_page = str(event.get("page") or "").strip().lower()
                for job in jobs:
                    page_name = str(job.get("page_name") or "").strip().lower()
                    page_id_or_url = str(job.get("page_id_or_url") or "").strip().lower()
                    if event_page and event_page in {page_name, page_id_or_url}:
                        return str(job.get("job_id") or "")
                return ""

            async def progress_callback(event: Dict[str, Any]) -> None:
                nonlocal progress_message_id, last_progress_edit_at, last_progress_text
                key = status_key_for_event(event)
                if key:
                    completed = bool(event.get("completed"))
                    success = bool(event.get("success"))
                    stage = str(event.get("stage") or "").strip()
                    detail = str(event.get("detail") or event.get("result") or event.get("status") or event.get("error") or "").strip()
                    if completed:
                        status = "success" if success else "failed"
                        stage = "Done" if success else "Failed"
                    else:
                        status = "running"
                        stage = stage or "Working"
                    if detail and not completed:
                        stage = f"{stage}: {compact_text(detail, 52)}"
                    elif detail and completed and not success:
                        stage = f"{stage}: {compact_text(detail, 52)}"
                    page_statuses[key] = {"status": status, "stage": stage}

                done = sum(1 for state in page_statuses.values() if str(state.get("status") or "") in {"success", "failed", "skipped"})
                total = max(1, len(jobs))
                active_page = compact_text(event.get("page") or "", 42)
                active_stage = compact_text(event.get("stage") or "Posting pages...", 90)
                active_detail = f"{active_page}: {active_stage}" if active_page else active_stage
                text = posting_live_status_card(
                    "Batch posting...",
                    jobs,
                    page_statuses,
                    debug_id=trace_id,
                    active_detail=f"{active_detail} ({done}/{total})",
                )
                now = time.monotonic()
                if not bool(event.get("completed")) and text == last_progress_text:
                    return
                if not bool(event.get("completed")) and now - last_progress_edit_at < min_progress_edit_seconds:
                    return
                async with progress_lock:
                    now = time.monotonic()
                    if not bool(event.get("completed")) and text == last_progress_text:
                        return
                    if not bool(event.get("completed")) and now - last_progress_edit_at < min_progress_edit_seconds:
                        return
                    progress_message_id = await self.edit_or_send_message(
                        chat_id,
                        progress_message_id,
                        text,
                        reply_markup=progress_markup if bool(event.get("completed")) else None,
                    )
                    last_progress_text = text
                    last_progress_edit_at = now
            engine_timeout = _env_int(
                "BOT_BATCH_POSTING_ENGINE_TIMEOUT_SECONDS",
                max(300, len(posts) * 300),
                minimum=120,
            )
            heartbeat_seconds = _env_int("BOT_PROGRESS_HEARTBEAT_SECONDS", 8, minimum=2)

            async def engine_tick(elapsed: int) -> None:
                nonlocal progress_message_id
                progress_message_id = await self.edit_or_send_message(
                    chat_id,
                    progress_message_id,
                    posting_live_status_card(
                        "Batch posting...",
                        jobs,
                        page_statuses,
                        debug_id=trace_id,
                        active_detail=f"Browser still running... {elapsed}s elapsed. Waiting for Facebook result.",
                    ),
                )

            results_task = asyncio.create_task(
                create_facebook_posts(cookies_json(parse_cookies(cookie_string)), posts, progress_callback=progress_callback)
            )
            self.debug_event("batch_post_engine_start", trace_id, batch_id=batch_id, timeout_seconds=engine_timeout, post_count=len(posts))
            results = await self.wait_for_task_with_heartbeat(
                results_task,
                timeout_seconds=engine_timeout,
                heartbeat_seconds=heartbeat_seconds,
                on_tick=engine_tick,
                timeout_message=f"Batch posting timed out after {engine_timeout}s while waiting for Facebook.",
            )
            progress_message_id = await self.edit_or_send_message(
                chat_id,
                progress_message_id,
                posting_live_status_card(
                    "Batch posting...",
                    jobs,
                    page_statuses,
                    debug_id=trace_id,
                    active_detail="Facebook returned batch results.",
                ),
            )
            success_count = 0
            completions: List[Dict[str, Any]] = []
            result_items: List[Dict[str, Any]] = []
            for index, job in enumerate(jobs):
                result = results[index] if index < len(results) else {"success": False, "status": "no_result"}
                success = bool(result.get("success"))
                if success:
                    success_count += 1
                error = "" if success else str(result.get("result") or result.get("status") or result.get("error") or "posting failed")
                page = str(job.get("page_name") or job.get("page_id_or_url") or result.get("page") or "Unknown page")
                page_statuses[str(job["job_id"])] = {
                    "status": "success" if success else "failed",
                    "stage": "Done" if success else "Failed",
                }
                result_items.append(
                    {
                        "page": page,
                        "success": success,
                        "result": result.get("result") or result.get("status") or result.get("error") or error,
                    }
                )
                completions.append(
                    {
                        "job_id": str(job["job_id"]),
                        "success": success,
                        "result": result,
                        "error": error,
                    }
                )
                self.debug_event(
                    "batch_post_page_result",
                    trace_id,
                    batch_id=batch_id,
                    job_id=str(job["job_id"]),
                    page=page,
                    success=success,
                    error=error[:300],
                )
            await asyncio.to_thread(self.storage.mark_jobs_completed, completions)
            progress_message_id = await self.edit_or_send_message(
                chat_id,
                progress_message_id,
                posting_result_card(result_items, debug_id=trace_id),
                reply_markup=await self.dashboard_reply_markup(user_id),
            )
            self.debug_event("batch_post_complete", trace_id, batch_id=batch_id, success_count=success_count, total=len(jobs))
        except Exception as exc:
            logger.exception("Batch post job failed")
            self.debug_event("batch_post_failed", trace_id, batch_id=batch_id, error=compact_error(exc))
            with suppress(Exception):
                await asyncio.to_thread(
                    self.storage.mark_jobs_completed,
                    [
                        {
                            "job_id": job_id,
                            "success": False,
                            "result": {"exception": str(exc)},
                            "error": str(exc),
                        }
                        for job_id in job_ids
                    ],
                )
            if progress_message_id:
                failed_results = [
                    {
                        "page": job.get("page_name") or job.get("page_id_or_url") or "Unknown page",
                        "success": False,
                        "result": compact_error(exc),
                    }
                    for job in jobs
                ]
                await self.edit_or_send_message(
                    chat_id,
                    progress_message_id,
                    posting_result_card(
                        failed_results,
                        title=f"Posting complete: 0/{len(jobs)} succeeded",
                        debug_id=trace_id,
                    ),
                    reply_markup=await self.dashboard_reply_markup(user_id),
                )
            else:
                await self.send_message(chat_id, f"Batch posting failed: {exc}", reply_markup=await self.dashboard_reply_markup(user_id))
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
            if lock_acquired:
                await asyncio.to_thread(
                    self.storage.release_account_runtime,
                    account_id,
                    lock_owner,
                    cookie_session_attempted,
                )

    async def wait_for_account_slot(
        self,
        account_id: str,
        owner: str,
        chat_id: int,
        progress_update: Optional[Callable[[str], Awaitable[None]]] = None,
        *,
        trace_id: str = "",
    ) -> bool:
        cooldown_seconds = _env_int(
            "BOT_ACCOUNT_COOKIE_COOLDOWN_SECONDS",
            _env_int("LIVE_MATRIX_COOKIE_COOLDOWN_SECONDS", 360, minimum=0),
            minimum=0,
        )
        lease_seconds = _env_int(
            "BOT_ACCOUNT_LOCK_LEASE_SECONDS",
            max(1800, cooldown_seconds + 900),
            minimum=120,
        )
        poll_seconds = _env_int("BOT_ACCOUNT_LOCK_POLL_SECONDS", 10, minimum=1)
        max_wait_seconds = _env_int("BOT_ACCOUNT_LOCK_MAX_WAIT_SECONDS", 3600, minimum=60)
        storage_timeout_seconds = _env_int("BOT_STORAGE_OPERATION_TIMEOUT_SECONDS", 45, minimum=5)
        started = time.monotonic()
        notified_wait = False

        async def notify(detail: str) -> None:
            if progress_update is not None:
                await progress_update(detail)
            else:
                await self.send_message(chat_id, detail)

        while True:
            await notify("Checking isolated account slot...")
            self.debug_event(
                "account_slot_claim_attempt",
                trace_id,
                account_id=account_id,
                owner=owner,
                lease_seconds=lease_seconds,
            )
            try:
                runtime = await asyncio.wait_for(
                    asyncio.to_thread(self.storage.claim_account_runtime, account_id, owner, lease_seconds),
                    timeout=storage_timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                self.debug_event(
                    "account_slot_claim_timeout",
                    trace_id,
                    account_id=account_id,
                    timeout_seconds=storage_timeout_seconds,
                )
                raise TimeoutError(
                    f"Timed out after {storage_timeout_seconds}s while checking account runtime lock. "
                    "Check DATABASE_URL/Supabase network connectivity."
                ) from exc
            if runtime:
                remaining = int(max(0, cooldown_seconds - seconds_since(runtime.get("last_cookie_used_at"))))
                if remaining > 0:
                    while remaining > 0:
                        await notify(
                            f"Account slot acquired. Cookie cooldown active: {remaining}s before browser posting starts."
                        )
                        sleep_for = min(max(1, poll_seconds), remaining)
                        await asyncio.sleep(sleep_for)
                        remaining = int(max(0, cooldown_seconds - seconds_since(runtime.get("last_cookie_used_at"))))
                return True

            if time.monotonic() - started > max_wait_seconds:
                raise RuntimeError(f"Timed out waiting for account lock: {account_id}")
            elapsed = int(time.monotonic() - started)
            if progress_update is not None:
                await notify(f"Account is busy; waiting for an isolated posting slot. {elapsed}s elapsed.")
            elif not notified_wait:
                await notify(f"Account {account_id} is busy; waiting for an isolated posting slot.")
                notified_wait = True
            await asyncio.sleep(poll_seconds)

    async def account_lock_heartbeat(self, account_id: str, owner: str) -> None:
        lease_seconds = _env_int("BOT_ACCOUNT_LOCK_LEASE_SECONDS", 1800, minimum=120)
        heartbeat_seconds = _env_int("BOT_ACCOUNT_LOCK_HEARTBEAT_SECONDS", 30, minimum=5)
        while True:
            await asyncio.sleep(heartbeat_seconds)
            await asyncio.to_thread(self.storage.extend_account_runtime, account_id, owner, lease_seconds)

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def webhook(self, request: web.Request) -> web.Response:
        path_secret = request.match_info.get("secret", "")
        header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        authorized = False
        if header_secret:
            authorized = header_secret == self.webhook_secret
        elif path_secret:
            authorized = path_secret == self.webhook_secret
        if not authorized:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            update = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
        self.start_background_task(self.handle_update_safe(update), "telegram update")
        return web.json_response({"ok": True})


def create_app() -> web.Application:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    configure_access_log_filters()
    bot = TelegramBotApp()
    app = web.Application(client_max_size=int(os.getenv("AIOHTTP_CLIENT_MAX_SIZE", str(32 * 1024 * 1024))))
    app["bot"] = bot
    app.on_startup.append(bot.startup)
    app.on_cleanup.append(bot.cleanup)
    app.router.add_get("/", bot.health)
    app.router.add_get("/healthz", bot.health)
    app.router.add_post("/telegram/webhook", bot.webhook)
    app.router.add_post("/telegram/webhook/{secret}", bot.webhook)
    return app


def main() -> None:
    app = create_app()
    port = int(os.getenv("PORT", "10000"))
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
