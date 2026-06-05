#!/usr/bin/env python3
"""Live matrix posting test for Facebook pages.

Posts these cases to each discovered page:
  1. caption only
  2. image without caption
  3. image with caption
  4. video without caption
  5. video with caption

Pages are processed concurrently. Cases for the same page are processed
sequentially to avoid conflicting composer dialogs in the same account/page.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure repo modules are importable when the script is run directly.
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent))
sys.path.insert(0, str(REPO_ROOT))

from run_live_image_test import (  # noqa: E402
    ARTIFACT_DIR,
    COOKIE_STRING,
    IMAGE_PATH,
    attach_upload_response_tracker,
    click_exact_dialog_button,
    click_publish_flow,
    handle_post_submit_interstitial,
    parse_cookies,
    upload_image_without_stale_file_dialog,
    visible_dialog_error,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("live_matrix_test")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

CASE_ORDER = [
    "caption",
    "image_without_caption",
    "image_with_caption",
    "video_without_caption",
    "video_with_caption",
]


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning("Invalid %s=%r; using %d", name, raw, default)
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(0.1, float(raw))
    except ValueError:
        logger.warning("Invalid %s=%r; using %.1f", name, raw, default)
        return default


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    slug = slug.strip("._")
    return slug[:80] or "page"


def page_identity(page_info: Dict[str, str]) -> str:
    for key in ("id", "url", "name"):
        value = str(page_info.get(key) or "").strip()
        if not value:
            continue
        match = re.search(r"(?:id=|/profile\.php\?id=)(\d+)", value)
        if match:
            return match.group(1)
        if value.isdigit():
            return value
    return str(page_info.get("url") or page_info.get("name") or "").strip()


def _env_csv(name: str) -> set:
    return {
        item.strip()
        for item in os.getenv(name, "").split(",")
        if item.strip()
    }


def filter_pages(
    pages: List[Dict[str, str]],
    *,
    only_page_ids: Optional[List[str]] = None,
    skip_page_ids: Optional[List[str]] = None,
) -> List[Dict[str, str]]:
    only = set(only_page_ids or []) or _env_csv("LIVE_MATRIX_ONLY_PAGE_IDS")
    skip = set(skip_page_ids or []) or _env_csv("LIVE_MATRIX_SKIP_PAGE_IDS")
    if not only and not skip:
        return pages

    filtered = []
    for page in pages:
        identity = page_identity(page)
        name = str(page.get("name") or "")
        url = str(page.get("url") or "")
        haystack = {identity, name, url}
        if only and not (haystack & only):
            continue
        if skip and (haystack & skip):
            continue
        filtered.append(page)
    logger.info(
        "Page filters applied: before=%d after=%d only=%s skip=%s",
        len(pages),
        len(filtered),
        sorted(only),
        sorted(skip),
    )
    return filtered


def raw_cookie_string() -> str:
    return (
        os.getenv("FB_RAW_COOKIE_STRING", "").strip()
        or os.getenv("FB_COOKIE_STRING", "").strip()
        or COOKIE_STRING
    )


def account_key_from_cookie(cookie_string: str, account_id: str = "") -> str:
    if account_id:
        return safe_slug(account_id)
    for pair in cookie_string.split(";"):
        if pair.strip().startswith("c_user="):
            return safe_slug(pair.split("=", 1)[1].strip())
    digest = hashlib.sha256(cookie_string.encode("utf-8")).hexdigest()[:16]
    return f"cookie_{digest}"


def load_accounts() -> List[Dict[str, Any]]:
    """Load one or more account configs without logging cookie values."""
    raw_json = os.getenv("LIVE_MATRIX_ACCOUNTS_JSON", "").strip()
    accounts_file = os.getenv("LIVE_MATRIX_ACCOUNTS_FILE", "").strip()
    payload: Any = None

    if raw_json:
        payload = json.loads(raw_json)
    elif accounts_file:
        payload = json.loads(Path(accounts_file).expanduser().read_text(encoding="utf-8"))

    if payload is None:
        cookie = raw_cookie_string()
        return [
            {
                "account_id": account_key_from_cookie(cookie),
                "cookie": cookie,
                "only_page_ids": sorted(_env_csv("LIVE_MATRIX_ONLY_PAGE_IDS")),
                "skip_page_ids": sorted(_env_csv("LIVE_MATRIX_SKIP_PAGE_IDS")),
                "cases": [],
                "max_page_concurrency": _env_int("LIVE_MATRIX_MAX_CONCURRENCY", 2),
            }
        ]

    if isinstance(payload, dict):
        payload = payload.get("accounts", [])
    if not isinstance(payload, list):
        raise ValueError("LIVE_MATRIX_ACCOUNTS_JSON/FILE must be a list or {\"accounts\": [...]}")

    accounts: List[Dict[str, Any]] = []
    for index, item in enumerate(payload, 1):
        if not isinstance(item, dict):
            raise ValueError(f"Account entry #{index} must be an object")
        cookie = str(item.get("cookie") or item.get("cookie_string") or "").strip()
        if not cookie:
            raise ValueError(f"Account entry #{index} is missing cookie/cookie_string")
        account_id = str(item.get("account_id") or item.get("name") or "").strip()
        account_key = account_key_from_cookie(cookie, account_id)
        accounts.append(
            {
                "account_id": account_key,
                "display_name": str(item.get("display_name") or item.get("name") or account_key),
                "cookie": cookie,
                "only_page_ids": [str(v).strip() for v in item.get("only_page_ids", []) if str(v).strip()],
                "skip_page_ids": [str(v).strip() for v in item.get("skip_page_ids", []) if str(v).strip()],
                "cases": [str(v).strip() for v in item.get("cases", []) if str(v).strip()],
                "max_page_concurrency": int(item.get("max_page_concurrency") or _env_int("LIVE_MATRIX_MAX_CONCURRENCY", 2)),
            }
        )
    return accounts


def cooldown_store_path() -> Path:
    raw = os.getenv("LIVE_MATRIX_COOLDOWN_STORE", "").strip()
    if raw:
        return Path(raw).expanduser()
    return ARTIFACT_DIR / "matrix_cookie_cooldowns.json"


def read_cooldowns() -> Dict[str, Any]:
    path = cooldown_store_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_cooldowns(data: Dict[str, Any]) -> None:
    path = cooldown_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    temp_path.replace(path)


async def wait_for_cookie_cooldown(account_id: str, cooldown_seconds: int) -> Dict[str, Any]:
    if not _env_bool("LIVE_MATRIX_ENFORCE_COOKIE_COOLDOWN", True):
        return {"waited_seconds": 0, "remaining_seconds": 0, "enforced": False}
    data = read_cooldowns()
    entry = data.get(account_id) or {}
    last_finished = float(entry.get("last_finished_at") or 0)
    remaining = int(max(0, (last_finished + cooldown_seconds) - time.time()))
    if remaining > 0:
        logger.info(
            "Account %s cooling down for %ds before next browser session.",
            account_id,
            remaining,
        )
        await asyncio.sleep(remaining)
    return {
        "waited_seconds": remaining,
        "remaining_seconds": 0,
        "enforced": True,
        "cooldown_seconds": cooldown_seconds,
        "last_finished_at": last_finished,
    }


def mark_cookie_finished(account_id: str) -> None:
    data = read_cooldowns()
    data[account_id] = {
        "last_finished_at": time.time(),
        "last_finished_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    write_cooldowns(data)


def configure_engine_runtime_env() -> None:
    """Keep nested engine fallback inside the matrix account-level lock."""
    os.environ.setdefault("POST_COOKIE_MIN_INTERVAL_SECONDS", "0")
    os.environ.setdefault("POST_ENABLE_PAGES_PORTAL_FALLBACK", "true")
    os.environ.setdefault("POST_PAGES_PORTAL_FALLBACK_TEXT_ENABLED", "true")
    os.environ.setdefault("POST_PAGES_PORTAL_FALLBACK_IMAGE_ENABLED", "true")
    os.environ.setdefault("POST_PAGES_PORTAL_FALLBACK_VIDEO_ENABLED", "true")
    os.environ.setdefault("POST_PAGES_PORTAL_FIRST", "true")
    os.environ.setdefault("POST_PAGES_PORTAL_FIRST_TEXT_ENABLED", "true")
    os.environ.setdefault("POST_PAGES_PORTAL_FIRST_IMAGE_ENABLED", "true")
    os.environ.setdefault("POST_PAGES_PORTAL_FIRST_VIDEO_ENABLED", "true")


def cookies_json(cookies: List[Dict[str, str]]) -> str:
    return json.dumps(cookies, ensure_ascii=False)


def generate_video_artifact(timestamp: str, run_dir: Path, source_image_path: Optional[Path] = None) -> Path:
    """Generate a small Facebook-compatible MP4 from the configured image."""
    image_path = source_image_path or Path(IMAGE_PATH)
    video_path = run_dir / f"matrix_video_{timestamp}.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-loop",
            "1",
            "-i",
            str(image_path),
            "-t",
            "5",
            "-r",
            "30",
            "-vf",
            "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(video_path),
        ],
        check=True,
    )
    return video_path


async def extract_tokens(page) -> Dict[str, Any]:
    return await page.evaluate(
        """() => {
            const get = (name) => { try { return require(name); } catch (_) { return {}; } };
            const cookie = document.cookie || '';
            return {
                fb_dtsg: get('DTSGInitialData').token ||
                    get('DTSGInitData').token ||
                    document.querySelector('input[name="fb_dtsg"]')?.value || '',
                lsd: get('LSD').token || document.querySelector('input[name="lsd"]')?.value || '',
                user_id: get('CurrentUserInitialData').USER_ID || '',
                revision: String(get('SiteData').client_revision || ''),
                xs_present: /(?:^|;\\s*)xs=/.test(cookie),
                timestamp: Date.now() / 1000
            };
        }"""
    )


async def discover_pages_from_browser(browser, cookies: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Fallback page discovery that does not depend on repo-root playwright_engine.py."""
    context = await browser.new_context(
        viewport={"width": 1294, "height": 617},
        user_agent=USER_AGENT,
    )
    await context.add_cookies(cookies)
    page = await context.new_page()
    try:
        await page.goto("https://www.facebook.com/pages/?category=your_pages", wait_until="domcontentloaded", timeout=30000)
        await wait_for_pages_portal_ready(page, "", timeout_seconds=75)
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1.5)
            await page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(0.5)
        except Exception:
            pass
        raw_pages = await page.evaluate(
            """() => {
                const normalize = value => String(value || '')
                    .replace(/\\s+/g, ' ')
                    .trim();
                const visible = el => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden' &&
                        style.display !== 'none' &&
                        rect.width > 2 &&
                        rect.height > 2;
                };
                const pageIdFromUrl = href => {
                    const match = String(href || '').match(/[?&]id=(\\d{8,})/);
                    return match ? match[1] : '';
                };
                const cleanName = text => {
                    const lines = normalize(text).split(/\\s{2,}|\\n/).map(normalize).filter(Boolean);
                    for (const line of lines) {
                        if (!line) continue;
                        if (/create post|promote|notifications?|messages?|professional dashboard|pages you manage|your pages/i.test(line)) continue;
                        if (line.length <= 80) return line;
                    }
                    return '';
                };
                const results = [];
                const seen = new Set();
                const add = (id, href, name) => {
                    if (!id && href) id = pageIdFromUrl(href);
                    if (!id) return;
                    const url = `https://www.facebook.com/profile.php?id=${id}`;
                    if (seen.has(id)) return;
                    seen.add(id);
                    results.push({id, url, name: cleanName(name) || id});
                };

                for (const a of Array.from(document.querySelectorAll('a[href*="profile.php?id="]')).filter(visible)) {
                    const id = pageIdFromUrl(a.href);
                    const text = normalize(a.innerText || a.textContent || a.getAttribute('aria-label') || '');
                    add(id, a.href, text);
                }

                const controls = Array.from(document.querySelectorAll('button, [role="button"], a')).filter(visible);
                const createPostControls = controls.filter(el => /create post/i.test(
                    el.innerText || el.textContent || el.getAttribute('aria-label') || ''
                ));
                for (const control of createPostControls) {
                    let node = control;
                    for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
                        const text = normalize(node.innerText || node.textContent || '');
                        const links = Array.from(node.querySelectorAll('a[href*="profile.php?id="]'));
                        const link = links.find(item => pageIdFromUrl(item.href));
                        if (link) {
                            add(pageIdFromUrl(link.href), link.href, link.innerText || text);
                            break;
                        }
                    }
                }
                return results;
            }"""
        )
        discovered: List[Dict[str, str]] = []
        seen = set()
        for item in raw_pages or []:
            url = str(item.get("url") or "").strip()
            identity = page_identity(item)
            if not url or not identity or identity in seen:
                continue
            seen.add(identity)
            discovered.append(
                {
                    "id": str(item.get("id") or identity),
                    "url": url,
                    "name": str(item.get("name") or identity),
                }
            )
        if discovered:
            logger.info("Local browser discovery returned %d page(s).", len(discovered))
        return discovered
    except Exception as exc:
        logger.warning("Local browser page discovery failed: %s", exc)
        return []
    finally:
        await context.close()


