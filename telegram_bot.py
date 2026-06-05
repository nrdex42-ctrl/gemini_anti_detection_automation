#!/usr/bin/env python3
"""Telegram webhook service for Render deployment."""

from __future__ import annotations

import asyncio
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
from run_live_image_test import parse_cookies
from run_live_matrix_test import cookies_json, discover_pages_from_browser

logger = logging.getLogger("telegram_bot")

POST_TYPES = {"text", "image", "video"}
UPLOAD_DIR = Path(os.getenv("TELEGRAM_UPLOAD_DIR", "artifacts/telegram_uploads"))


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


def account_id_from_cookie(cookie_string: str) -> str:
    for pair in cookie_string.split(";"):
        if pair.strip().startswith("c_user="):
            return pair.split("=", 1)[1].strip()
    raise ValueError("Cookie string must include c_user")


def split_command(text: str) -> Tuple[str, List[str]]:
    parts = (text or "").strip().split()
    if not parts:
        return "", []
    command = parts[0].split("@", 1)[0].lower()
    return command, parts[1:]


def help_text() -> str:
    return (
        "Facebook automation bot commands:\n\n"
        "/add_account <account_id> <raw_cookie> - store/update an account cookie\n"
        "/accounts - list stored accounts\n"
        "/remove_account <account_id> - deactivate an account\n"
        "/pages <account_id> - discover and store managed pages\n"
        "/list_pages <account_id> - list stored pages\n"
        "/post <account_id> <page_id_or_url> <text|image|video> <caption> - queue a post\n\n"
        "For image/video posts, attach or reply to the media in Telegram and include the /post command caption."
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
        self.webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
        if not self.webhook_secret:
            raise RuntimeError("TELEGRAM_WEBHOOK_SECRET is required")
        self.admin_ids = _csv_ints("BOT_ADMIN_IDS")
        self.storage = BotStorage.from_env()
        self.session: Optional[ClientSession] = None
        self.api_base = f"https://api.telegram.org/bot{self.token}"

    async def startup(self, app: web.Application) -> None:
        self.session = ClientSession()
        if _env_bool("AUTO_INIT_DB", True):
            await asyncio.to_thread(self.storage.ensure_schema)
            logger.info("Supabase/Postgres schema ready")
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

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

    async def send_message(self, chat_id: int, text: str, reply_to_message_id: int = 0) -> None:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:3900],
            "disable_web_page_preview": True,
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        await self.telegram_api("sendMessage", payload)

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

        if command in {"", "/start", "/help"}:
            await self.send_message(chat_id, help_text(), message_id)
            return
        if command == "/add_account":
            await self.command_add_account(chat_id, user_id, message_id, args, message)
            return
        if command == "/accounts":
            await self.command_accounts(chat_id, message_id)
            return
        if command == "/remove_account":
            await self.command_remove_account(chat_id, message_id, args)
            return
        if command == "/pages":
            await self.command_discover_pages(chat_id, message_id, args)
            return
        if command == "/list_pages":
            await self.command_list_pages(chat_id, message_id, args)
            return
        if command == "/post":
            await self.command_post(chat_id, user_id, message_id, args, message)
            return
        await self.send_message(chat_id, "Unknown command.\n\n" + help_text(), message_id)

    async def command_add_account(
        self,
        chat_id: int,
        user_id: int,
        message_id: int,
        args: List[str],
        message: Dict[str, Any],
    ) -> None:
        if len(args) < 1:
            await self.send_message(chat_id, "Usage: /add_account <account_id> <raw_cookie>", message_id)
            return
        account_id = args[0]
        cookie_string = " ".join(args[1:]).strip()
        if not cookie_string and message.get("reply_to_message"):
            reply = message["reply_to_message"]
            cookie_string = str(reply.get("text") or reply.get("caption") or "").strip()
        if not cookie_string:
            await self.send_message(chat_id, "Missing cookie string. Reply to the cookie message or pass it after account_id.", message_id)
            return
        parsed_account_id = account_id_from_cookie(cookie_string)
        if account_id in {"auto", "-"}:
            account_id = parsed_account_id
        await asyncio.to_thread(self.storage.upsert_account, account_id, cookie_string, account_id, user_id)
        if _env_bool("DELETE_COOKIE_MESSAGES", True):
            await self.delete_message(chat_id, message_id)
        await self.send_message(chat_id, f"Stored account {account_id}.")

    async def command_accounts(self, chat_id: int, message_id: int) -> None:
        accounts = await asyncio.to_thread(self.storage.list_accounts)
        if not accounts:
            await self.send_message(chat_id, "No accounts stored.", message_id)
            return
        lines = ["Stored accounts:"]
        for item in accounts:
            status = "active" if item.get("active") else "inactive"
            lines.append(f"- {item['account_id']} ({status})")
        await self.send_message(chat_id, "\n".join(lines), message_id)

    async def command_remove_account(self, chat_id: int, message_id: int, args: List[str]) -> None:
        if len(args) != 1:
            await self.send_message(chat_id, "Usage: /remove_account <account_id>", message_id)
            return
        changed = await asyncio.to_thread(self.storage.deactivate_account, args[0])
        await self.send_message(chat_id, "Account deactivated." if changed else "Account not found.", message_id)

    async def command_discover_pages(self, chat_id: int, message_id: int, args: List[str]) -> None:
        if len(args) != 1:
            await self.send_message(chat_id, "Usage: /pages <account_id>", message_id)
            return
        account_id = args[0]
        await self.send_message(chat_id, f"Discovering pages for {account_id}...", message_id)
        try:
            pages = await self.discover_pages(account_id)
            if pages:
                await asyncio.to_thread(self.storage.upsert_pages, account_id, pages)
            if not pages:
                await self.send_message(chat_id, "No managed pages discovered.")
                return
            lines = [f"Discovered {len(pages)} page(s):"]
            for page in pages:
                lines.append(f"- {page.get('name') or page.get('id')} | {page.get('url')}")
            await self.send_message(chat_id, "\n".join(lines))
        except Exception as exc:
            logger.exception("Page discovery failed")
            await self.send_message(chat_id, f"Page discovery failed: {exc}")

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

    async def command_list_pages(self, chat_id: int, message_id: int, args: List[str]) -> None:
        if len(args) != 1:
            await self.send_message(chat_id, "Usage: /list_pages <account_id>", message_id)
            return
        pages = await asyncio.to_thread(self.storage.list_pages, args[0])
        if not pages:
            await self.send_message(chat_id, "No pages stored. Run /pages <account_id> first.", message_id)
            return
        lines = [f"Stored pages for {args[0]}:"]
        for page in pages:
            lines.append(f"- {page['page_name'] or page['page_id']} | {page['page_url'] or page['page_id']}")
        await self.send_message(chat_id, "\n".join(lines), message_id)

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
                "Usage: /post <account_id> <page_id_or_url> <text|image|video> <caption>",
                message_id,
            )
            return
        account_id, page_id_or_url, post_type = args[:3]
        post_type = post_type.lower()
        caption = " ".join(args[3:]).strip()
        if post_type not in POST_TYPES:
            await self.send_message(chat_id, f"post_type must be one of {sorted(POST_TYPES)}", message_id)
            return

        media_path = ""
        file_id = self.extract_media_file_id(message, post_type)
        if post_type in {"image", "video"}:
            if file_id:
                media_path = await self.download_file(file_id, account_id)
            else:
                await self.send_message(chat_id, f"Attach or reply to a {post_type} file for this post.", message_id)
                return

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
        await self.send_message(chat_id, f"Queued post job {job_id}.")
        asyncio.create_task(self.run_post_job(job_id, chat_id, account_id, page_id_or_url, post_type, caption, media_path))

    async def run_post_job(
        self,
        job_id: str,
        chat_id: int,
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
        try:
            await asyncio.to_thread(self.storage.mark_job_started, job_id)
            lock_acquired = await self.wait_for_account_slot(account_id, lock_owner, chat_id)
            heartbeat_task = asyncio.create_task(self.account_lock_heartbeat(account_id, lock_owner))
            cookie_string = await asyncio.to_thread(self.storage.get_account_cookie, account_id)
            post = {
                "page_id_or_url": page_id_or_url,
                "page_name": page_id_or_url,
                "caption": caption,
                "post_type": post_type if post_type in {"image", "video"} else "post",
                "media_url": media_path,
            }
            from playwright_engine import create_facebook_posts

            cookie_session_attempted = True
            results = await create_facebook_posts(cookies_json(parse_cookies(cookie_string)), [post], progress_callback=None)
            result = results[0] if results else {"success": False, "status": "no_result"}
            success = bool(result.get("success"))
            error = "" if success else str(result.get("result") or result.get("status") or result.get("error") or "posting failed")
            await asyncio.to_thread(self.storage.mark_job_completed, job_id, success, result, error)
            if success:
                await self.send_message(chat_id, f"Post job {job_id} succeeded.")
            else:
                await self.send_message(chat_id, f"Post job {job_id} failed: {error[:500]}")
        except Exception as exc:
            logger.exception("Post job failed")
            await asyncio.to_thread(self.storage.mark_job_completed, job_id, False, {"exception": str(exc)}, str(exc))
            await self.send_message(chat_id, f"Post job {job_id} failed: {exc}")
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
        if path_secret != self.webhook_secret or header_secret != self.webhook_secret:
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
    app.router.add_post("/telegram/webhook/{secret}", bot.webhook)
    return app


def main() -> None:
    app = create_app()
    port = int(os.getenv("PORT", "10000"))
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
