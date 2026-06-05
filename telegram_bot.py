#!/usr/bin/env python3
"""Telegram webhook service for Render deployment."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
    page_choice_label,
    parse_choice_id,
    parse_post_type_choice,
    prompt_text,
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

    def is_admin_user(self, user_id: int) -> bool:
        return bool(self.admin_ids and int(user_id or 0) in self.admin_ids)

    async def startup(self, app: web.Application) -> None:
        self.session = ClientSession()
        if _env_bool("AUTO_INIT_DB", True):
            await asyncio.to_thread(self.storage.ensure_schema)
            logger.info("Supabase/Postgres schema ready")
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        if _env_bool("AUTO_SET_TELEGRAM_WEBHOOK", True):
            await self.configure_telegram_webhook()
        if _env_bool("RESTART_BROADCAST_ENABLED", True):
            asyncio.create_task(self.notify_restart_dashboard())

    async def cleanup(self, app: web.Application) -> None:
        if self.session is not None:
            await self.session.close()

    def authorized(self, update: Dict[str, Any]) -> bool:
        if not self.admin_ids:
            return True
        message = update.get("message") or update.get("edited_message") or {}
        user = message.get("from") or {}
        chat = message.get("chat") or {}
        return int(user.get("id") or 0) in self.admin_ids or int(chat.get("id") or 0) in self.admin_ids

    async def telegram_api(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self.session is None:
            raise RuntimeError("HTTP session is not ready")
        async with self.session.post(f"{self.api_base}/{method}", json=payload, timeout=60) as resp:
            data = await resp.json(content_type=None)
            if not data.get("ok"):
                logger.warning("Telegram API %s failed: %s", method, data)
            return data

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
            "allowed_updates": ["message", "edited_message"],
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
            summary = await asyncio.to_thread(self.storage.dashboard_summary)
            accounts = await asyncio.to_thread(self.storage.list_accounts)
            sent = 0
            for target in targets:
                user_id = int(target.get("telegram_user_id") or 0)
                chat_id = int(target.get("chat_id") or user_id or 0)
                if not chat_id or not user_id:
                    continue
                active_account = await asyncio.to_thread(self.storage.get_active_account, user_id)
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
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        await self.telegram_api("editMessageText", payload)

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

    async def dashboard_accounts(self) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.storage.list_accounts)

    async def dashboard_reply_markup(self, user_id: int = 0) -> Dict[str, Any]:
        accounts = await self.dashboard_accounts()
        summary = await asyncio.to_thread(self.storage.dashboard_summary)
        active_account = ""
        if user_id:
            active_account = await asyncio.to_thread(self.storage.get_active_account, user_id)
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
            accounts = await self.dashboard_accounts()
            summary = await asyncio.to_thread(self.storage.dashboard_summary)
            active_account = ""
            if user_id:
                active_account = await asyncio.to_thread(self.storage.get_active_account, user_id)
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
                f"- {account_display_name(row, str(row.get('account_id') or ''))} ({status}) | pages={row.get('page_count', 0)} | "
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

    async def prompt_for_account(self, chat_id: int, message_id: int, prompt: str, user_id: int = 0) -> bool:
        accounts = await self.dashboard_accounts()
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
            active_account = await asyncio.to_thread(self.storage.get_active_account, user_id)
        await self.send_message(
            chat_id,
            prompt,
            message_id,
            reply_markup=choices_markup((account_choice_label(item, active_account) for item in accounts), placeholder="Choose account"),
        )
        return True

    async def prompt_for_page(self, chat_id: int, message_id: int, account_id: str) -> None:
        pages = await asyncio.to_thread(self.storage.list_pages, account_id)
        if pages:
            labels = [page_choice_label(page) for page in pages[:24]]
            await self.send_message(
                chat_id,
                prompt_text("post", "page"),
                message_id,
                reply_markup=choices_markup(labels, placeholder="Choose page"),
            )
            return
        await self.send_message(
            chat_id,
            "No stored pages for this account yet. Tap Refresh Pages to discover and cache them, or type a page id / full page URL.",
            message_id,
            reply_markup=cancel_markup(),
        )

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
                f"Account ID: {account.get('account_id') or 'unknown'}",
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
        account = await asyncio.to_thread(self.storage.get_account, account_id)
        if not account:
            await self.send_message(chat_id, f"Account not found: {account_id}", message_id, reply_markup=await self.dashboard_reply_markup(user_id))
            return
        pages = await asyncio.to_thread(self.storage.list_pages, account_id)
        await self.send_message(
            chat_id,
            self.account_action_text(account, pages),
            message_id,
            reply_markup=account_post_action_markup(),
        )

    async def active_account_or_warn(self, chat_id: int, message_id: int, user_id: int) -> str:
        active_account = await asyncio.to_thread(self.storage.get_active_account, user_id)
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
            active_account = await asyncio.to_thread(self.storage.get_active_account, user_id)
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
                await self.prompt_for_page(chat_id, message_id, active_account)
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
                    "action": "post_all_pages" if action == "post_all_pages" else "post",
                    "account_id": active_account,
                    "step": "post_type",
                },
            )
            await self.send_message(
                chat_id,
                prompt_text("post", "post_type"),
                message_id,
                reply_markup=choices_markup(POST_TYPE_CHOICES, placeholder="Choose post type"),
            )
            return
        if action in {"discover_pages", "list_pages"}:
            self.set_dashboard_session(chat_id, user_id, {"action": action, "step": "account"})
            if not await self.prompt_for_account(chat_id, message_id, prompt_text(action, "account"), user_id):
                self.clear_dashboard_session(chat_id, user_id)
            return
        if action in POST_ACTION_TYPES:
            post_type = POST_ACTION_TYPES[action]
            active_account = await asyncio.to_thread(self.storage.get_active_account, user_id)
            if active_account:
                self.set_dashboard_session(
                    chat_id,
                    user_id,
                    {"action": "post", "post_type": post_type, "account_id": active_account, "step": "page"},
                )
                await self.prompt_for_page(chat_id, message_id, active_account)
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
            progress_card("Adding Facebook account...", 1, 4, "Cookies parsed."),
            message_id,
            reply_markup=cookie_input_markup(),
        )
        progress_message_id = int((progress.get("result") or {}).get("message_id") or 0)

        label = f"Facebook Account {parsed.account_id}"
        name_source = "From cookies"
        name_error = ""
        await self.edit_message(
            chat_id,
            progress_message_id,
            progress_card("Adding Facebook account...", 2, 4, "Looking up account name..."),
            reply_markup=cookie_input_markup(),
        )
        try:
            from playwright_engine import get_facebook_account_name

            resolved, resolved_name, lookup_error = await get_facebook_account_name(cookies_json(parsed.cookies))
            if resolved and resolved_name:
                label = resolved_name
                name_source = "Auto-detected"
            elif lookup_error:
                name_error = lookup_error
        except Exception as exc:
            name_error = str(exc)

        await self.edit_message(
            chat_id,
            progress_message_id,
            progress_card("Adding Facebook account...", 3, 4, f"Saving {label}..."),
            reply_markup=cookie_input_markup(),
        )
        await asyncio.to_thread(
            self.storage.upsert_account,
            parsed.account_id,
            parsed.cookie_header,
            label,
            user_id,
        )
        await asyncio.to_thread(self.storage.set_active_account, user_id, parsed.account_id)

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
                *([f"Name lookup note: {name_error[:160]}"] if name_error and name_source != "Auto-detected" else []),
            ]
        )
        await self.edit_message(
            chat_id,
            progress_message_id,
            progress_card("Adding Facebook account...", 4, 4, "Account saved."),
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
            account_id = parse_choice_id(text)
            if not account_id:
                if not await self.prompt_for_account(chat_id, message_id, prompt_text(action, "account"), user_id):
                    self.clear_dashboard_session(chat_id, user_id)
                return True
            if not await asyncio.to_thread(self.storage.account_exists, account_id):
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
                self.clear_dashboard_session(chat_id, user_id)
                await self.show_dashboard(chat_id, message_id, prefix=f"Active account switched to {account_id}.", user_id=user_id)
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
            session["step"] = "post_type" if not session.get("post_type") else "page"
            self.set_dashboard_session(chat_id, user_id, session)
            if session["step"] == "post_type":
                await self.send_message(
                    chat_id,
                    prompt_text("post", "post_type"),
                    message_id,
                    reply_markup=choices_markup(POST_TYPE_CHOICES, placeholder="Choose post type"),
                )
                return True
            await self.prompt_for_page(chat_id, message_id, account_id)
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
            await self.prompt_for_page(chat_id, message_id, str(session.get("account_id") or ""))
            return True

        if action == "post" and step == "page_then_type":
            page_id_or_url = parse_choice_id(text)
            if not page_id_or_url:
                await self.prompt_for_page(chat_id, message_id, str(session.get("account_id") or ""))
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
            session["step"] = "caption" if post_type == "text" else f"media_{post_type}"
            self.set_dashboard_session(chat_id, user_id, session)
            await self.send_message(chat_id, prompt_text("post", session["step"]), message_id, reply_markup=cancel_markup())
            return True

        if action == "post" and step == "page":
            page_id_or_url = parse_choice_id(text)
            if not page_id_or_url:
                await self.prompt_for_page(chat_id, message_id, str(session.get("account_id") or ""))
                return True
            post_type = str(session.get("post_type") or "text")
            session["page_id_or_url"] = page_id_or_url
            session["step"] = "caption" if post_type == "text" else f"media_{post_type}"
            self.set_dashboard_session(chat_id, user_id, session)
            await self.send_message(chat_id, prompt_text("post", session["step"]), message_id, reply_markup=cancel_markup())
            return True

        if action == "post" and step == "caption":
            caption = text.strip()
            if not caption:
                await self.send_message(chat_id, "Caption cannot be empty. Send the post text.", message_id, reply_markup=cancel_markup())
                return True
            self.clear_dashboard_session(chat_id, user_id)
            await self.queue_post_job(
                chat_id,
                user_id,
                str(session.get("account_id") or ""),
                str(session.get("page_id_or_url") or ""),
                "text",
                caption,
                "",
            )
            return True

        if action == "post_all_pages" and step == "caption_all":
            caption = text.strip()
            if not caption:
                await self.send_message(chat_id, "Caption cannot be empty. Send the post text.", message_id, reply_markup=cancel_markup())
                return True
            pages = await asyncio.to_thread(self.storage.list_pages, str(session.get("account_id") or ""))
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
            await self.queue_bulk_post_jobs(
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
            pages = await asyncio.to_thread(self.storage.list_pages, str(session.get("account_id") or ""))
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
            await self.queue_bulk_post_jobs(
                chat_id,
                user_id,
                str(session.get("account_id") or ""),
                pages,
                post_type,
                text.strip(),
                media_path,
            )
            return True

        if action == "post" and step in {"media_image", "media_video"}:
            post_type = str(session.get("post_type") or "").strip()
            file_id = self.extract_media_file_id(message, post_type)
            if not file_id:
                await self.send_message(chat_id, prompt_text("post", step), message_id, reply_markup=cancel_markup())
                return True
            media_path = await self.download_file(file_id, str(session.get("account_id") or ""))
            self.clear_dashboard_session(chat_id, user_id)
            await self.queue_post_job(
                chat_id,
                user_id,
                str(session.get("account_id") or ""),
                str(session.get("page_id_or_url") or ""),
                post_type,
                text.strip(),
                media_path,
            )
            return True

        self.clear_dashboard_session(chat_id, user_id)
        await self.show_dashboard(chat_id, message_id, prefix="The previous dashboard flow expired.", user_id=user_id)
        return True

    async def handle_update(self, update: Dict[str, Any]) -> None:
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
            asyncio.create_task(self.touch_user_seen(user_id, chat_id))

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
        accounts = await asyncio.to_thread(self.storage.list_accounts)
        if not accounts:
            await self.send_message(chat_id, "No accounts stored.", message_id, reply_markup=dashboard_markup(has_accounts=False))
            return
        active_account = ""
        if user_id:
            active_account = await asyncio.to_thread(self.storage.get_active_account, user_id)
        lines = ["👤 Stored accounts:"]
        for item in accounts:
            status = "active" if item.get("active") else "inactive"
            marker = "✅" if item["account_id"] == active_account else "-"
            lines.append(f"{marker} {account_display_name(item)} ({status})")
        await self.send_message(chat_id, "\n".join(lines), message_id, reply_markup=await self.dashboard_reply_markup(user_id))

    async def command_check_cookies(self, chat_id: int, message_id: int, user_id: int = 0) -> None:
        accounts = await asyncio.to_thread(self.storage.list_accounts)
        if not accounts:
            await self.send_message(chat_id, "No accounts stored.", message_id, reply_markup=dashboard_markup(has_accounts=False))
            return

        lines = ["🧪 Cookie Validation Report", "━━━━━━━━━━━━━━━━━━━━━━"]
        valid_count = 0
        for account in accounts:
            account_id = str(account.get("account_id") or "")
            if not account.get("active"):
                lines.append(f"🔴 {account_id}: inactive")
                continue
            try:
                cookie_header = await asyncio.to_thread(self.storage.get_account_cookie, account_id)
                parsed = parse_account_cookie_payload(cookie_header, account_id)
                has_session = any(part.startswith("xs=") for part in parsed.cookie_header.split("; "))
                if has_session:
                    valid_count += 1
                    lines.append(f"🟢 {account_id}: stored cookie shape is usable")
                else:
                    lines.append(f"🟡 {account_id}: c_user found, xs missing")
            except Exception as exc:
                lines.append(f"🔴 {account_id}: {str(exc)[:120]}")
        lines.extend(["━━━━━━━━━━━━━━━━━━━━━━", f"Usable locally: {valid_count}/{len(accounts)}"])
        await self.send_message(chat_id, "\n".join(lines), message_id, reply_markup=await self.dashboard_reply_markup(user_id))

    async def command_check_account(self, chat_id: int, message_id: int, user_id: int, account_id: str) -> None:
        account = await asyncio.to_thread(self.storage.get_account, account_id)
        if not account:
            await self.send_message(chat_id, f"Account not found: {account_id}", message_id, reply_markup=await self.dashboard_reply_markup(user_id))
            return
        display = account_display_name(account, account_id)
        try:
            cookie_header = await asyncio.to_thread(self.storage.get_account_cookie, account_id)
            parsed = parse_account_cookie_payload(cookie_header, account_id)
            has_xs = any(part.startswith("xs=") for part in parsed.cookie_header.split("; "))
            status_line = "🟢 Stored cookie shape is usable" if has_xs else "🟡 c_user found, xs missing"
            hint = "Continue to cached pages or refresh pages if Facebook page access changed."
        except Exception as exc:
            status_line = f"🔴 Cookie payload is not usable: {str(exc)[:160]}"
            hint = "Add/update this account again."
        lines = [
            "🧪 Cookie Check",
            "━━━━━━━━━━━━━━━━━━",
            f"Account: {display}",
            f"Account ID: {account_id}",
            status_line,
            "",
            hint,
        ]
        await self.send_message(chat_id, "\n".join(lines), message_id, reply_markup=account_post_action_markup())

    async def command_post_history(self, chat_id: int, message_id: int, user_id: int = 0) -> None:
        summary = await asyncio.to_thread(self.storage.dashboard_summary)
        recent_jobs = summary.get("recent_jobs") or []
        if not recent_jobs:
            await self.send_message(chat_id, "No post jobs yet.", message_id, reply_markup=await self.dashboard_reply_markup(user_id))
            return
        lines = ["📊 Post History", "━━━━━━━━━━━━━━━━━━━━━━"]
        for job in recent_jobs:
            page = str(job.get("page_id_or_url") or "")[:42]
            lines.append(f"- {job.get('status')} | {job.get('account_id')} | {job.get('post_type')} | {page}")
        await self.send_message(chat_id, "\n".join(lines), message_id, reply_markup=await self.dashboard_reply_markup(user_id))

    async def command_remove_account(self, chat_id: int, message_id: int, args: List[str], user_id: int = 0) -> None:
        if len(args) != 1:
            await self.send_message(chat_id, "Choose an account from My Accounts to remove it.", message_id, reply_markup=await self.dashboard_reply_markup(user_id))
            return
        changed = await asyncio.to_thread(self.storage.deactivate_account, args[0])
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
        progress = await self.send_message(
            chat_id,
            progress_card(f"{verb}...", 0, 3, "Preparing Facebook session..."),
            message_id,
            reply_markup=cancel_markup(),
        )
        progress_message_id = int((progress.get("result") or {}).get("message_id") or 0)
        try:
            await self.edit_message(
                chat_id,
                progress_message_id,
                progress_card(f"{verb}...", 1, 3, "Opening pages manager..."),
                reply_markup=cancel_markup(),
            )
            await self.edit_message(
                chat_id,
                progress_message_id,
                progress_card(f"{verb}...", 2, 3, "Discovering managed pages..."),
                reply_markup=cancel_markup(),
            )
            pages = await self.discover_pages(account_id)
            if pages:
                await asyncio.to_thread(self.storage.upsert_pages, account_id, pages)
            if not pages:
                await self.edit_message(
                    chat_id,
                    progress_message_id,
                    progress_card(f"{verb}...", 3, 3, "No managed pages discovered."),
                    reply_markup=await self.dashboard_reply_markup(user_id),
                )
                return
            lines = [f"{'Refreshed' if refresh else 'Discovered'} and cached {len(pages)} page(s):"]
            for page in pages:
                lines.append(f"- {page.get('name') or page.get('id')} | {page.get('url')}")
            await self.edit_message(
                chat_id,
                progress_message_id,
                "\n".join([progress_card(f"{verb}...", 3, 3, "Pages saved to cache."), "", *lines]),
                reply_markup=await self.dashboard_reply_markup(user_id),
            )
        except Exception as exc:
            logger.exception("Page discovery failed")
            await self.edit_message(
                chat_id,
                progress_message_id,
                f"Page discovery failed: {exc}",
                reply_markup=await self.dashboard_reply_markup(user_id),
            )

    async def discover_pages(self, account_id: str) -> List[Dict[str, str]]:
        from playwright.async_api import async_playwright

        cookie_string = await asyncio.to_thread(self.storage.get_account_cookie, account_id)
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
        pages = await asyncio.to_thread(self.storage.list_pages, args[0])
        if not pages:
            await self.send_message(
                chat_id,
                "No pages stored. Tap Refresh Pages first.",
                message_id,
                reply_markup=await self.dashboard_reply_markup(user_id),
            )
            return
        lines = [f"Stored pages for {args[0]}:"]
        for page in pages:
            lines.append(f"- {page['page_name'] or page['page_id']} | {page['page_url'] or page['page_id']}")
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
    ) -> str:
        if post_type not in POST_TYPES:
            raise ValueError(f"post_type must be one of {sorted(POST_TYPES)}")
        if not account_id:
            raise ValueError("account_id is required")
        if not page_id_or_url:
            raise ValueError("page_id_or_url is required")
        if post_type in {"image", "video"} and not media_path:
            raise ValueError(f"{post_type} media is required")

        job_id = await asyncio.to_thread(
            self.storage.create_post_job,
            telegram_chat_id=chat_id,
            telegram_user_id=user_id,
            account_id=account_id,
            page_id_or_url=page_id_or_url,
            page_name="",
            post_type=post_type,
            caption=caption,
            media_path=media_path,
        )
        await self.send_message(
            chat_id,
            f"Queued post job {job_id}.",
            reply_markup=await self.dashboard_reply_markup(user_id),
        )
        asyncio.create_task(self.run_post_job(job_id, chat_id, user_id, account_id, page_id_or_url, post_type, caption, media_path))
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

        job_payloads: List[Dict[str, str]] = []
        job_ids: List[str] = []
        for page in pages:
            page_id_or_url = str(page.get("page_url") or page.get("page_id") or "").strip()
            page_name = str(page.get("page_name") or page.get("page_id") or page_id_or_url).strip()
            if not page_id_or_url:
                continue
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
            job_ids.append(job_id)
            job_payloads.append(
                {
                    "job_id": job_id,
                    "page_id_or_url": page_id_or_url,
                    "page_name": page_name,
                    "post_type": post_type,
                    "caption": caption,
                    "media_path": media_path,
                }
            )

        if not job_ids:
            raise ValueError("No usable stored pages were found")
        await self.send_message(
            chat_id,
            f"Queued {len(job_ids)} post job(s) for all stored pages.",
            reply_markup=await self.dashboard_reply_markup(user_id),
        )
        asyncio.create_task(self.run_post_jobs_batch(chat_id, user_id, account_id, job_payloads))
        return job_ids

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

        await self.queue_post_job(
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
    ) -> None:
        lock_owner = f"telegram:{os.getpid()}:{job_id}:{uuid.uuid4().hex[:12]}"
        lock_acquired = False
        cookie_session_attempted = False
        heartbeat_task: Optional[asyncio.Task[None]] = None
        progress_message_id = 0
        try:
            progress = await self.send_message(
                chat_id,
                progress_card("Posting...", 0, 4, "Queued and waiting for account isolation slot."),
                reply_markup=await self.dashboard_reply_markup(user_id),
            )
            progress_message_id = int((progress.get("result") or {}).get("message_id") or 0)
            await asyncio.to_thread(self.storage.mark_job_started, job_id)
            lock_acquired = await self.wait_for_account_slot(account_id, lock_owner, chat_id)
            await self.edit_message(
                chat_id,
                progress_message_id,
                progress_card("Posting...", 1, 4, "Account slot acquired."),
                reply_markup=await self.dashboard_reply_markup(user_id),
            )
            heartbeat_task = asyncio.create_task(self.account_lock_heartbeat(account_id, lock_owner))
            cookie_string = await asyncio.to_thread(self.storage.get_account_cookie, account_id)
            await self.edit_message(
                chat_id,
                progress_message_id,
                progress_card("Posting...", 2, 4, "Cookie loaded. Opening Facebook composer..."),
                reply_markup=await self.dashboard_reply_markup(user_id),
            )
            post = self.engine_post_payload(page_id_or_url, page_id_or_url, post_type, caption, media_path)
            from playwright_engine import create_facebook_posts

            cookie_session_attempted = True
            results = await create_facebook_posts(cookies_json(parse_cookies(cookie_string)), [post], progress_callback=None)
            await self.edit_message(
                chat_id,
                progress_message_id,
                progress_card("Posting...", 3, 4, "Facebook returned a posting result."),
                reply_markup=await self.dashboard_reply_markup(user_id),
            )
            result = results[0] if results else {"success": False, "status": "no_result"}
            success = bool(result.get("success"))
            error = "" if success else str(result.get("result") or result.get("status") or result.get("error") or "posting failed")
            await asyncio.to_thread(self.storage.mark_job_completed, job_id, success, result, error)
            if success:
                await self.edit_message(
                    chat_id,
                    progress_message_id,
                    progress_card("Posting...", 4, 4, f"Post job {job_id} succeeded."),
                    reply_markup=await self.dashboard_reply_markup(user_id),
                )
            else:
                await self.edit_message(
                    chat_id,
                    progress_message_id,
                    progress_card("Posting...", 4, 4, f"Post job {job_id} failed: {error[:500]}"),
                    reply_markup=await self.dashboard_reply_markup(user_id),
                )
        except Exception as exc:
            logger.exception("Post job failed")
            await asyncio.to_thread(self.storage.mark_job_completed, job_id, False, {"exception": str(exc)}, str(exc))
            if progress_message_id:
                await self.edit_message(chat_id, progress_message_id, f"Post job {job_id} failed: {exc}", reply_markup=await self.dashboard_reply_markup(user_id))
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
        lock_acquired = False
        cookie_session_attempted = False
        heartbeat_task: Optional[asyncio.Task[None]] = None
        progress_message_id = 0
        job_ids = [str(job["job_id"]) for job in jobs]
        try:
            progress = await self.send_message(
                chat_id,
                progress_card("Batch posting...", 0, 4, f"Queued {len(jobs)} page job(s)."),
                reply_markup=await self.dashboard_reply_markup(user_id),
            )
            progress_message_id = int((progress.get("result") or {}).get("message_id") or 0)
            for job_id in job_ids:
                await asyncio.to_thread(self.storage.mark_job_started, job_id)
            lock_acquired = await self.wait_for_account_slot(account_id, lock_owner, chat_id)
            await self.edit_message(
                chat_id,
                progress_message_id,
                progress_card("Batch posting...", 1, 4, "Account slot acquired."),
                reply_markup=await self.dashboard_reply_markup(user_id),
            )
            heartbeat_task = asyncio.create_task(self.account_lock_heartbeat(account_id, lock_owner))
            cookie_string = await asyncio.to_thread(self.storage.get_account_cookie, account_id)
            await self.edit_message(
                chat_id,
                progress_message_id,
                progress_card("Batch posting...", 2, 4, "Cookie loaded. Posting cached pages..."),
                reply_markup=await self.dashboard_reply_markup(user_id),
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
            from playwright_engine import create_facebook_posts

            cookie_session_attempted = True
            results = await create_facebook_posts(cookies_json(parse_cookies(cookie_string)), posts, progress_callback=None)
            await self.edit_message(
                chat_id,
                progress_message_id,
                progress_card("Batch posting...", 3, 4, "Facebook returned batch results."),
                reply_markup=await self.dashboard_reply_markup(user_id),
            )
            success_count = 0
            for index, job in enumerate(jobs):
                result = results[index] if index < len(results) else {"success": False, "status": "no_result"}
                success = bool(result.get("success"))
                if success:
                    success_count += 1
                error = "" if success else str(result.get("result") or result.get("status") or result.get("error") or "posting failed")
                await asyncio.to_thread(self.storage.mark_job_completed, str(job["job_id"]), success, result, error)
            await self.edit_message(
                chat_id,
                progress_message_id,
                progress_card("Batch posting...", 4, 4, f"Completed: {success_count}/{len(jobs)} succeeded."),
                reply_markup=await self.dashboard_reply_markup(user_id),
            )
        except Exception as exc:
            logger.exception("Batch post job failed")
            for job_id in job_ids:
                await asyncio.to_thread(self.storage.mark_job_completed, job_id, False, {"exception": str(exc)}, str(exc))
            if progress_message_id:
                await self.edit_message(chat_id, progress_message_id, f"Batch posting failed: {exc}", reply_markup=await self.dashboard_reply_markup(user_id))
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

    async def wait_for_account_slot(self, account_id: str, owner: str, chat_id: int) -> bool:
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
        started = time.monotonic()
        notified_wait = False

        while True:
            runtime = await asyncio.to_thread(self.storage.claim_account_runtime, account_id, owner, lease_seconds)
            if runtime:
                remaining = int(max(0, cooldown_seconds - seconds_since(runtime.get("last_cookie_used_at"))))
                if remaining > 0:
                    await self.send_message(
                        chat_id,
                        f"Account {account_id} is cooling down for {remaining}s before this job starts.",
                    )
                    await asyncio.sleep(remaining)
                return True

            if time.monotonic() - started > max_wait_seconds:
                raise RuntimeError(f"Timed out waiting for account lock: {account_id}")
            if not notified_wait:
                await self.send_message(chat_id, f"Account {account_id} is busy; waiting for an isolated posting slot.")
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
        asyncio.create_task(self.handle_update(update))
        return web.json_response({"ok": True})


def create_app() -> web.Application:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
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