async def discover_pages(cookies: List[Dict[str, str]], tokens: Dict[str, Any], browser=None) -> List[Dict[str, str]]:
    """Discover managed pages, preferring the stronger project discovery path."""
    discovered: List[Dict[str, str]] = []
    seen = set()

    original_sys_path = list(sys.path)
    removed_config = None
    try:
        existing_config = sys.modules.get("config")
        existing_config_path = str(getattr(existing_config, "__file__", "") or "")
        if existing_config_path.endswith("/fb_automation/config.py"):
            removed_config = existing_config
            del sys.modules["config"]
        sys.path = [
            str(REPO_ROOT),
            *[
                item
                for item in original_sys_path
                if item not in {str(REPO_ROOT), str(SCRIPT_DIR), str(SCRIPT_DIR.parent)}
            ],
        ]
        from playwright_engine import discover_facebook_pages

        ok, pages, detail = await discover_facebook_pages(cookies_json(cookies))
        if ok and pages:
            for item in pages:
                url = str(item.get("url") or item.get("id") or "").strip()
                name = str(item.get("name") or url or item.get("id") or "").strip()
                if not url or url in seen:
                    continue
                seen.add(url)
                discovered.append(
                    {
                        "id": str(item.get("id") or ""),
                        "url": url,
                        "name": name,
                    }
                )
            logger.info("Engine discovery returned %d page(s).", len(discovered))
        elif detail:
            logger.warning("Engine discovery did not return pages: %s", detail)
    except Exception as exc:
        logger.warning("Engine discovery failed: %s", exc)
    finally:
        sys.path = original_sys_path
        if removed_config is not None and "config" not in sys.modules:
            sys.modules["config"] = removed_config

    if discovered:
        return discovered

    if browser is not None:
        discovered = await discover_pages_from_browser(browser, cookies)
        if discovered:
            return discovered

    fallback_id = str(tokens.get("user_id") or "").strip()
    if fallback_id:
        return [
            {
                "id": fallback_id,
                "url": f"https://www.facebook.com/profile.php?id={fallback_id}",
                "name": f"Profile {fallback_id}",
            }
        ]
    return []


def import_engine_callable(name: str):
    """Import a callable from repo-root playwright_engine without path shadowing."""
    original_sys_path = list(sys.path)
    removed_config = None
    try:
        existing_config = sys.modules.get("config")
        existing_config_path = str(getattr(existing_config, "__file__", "") or "")
        if existing_config_path.endswith("/fb_automation/config.py"):
            removed_config = existing_config
            del sys.modules["config"]
        sys.path = [
            str(REPO_ROOT),
            *[
                item
                for item in original_sys_path
                if item not in {str(REPO_ROOT), str(SCRIPT_DIR), str(SCRIPT_DIR.parent)}
            ],
        ]
        import playwright_engine

        return getattr(playwright_engine, name)
    finally:
        sys.path = original_sys_path
        if removed_config is not None and "config" not in sys.modules:
            sys.modules["config"] = removed_config


async def publish_case_via_engine_fallback(
    page_info: Dict[str, str],
    case: Dict[str, str],
    timestamp: str,
    reason: str,
    cookie_string: str,
) -> Dict[str, Any]:
    page_name = page_info.get("name") or page_info.get("url") or "page"
    page_url = page_info.get("url") or page_info.get("id") or ""
    caption = case["caption_template"].format(timestamp=timestamp, page_name=page_name)
    post_type = case["post_type"] if case["post_type"] in {"image", "video"} else "post"
    post = {
        "page_id_or_url": page_url,
        "page_name": page_name,
        "caption": caption,
        "post_type": post_type,
        "media_url": case.get("media_path") or "",
    }
    create_facebook_posts = import_engine_callable("create_facebook_posts")
    engine_results = await create_facebook_posts(
        cookies_json(parse_cookies(cookie_string)),
        [post],
        progress_callback=None,
    )
    engine_result = engine_results[0] if engine_results else {}
    success = bool(engine_result.get("success"))
    return {
        "page": page_name,
        "page_url": page_url,
        "case": case["case"],
        "post_type": case["post_type"],
        "success": success,
        "error": "" if success else str(engine_result.get("result") or engine_result.get("status") or "Engine fallback failed"),
        "publish_flow": "engine_pages_portal_fallback",
        "prompt_actions": [],
        "upload_trace": [],
        "screenshots": {},
        "transport": "engine_pages_portal_fallback",
        "fallback_reason": reason,
        "engine_result": engine_result,
        "caption_present": bool(caption),
    }


async def set_existing_media_file_input(page, media_path: str, media_type: str) -> str:
    accept_marker = "video" if media_type == "video" else "image"
    selectors = [
        f"div[role='dialog'] input[type='file'][accept*='{accept_marker}']",
        "div[role='dialog'] input[type='file']",
        f"input[type='file'][accept*='{accept_marker}']",
        "input[type='file']",
    ]
    for selector in selectors:
        try:
            file_input = page.locator(selector).first
            if await file_input.count() <= 0:
                continue
            await file_input.set_input_files(media_path, timeout=10000)
            return selector
        except Exception:
            continue
    return ""


async def upload_media_without_stale_file_dialog(page, media_path: str, media_type: str) -> str:
    if media_type == "image":
        return await upload_image_without_stale_file_dialog(page, media_path)

    direct_selector = await set_existing_media_file_input(page, media_path, media_type)
    if direct_selector:
        return f"direct input: {direct_selector}"

    selectors = [
        "div[role='dialog'] div[aria-label='Photo/video']",
        "div[role='dialog'] div[aria-label='Photo/Video']",
        "div[role='dialog'] [aria-label='Photo/video']",
        "div[role='dialog'] div[role='button']:has-text('Video')",
        "div[role='dialog'] div[role='button']:has-text('Photo')",
        "div[role='dialog'] [aria-label*='ideo']",
        "div[role='dialog'] [aria-label*='Video']",
    ]
    for selector in selectors:
        button = page.locator(selector).first
        try:
            if await button.count() <= 0:
                continue
            try:
                async with page.expect_file_chooser(timeout=5000) as chooser_info:
                    await button.click(timeout=3000)
                chooser = await chooser_info.value
                await chooser.set_files(media_path)
                return f"file chooser: {selector}"
            except Exception:
                direct_selector = await set_existing_media_file_input(page, media_path, media_type)
                if direct_selector:
                    return f"post-click input: {direct_selector}"
        except Exception:
            continue
    return ""


async def composer_visible(page) -> bool:
    try:
        return bool(await page.evaluate(
            """() => {
                const visible = el => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden' &&
                        style.display !== 'none' &&
                        rect.width > 2 &&
                        rect.height > 2;
                };
                const dialogs = Array.from(document.querySelectorAll('[role="dialog"]'))
                    .filter(visible);
                for (const dialog of dialogs) {
                    const text = (dialog.innerText || dialog.textContent || '')
                        .replace(/\\s+/g, ' ')
                        .trim();
                    const lowerText = text.toLowerCase();
                    const hasEditor = Boolean(dialog.querySelector(
                        '[contenteditable="true"], textarea, [role="textbox"]'
                    ));
                    const hasComposerControls =
                        lowerText.includes("what's on your mind") ||
                        lowerText.includes("add to your post") ||
                        lowerText.includes("post preview") ||
                        lowerText.includes("post audience") ||
                        lowerText.includes("photo/video") ||
                        lowerText.includes("live video") ||
                        lowerText.includes("reel");
                    if ((hasEditor || hasComposerControls) &&
                        /create post|post preview|what's on your mind/i.test(text)) {
                        return true;
                    }
                }
                return false;
            }"""
        ))
    except Exception:
        pass
    return False


async def wait_for_composer_ready(page, timeout_seconds: Optional[float] = None) -> bool:
    timeout = timeout_seconds or _env_float("LIVE_MATRIX_COMPOSER_READY_TIMEOUT_SECONDS", 35.0)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await composer_visible(page):
            return True
        await asyncio.sleep(0.5)
    return False


async def reset_page_state(page) -> None:
    """Close leftover dialogs/menus before opening a new composer in the same context."""
    for _ in range(3):
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.4)
        except Exception:
            break


async def wait_for_pages_portal_ready(page, page_name: str, timeout_seconds: Optional[float] = None) -> bool:
    timeout = timeout_seconds or _env_float("LIVE_MATRIX_PAGES_PORTAL_READY_TIMEOUT_SECONDS", 60.0)
    target = page_name.strip().lower()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            ready = await page.evaluate(
                """(target) => {
                    const text = (document.body.innerText || document.body.textContent || '')
                        .replace(/\\s+/g, ' ')
                        .trim()
                        .toLowerCase();
                    if (!text) return false;
                    return text.includes('create post') ||
                        text.includes('your pages') ||
                        text.includes('pages you manage') ||
                        (target && text.includes(target));
                }""",
                target,
            )
            if ready:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.75)
    return False


async def click_pages_portal_create_post(page, page_name: str) -> str:
    """Click the Create Post control on the Pages directory card for page_name."""
    try:
        return await page.evaluate(
            """(pageName) => {
                const normalize = value => String(value || '')
                    .replace(/\\s+/g, ' ')
                    .trim()
                    .toLowerCase();
                const textOf = el => normalize(
                    el?.innerText || el?.getAttribute?.('aria-label') || el?.textContent || ''
                );
                const visible = el => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden' &&
                        style.display !== 'none' &&
                        rect.width > 2 &&
                        rect.height > 2;
                };
                const rectCenter = rect => ({
                    x: rect.left + rect.width / 2,
                    y: rect.top + rect.height / 2,
                });
                const target = normalize(pageName);
                const createTextMatches = text => (
                    text === 'create post' ||
                    text === 'create' ||
                    (text.includes('create post') && text.length < 80)
                );
                const controls = Array.from(document.querySelectorAll(
                    'button, [role="button"], a'
                )).filter(visible);
                const targetEls = Array.from(document.querySelectorAll(
                    'a, span, div, strong, h1, h2, h3, [role="heading"]'
                )).filter(el => {
                    if (!visible(el)) return false;
                    const text = textOf(el);
                    return target && (text === target || (text.includes(target) && text.length <= target.length + 80));
                });
                const targetRects = targetEls.map(el => el.getBoundingClientRect());
                let best = null;
                let bestScore = -999;
                let bestReason = '';
                for (const el of controls) {
                    const text = textOf(el);
                    if (!createTextMatches(text)) continue;
                    const rect = el.getBoundingClientRect();
                    let score = 0;
                    let reason = 'text';
                    let node = el;
                    for (let depth = 0; node && depth < 11; depth += 1, node = node.parentElement) {
                        const blockText = textOf(node);
                        if (!blockText) continue;
                        if (target && blockText.includes(target) && blockText.includes('create post')) {
                            const nextScore = 220 - depth * 10 - Math.floor(blockText.length / 120);
                            if (nextScore > score) {
                                score = nextScore;
                                reason = `card-depth-${depth}`;
                            }
                        } else if (target && blockText.includes(target)) {
                            const nextScore = 110 - depth * 8;
                            if (nextScore > score) {
                                score = nextScore;
                                reason = `ancestor-depth-${depth}`;
                            }
                        }
                        if (/professional dashboard|insights|planner|monetisation|monetization|ad centre|ad center|advertise|promote/i.test(blockText)) {
                            score -= 15;
                        }
                    }
                    for (const targetRect of targetRects) {
                        const verticalDistance = Math.abs(rect.top - targetRect.top);
                        const horizontalOverlap = rect.left < targetRect.right + 900 && rect.right > targetRect.left - 150;
                        if (horizontalOverlap && verticalDistance < 190) {
                            const targetCenter = rectCenter(targetRect);
                            const controlCenter = rectCenter(rect);
                            const distance = Math.hypot(controlCenter.x - targetCenter.x, controlCenter.y - targetCenter.y);
                            const nextScore = 190 - Math.floor(distance / 5);
                            if (nextScore > score) {
                                score = nextScore;
                                reason = 'geometry';
                            }
                        }
                    }
                    if (score > bestScore) {
                        best = el;
                        bestScore = score;
                        bestReason = reason;
                    }
                }
                if (!best || bestScore < 25) return '';
                best.scrollIntoView({block: 'center', inline: 'center'});
                const mouse = new MouseEvent('click', {bubbles: true, cancelable: true, view: window});
                best.dispatchEvent(mouse);
                best.click();
                return `${(best.innerText || best.getAttribute('aria-label') || 'Create Post').trim()} score=${bestScore} reason=${bestReason}`;
            }""",
            page_name,
        )
    except Exception:
        return ""


async def handle_profile_switch_modal(page, page_name: str) -> bool:
    """Complete Facebook's Page actor switch prompt if it appears."""
    switched = False
    for _ in range(4):
        if await composer_visible(page):
            return switched
        text = (await active_dialog_text(page)).lower()
        if "switch" not in text and page_name.lower() not in text:
            await asyncio.sleep(0.5)
            continue
        clicked = await click_exact_dialog_button(page, ["Switch", "Continue"], timeout=2500)
        if clicked:
            switched = True
            await asyncio.sleep(3)
            continue
        break
    return switched


async def open_pages_portal_composer(page, page_name: str) -> str:
    portal_urls = [
        "https://www.facebook.com/pages/?category=your_pages",
        "https://www.facebook.com/bookmarks/pages",
    ]
    for portal_url in portal_urls:
        try:
            await page.goto(portal_url, wait_until="domcontentloaded", timeout=30000)
            await wait_for_pages_portal_ready(page, page_name)
            detail = await click_pages_portal_create_post(page, page_name)
            if not detail:
                continue
            await wait_for_composer_ready(page, timeout_seconds=8)
            await handle_profile_switch_modal(page, page_name)
            if not await wait_for_composer_ready(page, timeout_seconds=8):
                retry_detail = await click_pages_portal_create_post(page, page_name)
                if retry_detail:
                    detail = f"{detail}; retry {retry_detail}"
                    await wait_for_composer_ready(page, timeout_seconds=8)
                    await handle_profile_switch_modal(page, page_name)
            if await wait_for_composer_ready(page):
                return f"pages portal: {detail}"
        except Exception as exc:
            logger.warning("Pages Portal opener failed for %s via %s: %s", page_name, portal_url, exc)
    return ""


async def open_composer(page, page_url: str, page_name: str) -> str:
    try:
        await page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
    except Exception as exc:
        logger.warning("Direct page navigation failed for %s; trying Pages Portal: %s", page_name, exc)
        portal_selector = await open_pages_portal_composer(page, page_name)
        if portal_selector:
            return portal_selector
        raise
    await asyncio.sleep(3)
    try:
        unavailable_text = await page.locator("text=\"This page isn't available\"").count()
        if unavailable_text:
            portal_selector = await open_pages_portal_composer(page, page_name)
            if portal_selector:
                return portal_selector
    except Exception:
        pass

    switch_selectors = [
        'text="Switch Now"',
        'text="Switch now"',
        'div[role="button"]:has-text("Switch")',
    ]
    for selector in switch_selectors:
        try:
            switch_btn = page.locator(selector).first
            if await switch_btn.count() > 0 and await switch_btn.is_visible(timeout=700):
                await switch_btn.click(timeout=3000)
                await asyncio.sleep(2)
                break
        except Exception:
            continue

    composer_selectors = [
        "div[role='button']:has-text(\"What's on your mind\")",
        f"div[role='button']:has-text(\"What's on your mind, {page_name}\")",
        "[aria-label='Create post']",
        "div[role='button']:has-text('Create post')",
        "div[role='button']:has-text('Write something')",
        "div[role='button']:has-text('Create Post')",
    ]

    for scroll_y in (0, 280, 560, 900, 1250):
        try:
            await page.evaluate("(y) => window.scrollTo(0, y)", scroll_y)
            await asyncio.sleep(0.8)
        except Exception:
            pass
        for selector in composer_selectors:
            try:
                composer = page.locator(selector).first
                if await composer.count() > 0 and await composer.is_visible(timeout=1000):
                    await composer.click(timeout=5000)
                    if await wait_for_composer_ready(page):
                        return f"timeline scroll={scroll_y}: {selector}"
                    await reset_page_state(page)
            except Exception:
                continue

    separator = "&" if "?" in page_url else "?"
    try:
        await page.goto(f"{page_url}{separator}sk=composer", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2)
        for selector in composer_selectors:
            try:
                composer = page.locator(selector).first
                if await composer.count() > 0 and await composer.is_visible(timeout=1000):
                    await composer.click(timeout=5000)
                    if await wait_for_composer_ready(page):
                        return f"direct composer route: {selector}"
                    await reset_page_state(page)
            except Exception:
                continue
    except Exception as exc:
        logger.warning("Direct composer route failed for %s; trying Pages Portal: %s", page_name, exc)
    portal_selector = await open_pages_portal_composer(page, page_name)
    if portal_selector:
        return portal_selector
    return ""


async def fill_caption(page, caption: str) -> bool:
    if not caption:
        return True

    selectors = [
        "div[role='dialog'] [contenteditable='true'][role='textbox']",
        "div[role='dialog'] [contenteditable='true']",
        "[contenteditable='true'][role='textbox']",
    ]
    deadline = time.monotonic() + _env_float("LIVE_MATRIX_TEXTBOX_TIMEOUT_SECONDS", 35.0)
    while time.monotonic() < deadline:
        for selector in selectors:
            try:
                textbox = page.locator(selector).first
                if await textbox.count() <= 0:
                    continue
                if not await textbox.is_visible(timeout=500):
                    continue
                await textbox.click(timeout=3000)
                await asyncio.sleep(0.25)
                await textbox.fill(caption)
                return True
            except Exception:
                continue
        try:
            filled = await page.evaluate(
                """(caption) => {
                    const visible = el => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.visibility !== 'hidden' &&
                            style.display !== 'none' &&
                            rect.width > 2 &&
                            rect.height > 2;
                    };
                    const dialogs = Array.from(document.querySelectorAll('[role="dialog"]'))
                        .filter(visible);
                    const dialog = dialogs.find(el => /create post|post preview/i.test(el.innerText || '')) ||
                        dialogs[dialogs.length - 1];
                    if (!dialog) return false;
                    const editors = Array.from(dialog.querySelectorAll(
                        '[contenteditable="true"], [role="textbox"], textarea'
                    )).filter(visible);
                    const editor = editors[0];
                    if (!editor) return false;
                    editor.focus();
                    if ('value' in editor) {
                        editor.value = caption;
                    } else {
                        editor.textContent = caption;
                    }
                    editor.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: caption}));
                    editor.dispatchEvent(new Event('change', {bubbles: true}));
                    return true;
                }""",
                caption,
            )
            if filled:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return False


async def active_dialog_text(page) -> str:
    try:
        return await page.evaluate(
            """() => {
                const visible = el => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden' &&
                        style.display !== 'none' &&
                        rect.width > 2 &&
                        rect.height > 2;
                };
                return Array.from(document.querySelectorAll('[role="dialog"]'))
                    .filter(visible)
                    .map(el => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim())
                    .join('\\n---\\n')
                    .slice(0, 800);
            }"""
        )
    except Exception:
        return ""


async def composer_or_preview_open(page) -> bool:
    text = (await active_dialog_text(page)).lower()
    if not text:
        return False
    return any(
        marker in text
        for marker in (
            "create post",
            "post preview",
            "publish original post",
            "hosting an event",
            "add a whatsapp",
            "make it easier",
            "boost post",
        )
    )


async def wait_after_publish(page, upload_tracker: Dict[str, Any], timeout_seconds: int) -> Dict[str, Any]:
    prompt_actions: List[str] = []
    deadline = time.monotonic() + timeout_seconds
    last_dialog_text = ""

    while time.monotonic() < deadline:
        action = await handle_post_submit_interstitial(page)
        if action:
            prompt_actions.append(action)
            logger.info("   ✅ Clicked post-submit prompt: %s", action)
            await asyncio.sleep(2)
            continue

        error_text = await visible_dialog_error(page)
        if not error_text and upload_tracker.get("last_error"):
            error_text = f"Upload endpoint error: {upload_tracker['last_error']}"
        if error_text:
            normalized_error = re.sub(r"\s+", " ", error_text).strip().lower()
            if normalized_error in {"posting", "posting posting"}:
                await asyncio.sleep(1)
                continue
            return {
                "success": False,
                "error": error_text,
                "prompt_actions": prompt_actions,
                "dialog_text": await active_dialog_text(page),
            }

        last_dialog_text = await active_dialog_text(page)
        if not await composer_or_preview_open(page):
            return {
                "success": True,
                "error": "",
                "prompt_actions": prompt_actions,
                "dialog_text": last_dialog_text,
            }

        lowered = last_dialog_text.lower()
        if "processing your video" in lowered or "video is processing" in lowered:
            return {
                "success": True,
                "error": "",
                "prompt_actions": prompt_actions,
                "dialog_text": last_dialog_text,
                "processing_video": True,
            }

        await asyncio.sleep(1)

    return {
        "success": False,
        "error": f"Dialog still open after {timeout_seconds}s",
        "prompt_actions": prompt_actions,
        "dialog_text": last_dialog_text,
    }


def build_cases(timestamp: str, image_path: Path, video_path: Path) -> List[Dict[str, str]]:
    cases = [
        {
            "case": "caption",
            "post_type": "text",
            "caption_template": "Live automation caption-only case {timestamp} for {page_name}. Please ignore.",
            "media_path": "",
        },
        {
            "case": "image_without_caption",
            "post_type": "image",
            "caption_template": "",
            "media_path": str(image_path),
        },
        {
            "case": "image_with_caption",
            "post_type": "image",
            "caption_template": "Live automation image-with-caption case {timestamp} for {page_name}. Please ignore.",
            "media_path": str(image_path),
        },
        {
            "case": "video_without_caption",
            "post_type": "video",
            "caption_template": "",
            "media_path": str(video_path),
        },
        {
            "case": "video_with_caption",
            "post_type": "video",
            "caption_template": "Live automation video-with-caption case {timestamp} for {page_name}. Please ignore.",
            "media_path": str(video_path),
        },
    ]
    requested = _env_csv("LIVE_MATRIX_CASES")
    if requested:
        cases = [case for case in cases if case["case"] in requested]
        logger.info("Case filter applied: %s", sorted(requested))
    return cases


async def publish_case(
    page,
    page_info: Dict[str, str],
    case: Dict[str, str],
    timestamp: str,
    run_dir: Path,
    account_cookie: str,
) -> Dict[str, Any]:
    page_name = page_info.get("name") or page_info.get("url") or "page"
    page_url = page_info.get("url") or page_info.get("id") or ""
    case_name = case["case"]
    slug = f"{safe_slug(page_name)}_{case_name}"
    upload_tracker, detach_upload_tracker = attach_upload_response_tracker(page, f"{page_name}:{case_name}")

    result: Dict[str, Any] = {
        "page": page_name,
        "page_url": page_url,
        "case": case_name,
        "post_type": case["post_type"],
        "success": False,
        "error": "",
        "publish_flow": "",
        "prompt_actions": [],
        "upload_trace": [],
        "screenshots": {},
    }

    try:
        logger.info("─" * 60)
        logger.info("Posting case=%s page=%s", case_name, page_name)
        await reset_page_state(page)
        composer_selector = await open_composer(page, page_url, page_name)
        if not composer_selector:
            logger.info("Composer not found for %s/%s; waiting and retrying once.", page_name, case_name)
            await asyncio.sleep(6)
            composer_selector = await open_composer(page, page_url, page_name)
        if not composer_selector:
            page_fail_path = run_dir / f"00_page_composer_not_found_{slug}_{timestamp}.png"
            try:
                await page.screenshot(path=str(page_fail_path))
                result["screenshots"]["composer_not_found"] = str(page_fail_path)
                result["page_text"] = (await active_dialog_text(page))[:500]
            except Exception:
                pass
            result["error"] = "Composer not found"
            if _env_bool("LIVE_MATRIX_ENGINE_FALLBACK_ENABLED", False):
                fallback = await publish_case_via_engine_fallback(
                    page_info,
                    case,
                    timestamp,
                    "direct composer not found",
                    account_cookie,
                )
                fallback["direct_attempt"] = result
                return fallback
            return result
        result["composer_selector"] = composer_selector

        before_path = run_dir / f"01_composer_{slug}_{timestamp}.png"
        await page.screenshot(path=str(before_path))
        result["screenshots"]["composer"] = str(before_path)

        media_path = case.get("media_path") or ""
        if media_path:
            upload_method = await upload_media_without_stale_file_dialog(page, media_path, case["post_type"])
            result["upload_method"] = upload_method
            if not upload_method:
                result["error"] = f"No {case['post_type']} file input or chooser found"
                return result
            media_timeout = 18 if case["post_type"] == "video" else 3
            await asyncio.sleep(media_timeout)
            if upload_tracker.get("last_error"):
                result["error"] = f"Upload endpoint error: {upload_tracker['last_error']}"
                return result

            media_path_ss = run_dir / f"02_media_attached_{slug}_{timestamp}.png"
            await page.screenshot(path=str(media_path_ss))
            result["screenshots"]["media_attached"] = str(media_path_ss)

        caption = case["caption_template"].format(timestamp=timestamp, page_name=page_name)
        result["caption_present"] = bool(caption)
        if caption:
            if not await fill_caption(page, caption):
                result["error"] = "Textbox not found for caption"
                return result
            await asyncio.sleep(0.5)

        ready_path = run_dir / f"03_ready_{slug}_{timestamp}.png"
        await page.screenshot(path=str(ready_path))
        result["screenshots"]["ready"] = str(ready_path)

        posted, publish_flow = await click_publish_flow(page)
        result["publish_flow"] = publish_flow
        if not posted:
            result["error"] = publish_flow
            return result

        if case["post_type"] == "video":
            wait_timeout = int(_env_float("LIVE_MATRIX_VIDEO_POST_TIMEOUT_SECONDS", 300.0))
        elif case["post_type"] == "image":
            wait_timeout = int(_env_float("LIVE_MATRIX_IMAGE_POST_TIMEOUT_SECONDS", 180.0))
        else:
            wait_timeout = int(_env_float("LIVE_MATRIX_TEXT_POST_TIMEOUT_SECONDS", 120.0))
        final_state = await wait_after_publish(page, upload_tracker, wait_timeout)
        result["prompt_actions"] = final_state.get("prompt_actions", [])
        if result["prompt_actions"]:
            result["publish_flow"] = f"{publish_flow} -> {' -> '.join(result['prompt_actions'])}"
        result["success"] = bool(final_state.get("success"))
        result["error"] = str(final_state.get("error") or "")
        if final_state.get("processing_video"):
            result["processing_video"] = True
        if not result["success"]:
            result["dialog_text"] = str(final_state.get("dialog_text") or "")[:500]

        after_path = run_dir / f"04_after_{slug}_{timestamp}.png"
        await page.screenshot(path=str(after_path))
        result["screenshots"]["after"] = str(after_path)

        if not result["success"]:
            try:
                await page.keyboard.press("Escape")
                await asyncio.sleep(1)
            except Exception:
                pass
        return result
    except Exception as exc:
        result["error"] = str(exc)[:300]
        try:
            error_path = run_dir / f"error_{slug}_{timestamp}.png"
            await page.screenshot(path=str(error_path))
            result["screenshots"]["error"] = str(error_path)
        except Exception:
            pass
        return result
    finally:
        result["upload_trace"] = list(upload_tracker.get("events") or [])
        try:
            await reset_page_state(page)
        except Exception:
            pass
        detach_upload_tracker()


async def run_page_worker(
    browser,
    cookies: List[Dict[str, str]],
    page_info: Dict[str, str],
    cases: List[Dict[str, str]],
    timestamp: str,
    run_dir: Path,
    account_cookie: str,
) -> List[Dict[str, Any]]:
    page_name = page_info.get("name") or page_info.get("url") or "page"
    context = await browser.new_context(
        viewport={"width": 1294, "height": 617},
        user_agent=USER_AGENT,
    )
    await context.add_cookies(cookies)
    page = await context.new_page()
    results: List[Dict[str, Any]] = []
    try:
        for case in cases:
            result = await publish_case(page, page_info, case, timestamp, run_dir, account_cookie)
            results.append(result)
            status = "SUCCESS" if result.get("success") else "FAILED"
            logger.info(
                "%s page=%s case=%s detail=%s",
                status,
                page_name,
                case["case"],
                result.get("error") or result.get("publish_flow") or "OK",
            )
            await asyncio.sleep(float(os.getenv("LIVE_MATRIX_CASE_DELAY_SECONDS", "2")))
    finally:
        await context.close()
    return results


async def run_account_matrix(pw, account: Dict[str, Any], timestamp: str, base_run_dir: Path) -> Dict[str, Any]:
    account_id = str(account["account_id"])
    account_label = str(account.get("display_name") or account_id)
    account_dir = base_run_dir / safe_slug(account_id)
    account_dir.mkdir(parents=True, exist_ok=True)

    cooldown_seconds = _env_int("LIVE_MATRIX_COOKIE_COOLDOWN_SECONDS", 360)
    cooldown = await wait_for_cookie_cooldown(account_id, cooldown_seconds)

    image_path = Path(str(account.get("image_path") or IMAGE_PATH)).expanduser()
    if not image_path.exists():
        raise FileNotFoundError(f"Image does not exist for account {account_id}: {image_path}")

    video_path = generate_video_artifact(timestamp, account_dir, image_path)
    account_cookie = str(account["cookie"])
    cookies = parse_cookies(account_cookie)
    max_concurrency = max(1, int(account.get("max_page_concurrency") or _env_int("LIVE_MATRIX_MAX_CONCURRENCY", 2)))

    logger.info("=" * 60)
    logger.info("ACCOUNT MATRIX START account=%s", account_label)
    logger.info("Image: %s", image_path)
    logger.info("Video: %s", video_path)
    logger.info("Cookies: %d entries", len(cookies))
    logger.info("Max page concurrency: %d", max_concurrency)
    logger.info("Cookie cooldown: %ds waited=%ds", cooldown_seconds, cooldown.get("waited_seconds", 0))
    logger.info("=" * 60)

    browser = await pw.chromium.launch(
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ],
    )

    tokens: Dict[str, Any] = {}
    pages: List[Dict[str, str]] = []
    results: List[Dict[str, Any]] = []
    selected_case_names = CASE_ORDER
    account_error = ""

    try:
        bootstrap_context = await browser.new_context(
            viewport={"width": 1294, "height": 617},
            user_agent=USER_AGENT,
        )
        await bootstrap_context.add_cookies(cookies)
        bootstrap_page = await bootstrap_context.new_page()
        await bootstrap_page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)
        home_ss = account_dir / f"00_home_{account_id}_{timestamp}.png"
        await bootstrap_page.screenshot(path=str(home_ss))
        tokens = await extract_tokens(bootstrap_page)
        logger.info(
            "Tokens account=%s fb_dtsg=%s lsd=%s user_id=%s xs=%s",
            account_label,
            "present" if tokens.get("fb_dtsg") else "missing",
            "present" if tokens.get("lsd") else "missing",
            tokens.get("user_id") or "missing",
            "present" if tokens.get("xs_present") else "missing",
        )
        await bootstrap_context.close()

        if not tokens.get("fb_dtsg"):
            account_error = "Could not extract fb_dtsg; cookies may be expired."
            raise RuntimeError(account_error)

        pages = await discover_pages(cookies, tokens, browser)
        pages = filter_pages(
            pages,
            only_page_ids=list(account.get("only_page_ids") or []),
            skip_page_ids=list(account.get("skip_page_ids") or []),
        )
        if not pages:
            account_error = "No pages discovered after filters."
            raise RuntimeError(account_error)

        logger.info("Account %s discovered %d selected page target(s):", account_label, len(pages))
        for page in pages:
            logger.info("  - %s (%s)", page.get("name"), page.get("url"))

        cases = build_cases(timestamp, image_path, video_path)
        account_cases = {str(value).strip() for value in account.get("cases", []) if str(value).strip()}
        if account_cases:
            cases = [case for case in cases if case["case"] in account_cases]
            logger.info("Account case filter applied account=%s cases=%s", account_label, sorted(account_cases))
        selected_case_names = [case["case"] for case in cases]
        if not cases:
            account_error = "No cases selected after filters."
            raise RuntimeError(account_error)
        semaphore = asyncio.Semaphore(max_concurrency)

        async def guarded_worker(page_info: Dict[str, str]) -> List[Dict[str, Any]]:
            async with semaphore:
                return await run_page_worker(
                    browser,
                    cookies,
                    page_info,
                    cases,
                    timestamp,
                    account_dir,
                    account_cookie,
                )

        nested_results = await asyncio.gather(*(guarded_worker(page_info) for page_info in pages))
        results = [item for group in nested_results for item in group]
    except Exception as exc:
        account_error = account_error or str(exc)
        logger.error("Account matrix failed account=%s error=%s", account_label, account_error)
    finally:
        try:
            await browser.close()
        finally:
            mark_cookie_finished(account_id)

    success_count = sum(1 for item in results if item.get("success"))
    result_payload = {
        "timestamp": timestamp,
        "account_id": account_id,
        "account_label": account_label,
        "image_path": str(image_path),
        "video_path": str(video_path),
        "pages": pages,
        "cases": selected_case_names,
        "success": bool(results) and success_count == len(results) and not account_error,
        "success_count": success_count,
        "failure_count": len(results) - success_count,
        "account_error": account_error,
        "cooldown": cooldown,
        "max_page_concurrency": max_concurrency,
        "results": results,
        "tokens_available": {
            "fb_dtsg": bool(tokens.get("fb_dtsg")),
            "lsd": bool(tokens.get("lsd")),
            "user_id": tokens.get("user_id", ""),
            "xs": bool(tokens.get("xs_present")),
        },
    }
    result_path = account_dir / f"matrix_results_{account_id}_{timestamp}.json"
    result_path.write_text(json.dumps(result_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    result_payload["result_path"] = str(result_path)

    logger.info("=" * 60)
    logger.info(
        "ACCOUNT MATRIX RESULTS account=%s total=%d success=%d failed=%d",
        account_label,
        len(results),
        success_count,
        len(results) - success_count,
    )
    for item in results:
        logger.info(
            "%s account=%s page=%s case=%s flow=%s error=%s",
            "✅" if item.get("success") else "❌",
            account_label,
            item.get("page"),
            item.get("case"),
            item.get("publish_flow") or "",
            item.get("error") or "OK",
        )
    logger.info("Account results saved: %s", result_path)
    logger.info("=" * 60)
    return result_payload


async def run_matrix() -> None:
    from playwright.async_api import async_playwright

    configure_engine_runtime_env()
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    base_run_dir = ARTIFACT_DIR / f"matrix_{timestamp}"
    base_run_dir.mkdir(parents=True, exist_ok=True)
    accounts = load_accounts()
    max_account_concurrency = min(
        len(accounts),
        _env_int("LIVE_MATRIX_MAX_ACCOUNT_CONCURRENCY", max(1, len(accounts))),
    )

    logger.info("=" * 60)
    logger.info("LIVE MATRIX SCHEDULER START accounts=%d account_concurrency=%d", len(accounts), max_account_concurrency)
    logger.info("Base artifact directory: %s", base_run_dir)
    logger.info("=" * 60)

    async with async_playwright() as pw:
        semaphore = asyncio.Semaphore(max_account_concurrency)

        async def guarded_account(account: Dict[str, Any]) -> Dict[str, Any]:
            async with semaphore:
                return await run_account_matrix(pw, account, timestamp, base_run_dir)

        account_results = await asyncio.gather(*(guarded_account(account) for account in accounts))

    total_results = [item for account in account_results for item in account.get("results", [])]
    success_count = sum(1 for item in total_results if item.get("success"))
    master_payload = {
        "timestamp": timestamp,
        "success": bool(total_results) and success_count == len(total_results),
        "account_count": len(accounts),
        "max_account_concurrency": max_account_concurrency,
        "success_count": success_count,
        "failure_count": len(total_results) - success_count,
        "total_posts": len(total_results),
        "accounts": account_results,
        "sources_reviewed": [
            "https://developers.facebook.com/docs/graph-api/overview/rate-limiting/",
            "https://www.facebook.com/terms/",
            "https://about.fb.com/news/2025/04/reducing-spammy-content-on-facebook/",
        ],
    }
    master_path = base_run_dir / f"matrix_master_results_{timestamp}.json"
    master_path.write_text(json.dumps(master_payload, indent=2, ensure_ascii=False), encoding="utf-8")

    logger.info("=" * 60)
    logger.info(
        "FINAL MATRIX SCHEDULER RESULTS total=%d success=%d failed=%d",
        len(total_results),
        success_count,
        len(total_results) - success_count,
    )
    logger.info("Master results saved: %s", master_path)
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_matrix())
