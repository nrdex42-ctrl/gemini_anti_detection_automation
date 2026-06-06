"""
Playwright Automation Engine for Facebook
Handles browser sessions, stealth, and posting via cookies.
"""

import os
from playwright_runtime import configure_playwright_browsers_path

# Ensure Playwright browser path is set early, before importing playwright modules
# This is critical for deployment environments where the path may not be automatically detected
configure_playwright_browsers_path()

import json
import logging
import asyncio
import re
import io
import struct
from json import JSONDecodeError
import subprocess
import sys
import hashlib
import time
import shutil
import threading
import socket
import tempfile
import importlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Coroutine, Dict, List, Optional, Tuple, Any, Set, cast
from urllib import request as urllib_request
from urllib.parse import parse_qs, urlencode, unquote, urlparse, urlunparse
from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from playwright_stealth import stealth_async
from config import ADMIN_USER_IDS, DIAGNOSTICS_DIR, HEADLESS, TELEGRAM_TOKEN
from session_manager import session_manager

# Python 3.8 does not ship asyncio.to_thread; provide a compatible fallback so
# the rest of the module can use the same thread-offload API everywhere.
if not hasattr(asyncio, 'to_thread'):
    async def _asyncio_to_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: func(*args, **kwargs))

    asyncio.to_thread = _asyncio_to_thread  # type: ignore[attr-defined]

try:
    import requests
except Exception:  # pragma: no cover - requests is expected but diagnostics must not depend on it.
    requests = None

try:
    from rq import Worker, get_current_job
    from rq.job import Job
except Exception:  # pragma: no cover - RQ is optional for direct/local runs
    Worker = None
    get_current_job = None
    Job = None

try:
    import redis
except Exception:  # pragma: no cover - redis is optional for direct local runs
    redis = None

try:
    import psycopg
except Exception:  # pragma: no cover - PostgreSQL is optional for local runs
    psycopg = None

logger = logging.getLogger(__name__)

ADMIN_FAILURE_DEBUG_ENABLED = os.getenv('ADMIN_FAILURE_DEBUG_ENABLED', 'true').lower() == 'true'
ADMIN_FAILURE_SCREENSHOTS_ENABLED = os.getenv('ADMIN_FAILURE_SCREENSHOTS_ENABLED', 'true').lower() == 'true'
ADMIN_FAILURE_WORKER_DIRECT_SCREENSHOTS_ENABLED = (
    os.getenv('ADMIN_FAILURE_WORKER_DIRECT_SCREENSHOTS_ENABLED', 'false').lower() == 'true'
)


def _env_int(name: str, default: Any, minimum: Optional[int] = None) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(str(raw_value).strip())
    except (TypeError, ValueError):
        logger.warning("Invalid integer env %s=%r; using %s", name, raw_value, default)
        value = int(default)
    return max(minimum, value) if minimum is not None else value


def _env_float(name: str, default: Any, minimum: Optional[float] = None) -> float:
    raw_value = os.getenv(name, str(default))
    try:
        value = float(str(raw_value).strip())
    except (TypeError, ValueError):
        logger.warning("Invalid float env %s=%r; using %s", name, raw_value, default)
        value = float(default)
    return max(minimum, value) if minimum is not None else value


ADMIN_DIAGNOSTIC_REDIS_TTL_SECONDS = _env_int('ADMIN_DIAGNOSTIC_REDIS_TTL_SECONDS', 86400, minimum=300)
MAX_MEDIA_BYTES = _env_int('MAX_MEDIA_BYTES', 50 * 1024 * 1024)
FACEBOOK_IMAGE_MAX_BYTES = _env_int('FACEBOOK_IMAGE_MAX_BYTES', 10 * 1024 * 1024, minimum=1024 * 1024)
FACEBOOK_IMAGE_REENCODE_MAX_PIXELS = _env_int('FACEBOOK_IMAGE_REENCODE_MAX_PIXELS', 8_000_000, minimum=1_000_000)
FACEBOOK_IMAGE_REENCODE_QUALITY = _env_int('FACEBOOK_IMAGE_REENCODE_QUALITY', 92, minimum=50)
FACEBOOK_IMAGE_PREFER_JPEG_UPLOAD = os.getenv('FACEBOOK_IMAGE_PREFER_JPEG_UPLOAD', 'true').lower() == 'true'
FACEBOOK_IMAGE_FORCE_SANITIZE_UPLOAD = os.getenv('FACEBOOK_IMAGE_FORCE_SANITIZE_UPLOAD', 'true').lower() == 'true'
FACEBOOK_IMAGE_RETRY_JPEG_ON_REJECTION = (
    os.getenv('FACEBOOK_IMAGE_RETRY_JPEG_ON_REJECTION', 'true').lower() == 'true'
)
MEDIA_DOWNLOAD_TIMEOUT = _env_int('MEDIA_DOWNLOAD_TIMEOUT', 120)
PAGE_DISCOVERY_TIMEOUT = _env_int('PAGE_DISCOVERY_TIMEOUT', 30)
PAGE_DISCOVERY_SCROLLS = _env_int('PAGE_DISCOVERY_SCROLLS', 2)
PAGE_DISCOVERY_RESOURCE_BLOCKING = os.getenv('PAGE_DISCOVERY_RESOURCE_BLOCKING', 'true').lower() == 'true'
PAGE_DISCOVERY_GRAPHQL_WAIT_SECONDS = _env_float('PAGE_DISCOVERY_GRAPHQL_WAIT_SECONDS', 1.2, minimum=0.2)
PAGE_DISCOVERY_WAIT_UNTIL = os.getenv('PAGE_DISCOVERY_WAIT_UNTIL', 'commit').strip() or 'commit'
FACEBOOK_POST_TIMEOUT_SECONDS = _env_int('FACEBOOK_POST_TIMEOUT_SECONDS', 180, minimum=60)
POST_BATCH_PAGE_TIMEOUT_SECONDS = _env_int('POST_BATCH_PAGE_TIMEOUT_SECONDS', 130, minimum=60)
POST_BATCH_PAGE_TIMEOUT_MAX_SECONDS = _env_int('POST_BATCH_PAGE_TIMEOUT_MAX_SECONDS', 0, minimum=0)
POST_DIRECT_COMPOSER_TIMEOUT_SECONDS = _env_int('POST_DIRECT_COMPOSER_TIMEOUT_SECONDS', 45, minimum=15)
POST_DESKTOP_COMPOSER_TIMEOUT_SECONDS = _env_int('POST_DESKTOP_COMPOSER_TIMEOUT_SECONDS', 65, minimum=30)
POST_PAGES_PORTAL_TEXT_TIMEOUT_SECONDS = _env_int('POST_PAGES_PORTAL_TEXT_TIMEOUT_SECONDS', 180, minimum=60)
POST_PAGES_PORTAL_IMAGE_TIMEOUT_SECONDS = _env_int('POST_PAGES_PORTAL_IMAGE_TIMEOUT_SECONDS', 210, minimum=60)
POST_PAGES_PORTAL_VIDEO_TIMEOUT_SECONDS = _env_int('POST_PAGES_PORTAL_VIDEO_TIMEOUT_SECONDS', 240, minimum=90)
POST_SUBMIT_ACTION_TIMEOUT_SECONDS = _env_int('POST_SUBMIT_ACTION_TIMEOUT_SECONDS', 22, minimum=8)
POST_COMPOSER_ROUTE_LIMIT = _env_int('POST_COMPOSER_ROUTE_LIMIT', 2, minimum=1)
POST_COMPOSER_ENTRY_MODE = os.getenv('POST_COMPOSER_ENTRY_MODE', 'target').strip().lower() or 'target'
POST_COMPOSER_BUTTON_TIMEOUT_MS = _env_int('POST_COMPOSER_BUTTON_TIMEOUT_MS', 3500, minimum=800)
POST_COMPOSER_DIALOG_TIMEOUT_MS = _env_int('POST_COMPOSER_DIALOG_TIMEOUT_MS', 4000, minimum=1000)
POST_NAVIGATION_FATAL_CHECK_TIMEOUT_MS = _env_int('POST_NAVIGATION_FATAL_CHECK_TIMEOUT_MS', 2000, minimum=500)
POST_PROFILE_SWITCH_SETTLE_GRACE_SECONDS = _env_float(
    'POST_PROFILE_SWITCH_SETTLE_GRACE_SECONDS',
    2.0,
    minimum=0.0,
)
REDIS_URL = os.getenv('REDIS_URL', '').strip()
DATABASE_URL = os.getenv('DATABASE_URL', '').strip()
POST_ACCOUNT_LOCK_TTL_SECONDS = _env_int('POST_ACCOUNT_LOCK_TTL_SECONDS', 1800)
POST_ACCOUNT_LOCK_WAIT_SECONDS = _env_int('POST_ACCOUNT_LOCK_WAIT_SECONDS', 900)
POST_BATCH_ACCOUNT_LOCK_WAIT_SECONDS = _env_int(
    'POST_BATCH_ACCOUNT_LOCK_WAIT_SECONDS',
    min(180, POST_ACCOUNT_LOCK_WAIT_SECONDS),
    minimum=1,
)
POST_BATCH_ACCOUNT_LOCK_TTL_SECONDS = _env_int('POST_BATCH_ACCOUNT_LOCK_TTL_SECONDS', 900, minimum=60)
POST_BREAK_STALE_REDIS_LOCKS = os.getenv('POST_BREAK_STALE_REDIS_LOCKS', 'true').lower() == 'true'
POST_LOCK_HEARTBEAT_INTERVAL_SECONDS = _env_int('POST_LOCK_HEARTBEAT_INTERVAL_SECONDS', 15, minimum=5)
POST_LOCK_STALE_HEARTBEAT_SECONDS = _env_int('POST_LOCK_STALE_HEARTBEAT_SECONDS', 90, minimum=30)
POST_LOCK_METADATA_TTL_SECONDS = _env_int('POST_LOCK_METADATA_TTL_SECONDS', 7200, minimum=300)
POST_CONCURRENCY = _env_int('POST_CONCURRENCY', 4, minimum=1)
POST_OPERATION_SLOT_KEY = os.getenv('POST_OPERATION_SLOT_KEY', 'fb-post-active-operations')
POST_OPERATION_SLOT_TTL_SECONDS = _env_int('POST_OPERATION_SLOT_TTL_SECONDS', 3600, minimum=60)
POST_OPERATION_SLOT_WAIT_SECONDS = _env_int('POST_OPERATION_SLOT_WAIT_SECONDS', 120, minimum=1)
POST_WORKER_USE_OPERATION_SLOT = os.getenv('POST_WORKER_USE_OPERATION_SLOT', 'false').lower() == 'true'
POST_PARALLEL_BATCH_ENABLED = os.getenv('POST_PARALLEL_BATCH_ENABLED', 'false').lower() == 'true'
POST_PAGES_PORTAL_FIRST = os.getenv('POST_PAGES_PORTAL_FIRST', 'false').lower() == 'true'
POST_PAGES_PORTAL_FIRST_TEXT_ENABLED = os.getenv('POST_PAGES_PORTAL_FIRST_TEXT_ENABLED', 'false').lower() == 'true'
POST_PAGES_PORTAL_FIRST_IMAGE_ENABLED = os.getenv('POST_PAGES_PORTAL_FIRST_IMAGE_ENABLED', 'false').lower() == 'true'
POST_PAGES_PORTAL_FIRST_VIDEO_ENABLED = os.getenv('POST_PAGES_PORTAL_FIRST_VIDEO_ENABLED', 'false').lower() == 'true'
POST_ENABLE_PAGES_PORTAL_FALLBACK = os.getenv('POST_ENABLE_PAGES_PORTAL_FALLBACK', 'false').lower() == 'true'
POST_PAGES_PORTAL_FALLBACK_TEXT_ENABLED = os.getenv('POST_PAGES_PORTAL_FALLBACK_TEXT_ENABLED', 'false').lower() == 'true'
POST_PAGES_PORTAL_FALLBACK_IMAGE_ENABLED = os.getenv('POST_PAGES_PORTAL_FALLBACK_IMAGE_ENABLED', 'false').lower() == 'true'
POST_PAGES_PORTAL_FALLBACK_VIDEO_ENABLED = os.getenv('POST_PAGES_PORTAL_FALLBACK_VIDEO_ENABLED', 'false').lower() == 'true'
POST_PREFER_DIRECT_POSTING = os.getenv('POST_PREFER_DIRECT_POSTING', 'true').lower() == 'true'
POST_SKIP_DIRECT_AFTER_MEDIA_PORTAL_TIMEOUT = (
    os.getenv('POST_SKIP_DIRECT_AFTER_MEDIA_PORTAL_TIMEOUT', 'true').lower() == 'true'
)
POST_MOBILE_FIRST_ENABLED = os.getenv('POST_MOBILE_FIRST_ENABLED', 'false').lower() == 'true'
POST_MOBILE_ONLY_ENABLED = os.getenv('POST_MOBILE_ONLY_ENABLED', 'false').lower() == 'true'
POST_MOBILE_FALLBACK_TEXT_ENABLED = os.getenv('POST_MOBILE_FALLBACK_TEXT_ENABLED', 'false').lower() == 'true'
POST_MOBILE_FALLBACK_IMAGE_ENABLED = os.getenv('POST_MOBILE_FALLBACK_IMAGE_ENABLED', 'false').lower() == 'true'
POST_MOBILE_FALLBACK_VIDEO_ENABLED = os.getenv('POST_MOBILE_FALLBACK_VIDEO_ENABLED', 'false').lower() == 'true'
POST_PAGE_RECOVERY_RETRY_ENABLED = os.getenv('POST_PAGE_RECOVERY_RETRY_ENABLED', 'true').lower() == 'true'
POST_PAGE_RECOVERY_RETRY_MAX = _env_int('POST_PAGE_RECOVERY_RETRY_MAX', 1, minimum=0)
POST_ACCEPT_COMPOSER_CLOSE_AS_SUCCESS = os.getenv('POST_ACCEPT_COMPOSER_CLOSE_AS_SUCCESS', 'true').lower() == 'true'
POST_ACCEPT_VIDEO_PUBLISH_CLICK_AS_SUCCESS = os.getenv('POST_ACCEPT_VIDEO_PUBLISH_CLICK_AS_SUCCESS', 'true').lower() == 'true'
POST_ACCEPT_PUBLISH_CLICK_NO_ERROR_AS_SUCCESS = (
    os.getenv('POST_ACCEPT_PUBLISH_CLICK_NO_ERROR_AS_SUCCESS', 'false').lower() == 'true'
)
POST_PUBLISH_NO_ERROR_GRACE_MS = _env_int('POST_PUBLISH_NO_ERROR_GRACE_MS', 3500, minimum=1000)
POST_INITIAL_UI_CONFIRMATION_ENABLED = os.getenv('POST_INITIAL_UI_CONFIRMATION_ENABLED', 'true').lower() == 'true'
POST_INITIAL_UI_CONFIRMATION_TIMEOUT_MS = _env_int(
    'POST_INITIAL_UI_CONFIRMATION_TIMEOUT_MS',
    3500,
    minimum=500,
)
POST_PUBLISH_IN_PROGRESS_TIMEOUT_MS = _env_int(
    'POST_PUBLISH_IN_PROGRESS_TIMEOUT_MS',
    45000,
    minimum=3000,
)
POST_NETWORK_CONFIRMATION_ENABLED = os.getenv('POST_NETWORK_CONFIRMATION_ENABLED', 'true').lower() == 'true'
POST_NETWORK_CONFIRMATION_TIMEOUT_MS = _env_int('POST_NETWORK_CONFIRMATION_TIMEOUT_MS', 10000, minimum=1500)
POST_TARGET_FEED_CONFIRMATION_ENABLED = os.getenv('POST_TARGET_FEED_CONFIRMATION_ENABLED', 'true').lower() == 'true'
POST_TARGET_FEED_CONFIRMATION_TIMEOUT_MS = _env_int('POST_TARGET_FEED_CONFIRMATION_TIMEOUT_MS', 10000, minimum=1500)


def _pages_portal_first_enabled(post_type: str, has_media: bool) -> bool:
    if not POST_PAGES_PORTAL_FIRST:
        return False
    normalized = str(post_type or 'post').strip().lower()
    if normalized == 'video':
        return POST_PAGES_PORTAL_FIRST_VIDEO_ENABLED
    if normalized == 'image':
        return POST_PAGES_PORTAL_FIRST_IMAGE_ENABLED
    if has_media:
        return POST_PAGES_PORTAL_FIRST_IMAGE_ENABLED
    return POST_PAGES_PORTAL_FIRST_TEXT_ENABLED


def _pages_portal_fallback_enabled(post_type: str, has_media: bool) -> bool:
    if not POST_ENABLE_PAGES_PORTAL_FALLBACK:
        return False
    normalized = str(post_type or 'post').strip().lower()
    if normalized == 'video':
        return POST_PAGES_PORTAL_FALLBACK_VIDEO_ENABLED
    if normalized == 'image':
        return POST_PAGES_PORTAL_FALLBACK_IMAGE_ENABLED
    if has_media:
        return POST_PAGES_PORTAL_FALLBACK_IMAGE_ENABLED
    return POST_PAGES_PORTAL_FALLBACK_TEXT_ENABLED


def _mobile_fallback_enabled(post_type: str, has_media: bool) -> bool:
    normalized = str(post_type or 'post').strip().lower()
    if normalized == 'video':
        return POST_MOBILE_FALLBACK_VIDEO_ENABLED
    if normalized == 'image':
        return POST_MOBILE_FALLBACK_IMAGE_ENABLED
    if has_media:
        return POST_MOBILE_FALLBACK_IMAGE_ENABLED
    return POST_MOBILE_FALLBACK_TEXT_ENABLED


POST_RESOURCE_BLOCKING_ENABLED = os.getenv('POST_RESOURCE_BLOCKING_ENABLED', 'true').lower() == 'true'
POST_COOKIE_SESSION_LOCK_ENABLED = os.getenv('POST_COOKIE_SESSION_LOCK_ENABLED', 'true').lower() == 'true'
POST_COOKIE_SESSION_LOCK_WAIT_SECONDS = max(
    1,
    _env_int('POST_COOKIE_SESSION_LOCK_WAIT_SECONDS', POST_ACCOUNT_LOCK_WAIT_SECONDS),
)
POST_DISCOVERY_COOKIE_LOCK_WAIT_SECONDS = _env_int('POST_DISCOVERY_COOKIE_LOCK_WAIT_SECONDS', 3, minimum=0)
POST_COOKIE_SESSION_TRACKING_ENABLED = os.getenv('POST_COOKIE_SESSION_TRACKING_ENABLED', 'true').lower() == 'true'
POST_COOKIE_SESSION_LOCK_TTL_SECONDS = max(
    60,
    _env_int('POST_COOKIE_SESSION_LOCK_TTL_SECONDS', max(POST_ACCOUNT_LOCK_TTL_SECONDS, 3600)),
)
POST_COOKIE_MIN_INTERVAL_SECONDS = _env_int('POST_COOKIE_MIN_INTERVAL_SECONDS', 600, minimum=0)
POST_COOKIE_SECURITY_COOLDOWN_SECONDS = _env_int('POST_COOKIE_SECURITY_COOLDOWN_SECONDS', 21600, minimum=0)
POST_ALLOW_PARALLEL_SAME_COOKIE = os.getenv('POST_ALLOW_PARALLEL_SAME_COOKIE', 'false').lower() == 'true'
POST_PARALLEL_SAME_COOKIE_MAX_CONTEXTS = _env_int('POST_PARALLEL_SAME_COOKIE_MAX_CONTEXTS', 2, minimum=1)
POST_PARALLEL_SAME_COOKIE_STAGGER_SECONDS = _env_float(
    'POST_PARALLEL_SAME_COOKIE_STAGGER_SECONDS',
    10,
    minimum=0.0,
)
POST_BATCH_MEDIA_PRESTAGE_ENABLED = os.getenv('POST_BATCH_MEDIA_PRESTAGE_ENABLED', 'true').lower() == 'true'
POST_BATCH_MEDIA_PRESTAGE_CONCURRENCY = _env_int('POST_BATCH_MEDIA_PRESTAGE_CONCURRENCY', 4, minimum=1)
POST_DIRECT_FILE_INPUT_UPLOAD_ENABLED = os.getenv('POST_DIRECT_FILE_INPUT_UPLOAD_ENABLED', 'false').lower() == 'true'
FACEBOOK_UPLOAD_PATH_FALLBACK_ON_REJECTION = (
    os.getenv('FACEBOOK_UPLOAD_PATH_FALLBACK_ON_REJECTION', 'true').lower() == 'true'
)
POST_RQ_PROGRESS_MIN_INTERVAL_SECONDS = _env_float('POST_RQ_PROGRESS_MIN_INTERVAL_SECONDS', 1.0, minimum=0.0)
FACEBOOK_BROWSER_VALIDATION_FALLBACK_ENABLED = os.getenv('FACEBOOK_BROWSER_VALIDATION_FALLBACK_ENABLED', 'false').lower() == 'true'
FACEBOOK_BROWSER_ACCOUNT_LOOKUP_FALLBACK_ENABLED = os.getenv('FACEBOOK_BROWSER_ACCOUNT_LOOKUP_FALLBACK_ENABLED', 'false').lower() == 'true'
FACEBOOK_BROWSER_LOCALE = os.getenv('FACEBOOK_BROWSER_LOCALE', 'en-US').strip() or 'en-US'
FACEBOOK_BROWSER_TIMEZONE = os.getenv('FACEBOOK_BROWSER_TIMEZONE', 'Africa/Cairo').strip() or 'Africa/Cairo'
FACEBOOK_BROWSER_ACCEPT_LANGUAGE = os.getenv('FACEBOOK_BROWSER_ACCEPT_LANGUAGE', 'en-US,en;q=0.9,ar;q=0.8').strip() or 'en-US,en;q=0.9,ar;q=0.8'
FACEBOOK_SKIP_SNAP_CHROMIUM = os.getenv('FACEBOOK_SKIP_SNAP_CHROMIUM', 'true').lower() == 'true'
FACEBOOK_UPLOAD_SAFE_BROWSER_ENABLED = os.getenv('FACEBOOK_UPLOAD_SAFE_BROWSER_ENABLED', 'true').lower() == 'true'
FACEBOOK_STEALTH_ASYNC_ENABLED = os.getenv('FACEBOOK_STEALTH_ASYNC_ENABLED', 'false').lower() == 'true'
FACEBOOK_BROWSER_NO_SANDBOX = os.getenv('FACEBOOK_BROWSER_NO_SANDBOX', 'false').lower() == 'true'
FACEBOOK_UPLOAD_CDP_FALLBACK_ENABLED = os.getenv('FACEBOOK_UPLOAD_CDP_FALLBACK_ENABLED', 'true').lower() == 'true'
FACEBOOK_UPLOAD_PATH_FIRST_ENABLED = os.getenv('FACEBOOK_UPLOAD_PATH_FIRST_ENABLED', 'true').lower() == 'true'
FACEBOOK_BROWSER_EXECUTABLE = os.getenv('FACEBOOK_BROWSER_EXECUTABLE', '').strip()
FACEBOOK_BROWSER_USER_AGENT = os.getenv(
    'FACEBOOK_BROWSER_USER_AGENT',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
).strip()
MEDIA_UPLOAD_READY_IMAGE_TIMEOUT = _env_int('MEDIA_UPLOAD_READY_IMAGE_TIMEOUT', 20, minimum=5)
MEDIA_UPLOAD_READY_VIDEO_TIMEOUT = _env_int('MEDIA_UPLOAD_READY_VIDEO_TIMEOUT', 90, minimum=10)
FACEBOOK_MEDIA_UPLOAD_MAX_ATTEMPTS = _env_int('FACEBOOK_MEDIA_UPLOAD_MAX_ATTEMPTS', 4, minimum=1)
POST_UPLOAD_EARLY_FAILURE_DETECTION_ENABLED = os.getenv('POST_UPLOAD_EARLY_FAILURE_DETECTION_ENABLED', 'true').lower() == 'true'
MAX_PARALLEL_PAGES = _env_int('MAX_PARALLEL_PAGES', 2, minimum=1)
_PLAYWRIGHT_BROWSER_INSTALL_ATTEMPTED = False
_REDIS_CLIENT: Optional[Any] = None
_COOKIE_SESSION_LOCKS: Dict[str, threading.Lock] = {}
_COOKIE_SESSION_LOCKS_GUARD = threading.Lock()
_PLAYWRIGHT_COOKIE_KEYS = {'name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure', 'sameSite'}
_BUTTON_FINDER_LAST_SUCCESSFUL_STRATEGY: Optional[str] = None
_ProgressCoroutineCallback = Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]
PUBLISH_SENT_UNCONFIRMED_MARKER = 'publish_sent_unconfirmed'
_SAMESITE_ALIASES = {
    'strict': 'Strict',
    'lax': 'Lax',
    'none': 'None',
    'no_restriction': 'None',
    'unspecified': '',
}

_PAGE_DISCOVERY_URLS = (
    'https://www.facebook.com/pages/?category=your_pages',
    'https://www.facebook.com/bookmarks/pages',
)

_RESERVED_FACEBOOK_PATHS = {
    'about',
    'ads',
    'ajax',
    'bookmarks',
    'business',
    'campaign',
    'checkpoint',
    'events',
    'friends',
    'gaming',
    'groups',
    'help',
    'home',
    'login',
    'marketplace',
    'me',
    'messenger',
    'messages',
    'notifications',
    'pages',
    'people',
    'photo',
    'policies',
    'privacy',
    'profile.php',
    'reel',
    'search',
    'settings',
    'stories',
    'watch',
}

_BLOCKED_DISCOVERY_TEXTS = {
    'meta business suite',
    'business suite',
    'professional dashboard',
    'dashboard',
    'promote',
    'boost',
    'advertise',
    'all',
    'add page url',
    'create new page',
    'create page',
    'pages you may like',
    'home',
    'notifications',
    'watch',
    'marketplace',
    'message',
    'messages',
    'messenger',
    'inbox',
    'send message',
    'about',
    'posts',
    'mentions',
    'reviews',
    'followers',
    'photos',
    'videos',
    'reels',
    'more',
    'ترويج',
    'روّج',
    'إعلان',
    'اعلان',
    'إنشاء إعلان',
    'انشاء اعلان',
    'إدارة المنشورات',
    'الإجراءات لهذا المنشور',
    'إجراءات هذا المنشور',
    'إنشاء منشور',
    'انشاء منشور',
    'create post',
}

_BLOCKED_PAGE_SUBPATHS = {
    'about',
    'ads',
    'community',
    'dashboard',
    'events',
    'followers',
    'friends',
    'groups',
    'likes',
    'message',
    'messages',
    'messenger',
    'inbox',
    'mentions',
    'photos',
    'posts',
    'professional_dashboard',
    'promote',
    'reviews',
    'reels',
    'settings',
    'videos',
}

_AD_FLOW_URL_PARTS = (
    '/ad_center/',
    '/ads/',
    'adsmanager',
    'boostpost',
    'promote',
)

_LOGGED_OUT_URL_PARTS = (
    '/login/',
    '/login.php',
    '/reg/',
    'checkpoint',
)

_MOBILE_FACEBOOK_USER_AGENT = (
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
    'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 '
    'Mobile/15E148 Safari/604.1'
)


def _safe_diagnostic_name(value: str) -> str:
    cleaned = re.sub(r'[^a-zA-Z0-9_.-]+', '_', value.strip())[:80]
    return cleaned or 'facebook'


def _diagnostic_screenshot_redis_key(path_or_name: Any) -> str:
    return f'fb-diagnostic-screenshot:{Path(str(path_or_name)).name}'


def _store_diagnostic_screenshot_in_redis(folder: Path) -> None:
    if not ADMIN_FAILURE_DEBUG_ENABLED or not ADMIN_FAILURE_SCREENSHOTS_ENABLED:
        return
    screenshot_path = folder / 'screenshot.png'
    if not screenshot_path.exists() or not screenshot_path.is_file():
        return
    client = _redis_client()
    if client is None:
        return
    try:
        client.setex(
            _diagnostic_screenshot_redis_key(folder),
            ADMIN_DIAGNOSTIC_REDIS_TTL_SECONDS,
            screenshot_path.read_bytes(),
        )
        logger.info(f'Diagnostic screenshot cached in Redis: {folder.name}')
    except Exception as exc:
        logger.warning(f'Failed to cache diagnostic screenshot in Redis: {exc}')


def _diagnostic_caption(label: str, folder: Path, meta: Dict[str, Any]) -> str:
    lines = [
        'Playwright failure screenshot',
        f'Label: {str(label)[:100]}',
        f'Diagnostic: {str(folder)[:180]}',
    ]
    title = str(meta.get('title') or '').strip()
    url = str(meta.get('url') or '').strip()
    if title:
        lines.append(f'Title: {title[:160]}')
    if url:
        lines.append(f'URL: {url[:180]}')
    return '\n'.join(lines)[:950]


def _send_admin_diagnostic_sync(folder: Path, label: str, meta: Dict[str, Any]) -> None:
    if (
        not ADMIN_FAILURE_DEBUG_ENABLED
        or not ADMIN_FAILURE_SCREENSHOTS_ENABLED
        or not ADMIN_FAILURE_WORKER_DIRECT_SCREENSHOTS_ENABLED
        or not ADMIN_USER_IDS
        or not TELEGRAM_TOKEN
        or requests is None
    ):
        return
    screenshot_path = folder / 'screenshot.png'
    if not screenshot_path.exists() or not screenshot_path.is_file():
        return
    caption = _diagnostic_caption(label, folder, meta)
    endpoint = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto'
    for admin_id in sorted(ADMIN_USER_IDS):
        try:
            with screenshot_path.open('rb') as photo:
                response = requests.post(
                    endpoint,
                    data={
                        'chat_id': str(admin_id),
                        'caption': caption,
                    },
                    files={'photo': ('screenshot.png', photo, 'image/png')},
                    timeout=10,
                )
            if response.status_code >= 400:
                logger.warning(
                    'Telegram admin diagnostic send failed: admin=%s status=%s detail=%s',
                    admin_id,
                    response.status_code,
                    response.text[:200],
                )
        except Exception as exc:
            logger.warning(f'Failed to send Playwright diagnostic screenshot to admin {admin_id}: {exc}')


async def _notify_admin_diagnostic_saved(folder: Path, label: str, meta: Dict[str, Any]) -> None:
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_send_admin_diagnostic_sync, folder, label, meta),
            timeout=15,
        )
    except Exception as exc:
        logger.warning(f'Failed to notify admins about diagnostic screenshot: {exc}')


async def _save_diagnostics(page: Page, label: str) -> str:
    """Save browser diagnostics without logging cookies or request headers."""
    try:
        timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')
        folder = Path(DIAGNOSTICS_DIR) / f'{timestamp}_{_safe_diagnostic_name(label)}'
        folder.mkdir(parents=True, exist_ok=True)
        screenshot_path = folder / 'screenshot.png'
        html_path = folder / 'page.html'
        meta_path = folder / 'meta.json'
        controls_path = folder / 'controls.json'

        try:
            await page.screenshot(
                path=str(screenshot_path),
                full_page=False,
                animations='disabled',
                timeout=7000,
            )
        except Exception as screenshot_exc:
            logger.warning(f'Failed to save Playwright screenshot: {screenshot_exc}')
        html_path.write_text(await page.content(), encoding='utf-8')
        try:
            controls = await page.locator(
                "div[role='button'], button, a[role='button'], input[type='submit'], div[aria-label]"
            ).evaluate_all(
                """
                elements => elements.slice(0, 250).map(element => {
                    const rect = element.getBoundingClientRect();
                    return {
                        text: (
                            element.innerText ||
                            element.getAttribute('aria-label') ||
                            element.getAttribute('value') ||
                            element.textContent ||
                            ''
                        ).trim().slice(0, 160),
                        ariaLabel: element.getAttribute('aria-label') || '',
                        role: element.getAttribute('role') || element.tagName.toLowerCase(),
                        disabled: Boolean(
                            element.disabled ||
                            element.getAttribute('aria-disabled') === 'true' ||
                            element.closest('[aria-disabled="true"]') ||
                            element.closest('[disabled]')
                        ),
                        visible: rect.width > 0 && rect.height > 0,
                    };
                }).filter(item => item.text || item.ariaLabel)
                """
            )
            controls_path.write_text(json.dumps(controls, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception as controls_exc:
            logger.warning(f'Failed to save Playwright controls diagnostics: {controls_exc}')
        meta = {
            'label': label,
            'url': page.url,
            'title': await page.title(),
            'created_at': datetime.utcnow().isoformat(),
        }
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        _store_diagnostic_screenshot_in_redis(folder)
        await _notify_admin_diagnostic_saved(folder, label, meta)
        return str(folder)
    except Exception as exc:
        logger.warning(f'Failed to save Playwright diagnostics: {exc}')
        return ''


async def _smart_wait(
    condition_func: Callable[[], Any],
    timeout_ms: int = 5000,
    check_interval_ms: int = 200,
) -> bool:
    """Poll a condition with early return instead of using fixed sleeps."""
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
    while asyncio.get_running_loop().time() < deadline:
        try:
            value = condition_func()
            if asyncio.iscoroutine(value):
                value = await value
            if value:
                return True
        except Exception:
            pass
        await asyncio.sleep(check_interval_ms / 1000)
    return False


async def _wait_for_element_state(locator: Any, state: str = 'visible', timeout_ms: int = 3000) -> bool:
    """Return quickly when an element reaches a Playwright state."""
    try:
        await locator.wait_for(state=state, timeout=timeout_ms)
        return True
    except Exception:
        return False


async def _wait_for_locator_hidden(locator: Any, timeout_ms: int = 800) -> bool:
    """Return quickly when a locator is hidden or detached."""
    try:
        await locator.wait_for(state='hidden', timeout=timeout_ms)
        return True
    except Exception:
        return False


async def _wait_for_facebook_ui_ready(page: Page, timeout: int = 3500) -> bool:
    """Wait for the main Facebook shell instead of always sleeping a fixed delay."""
    try:
        await page.wait_for_load_state('domcontentloaded', timeout=timeout)
    except Exception:
        pass
    return await _smart_wait(
        lambda: page.locator('div[role="main"], div[role="dialog"], h1').count(),
        timeout_ms=timeout,
        check_interval_ms=200,
    )


def _attach_network_monitoring(page: Page, label: str) -> None:
    async def _on_response(response: Any) -> None:
        try:
            status = int(response.status)
            if status >= 400:
                logger.warning(
                    f'NETWORK stage="response_error" label="{label}" status={status} '
                    f'url={_safe_log_url(str(response.url or ""))}'
                )
        except Exception:
            pass

    async def _on_request_failed(request: Any) -> None:
        try:
            if request.resource_type in {'image', 'font', 'media'}:
                return
            lowered_url = str(request.url or '').lower()
            if any(marker in lowered_url for marker in ('fbcdn.net', 'emoji.php', 'hads-ak')):
                return
            logger.warning(
                f'NETWORK stage="request_failed" label="{label}" '
                f'url={_safe_log_url(str(request.url or ""))}'
            )
        except Exception:
            pass

    page.on('response', lambda response: asyncio.create_task(_on_response(response)))
    page.on('requestfailed', lambda request: asyncio.create_task(_on_request_failed(request)))


def _is_media_upload_url(url: str) -> bool:
    lowered = (url or '').lower()
    return any(marker in lowered for marker in ('upload', 'video', 'photo', 'media'))


def _is_blocking_upload_failure_url(url: str) -> bool:
    lowered = (url or '').lower()
    return any(marker in lowered for marker in ('upload', 'rupload', 'composer/attachments', 'media/upload'))


def _is_completed_upload_success_url(url: str) -> bool:
    lowered = (url or '').lower()
    return any(marker in lowered for marker in ('/requests/receive', 'rupload', 'composer/attachments', 'media/upload'))


def _upload_endpoint_kind(url: str) -> str:
    lowered = (url or '').lower()
    if 'rupload.facebook.com' in lowered:
        return 'rupload'
    if 'upload.facebook.com' in lowered:
        return 'upload_facebook'
    if 'composer/attachments' in lowered:
        return 'composer_attachment'
    if 'media/upload' in lowered:
        return 'media_upload'
    if 'upload' in lowered:
        return 'upload'
    return ''


def _sanitize_upload_error_detail(detail: str, limit: int = 260) -> str:
    text = re.sub(r'\s+', ' ', str(detail or '')).strip()
    if not text:
        return ''
    return text[:limit].rstrip()


def _extract_upload_error_detail(payload: Any) -> str:
    """Extract a concise, safe Facebook upload error from JSON-like payloads."""
    direct_keys = (
        'error',
        'error_summary',
        'errorSummary',
        'error_msg',
        'errorMsg',
        'error_description',
        'errorDescription',
        'error_user_title',
        'errorUserTitle',
        'error_user_msg',
        'errorUserMsg',
        'message',
        'summary',
        'description',
    )
    numeric_error_keys = {'error', 'error_code', 'errorCode', 'code', 'status'}

    def _walk(value: Any, depth: int = 0) -> List[str]:
        if depth > 5:
            return []
        if isinstance(value, dict):
            parts: List[str] = []
            for key in direct_keys:
                candidate = value.get(key)
                if isinstance(candidate, str):
                    cleaned = _sanitize_upload_error_detail(candidate)
                    if cleaned:
                        parts.append(cleaned)
            for key in numeric_error_keys:
                candidate = value.get(key)
                if isinstance(candidate, (int, float)) and candidate:
                    parts.append(f'{key}={candidate}')
            for key in ('error', 'errors', 'debug_info', 'data'):
                if key in value:
                    parts.extend(_walk(value.get(key), depth + 1))
            return parts
        if isinstance(value, list):
            parts = []
            for item in value[:5]:
                parts.extend(_walk(item, depth + 1))
            return parts
        if isinstance(value, str):
            cleaned = _sanitize_upload_error_detail(value)
            return [cleaned] if cleaned else []
        return []

    seen: Set[str] = set()
    details: List[str] = []
    for detail in _walk(payload):
        if detail and detail not in seen:
            seen.add(detail)
            details.append(detail)
        if len(details) >= 3:
            break
    return '; '.join(details)


async def _read_upload_response_error_detail(response: Any) -> str:
    try:
        body_text = await asyncio.wait_for(response.text(), timeout=2.0)
    except Exception:
        return ''
    body_text = _strip_facebook_json_prefix(body_text)
    if not body_text:
        return ''
    truncated = body_text[:20000]
    try:
        payload = _parse_graphql_payload(truncated)
        detail = _extract_upload_error_detail(payload)
        if detail:
            return detail
    except Exception:
        pass
    if re.search(r'error|failed|unsupported|invalid|too\s+large|too\s+long|تعذر|فشل|خطأ', truncated, re.I):
        return _sanitize_upload_error_detail(truncated)
    return ''


def _collect_payload_field_names(payload: Any, pattern: Any, limit: int = 16) -> List[str]:
    names: List[str] = []
    seen: Set[str] = set()

    def _walk(value: Any, depth: int = 0) -> None:
        if depth > 6 or len(names) >= limit:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                key_text = str(key)
                if pattern.search(key_text) and key_text not in seen:
                    seen.add(key_text)
                    names.append(key_text)
                    if len(names) >= limit:
                        return
                _walk(child, depth + 1)
                if len(names) >= limit:
                    return
        elif isinstance(value, list):
            for child in value[:8]:
                _walk(child, depth + 1)
                if len(names) >= limit:
                    return

    _walk(payload)
    return names


def _upload_response_payload_summary(payload: Any, body_length: int) -> Dict[str, Any]:
    summary: Dict[str, Any] = {'body_length': body_length}
    if isinstance(payload, dict):
        summary['top_keys'] = [str(key) for key in list(payload.keys())[:12]]
    elif isinstance(payload, list):
        summary['top_type'] = 'list'
        summary['items'] = len(payload)
    else:
        summary['top_type'] = type(payload).__name__

    error_detail = _extract_upload_error_detail(payload)
    if error_detail:
        summary['error'] = _sanitize_upload_error_detail(error_detail, limit=180)

    id_field_pattern = re.compile(r'(fbid|media|photo|video|attachment|upload|file).*id|id$', re.I)
    id_fields = _collect_payload_field_names(payload, id_field_pattern)
    if id_fields:
        summary['id_fields'] = id_fields

    return summary


async def _read_upload_response_success_summary(response: Any) -> Dict[str, Any]:
    try:
        body_text = await asyncio.wait_for(response.text(), timeout=2.0)
    except Exception:
        return {}
    body_text = _strip_facebook_json_prefix(body_text)
    if not body_text:
        return {}
    truncated = body_text[:24000]
    payload = None
    try:
        payload = _parse_graphql_payload(truncated)
    except Exception:
        payload = None
    if payload is not None:
        return _upload_response_payload_summary(payload, len(body_text))
    summary: Dict[str, Any] = {'body_length': len(body_text), 'top_type': 'text'}
    if re.search(r'error|failed|unsupported|invalid|too\s+large|too\s+long|تعذر|فشل|خطأ', truncated, re.I):
        summary['error'] = _sanitize_upload_error_detail(truncated, limit=180)
    return summary


def _upload_tracker_failure_detail(state: Optional[Dict[str, Any]]) -> str:
    if not state or int(state.get('failed') or 0) <= 0:
        return ''
    return str(state.get('last_error') or 'Facebook returned an upload error.').strip()


_UPLOAD_TRACKER_EVENT_LIMIT = 25


def _append_upload_tracker_event(state: Dict[str, Any], event: Dict[str, Any]) -> None:
    events = state.setdefault('events', [])
    if not isinstance(events, list):
        events = []
        state['events'] = events
    if len(events) >= _UPLOAD_TRACKER_EVENT_LIMIT:
        state['events_truncated'] = int(state.get('events_truncated') or 0) + 1
        return
    events.append(event)


def _upload_tracker_snapshot(state: Optional[Dict[str, Any]], max_events: int = 12) -> Dict[str, Any]:
    if not state:
        return {}
    events = state.get('events') or []
    if not isinstance(events, list):
        events = []
    recent_events = events[-max(1, max_events):]
    omitted = max(0, len(events) - len(recent_events)) + int(state.get('events_truncated') or 0)
    snapshot: Dict[str, Any] = {
        'seen': int(state.get('seen') or 0),
        'success': int(state.get('success') or 0),
        'completed_success': int(state.get('completed_success') or 0),
        'failed': int(state.get('failed') or 0),
        'last_status': int(state.get('last_status') or 0),
        'last_error': _sanitize_upload_error_detail(str(state.get('last_error') or ''), limit=180),
        'last_success_url': str(state.get('last_success_url') or ''),
        'events': recent_events,
    }
    if omitted:
        snapshot['events_omitted'] = omitted
    return snapshot


def _upload_tracker_summary(state: Optional[Dict[str, Any]], max_events: int = 12, limit: int = 1800) -> str:
    snapshot = _upload_tracker_snapshot(state, max_events=max_events)
    if not snapshot:
        return ''
    try:
        text = json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'))
    except Exception:
        text = str(snapshot)
    return _sanitize_upload_error_detail(text, limit=limit)


_UPLOAD_TRACE_HEADER_KEYS = {
    'accept',
    'content-length',
    'content-type',
    'origin',
    'referer',
    'sec-fetch-dest',
    'sec-fetch-mode',
    'sec-fetch-site',
    'x-asbd-id',
    'x-entity-length',
    'x-entity-name',
    'x-entity-type',
    'x-fb-fb-dtsg',
    'x-fb-friendly-name',
    'x-fb-lsd',
    'x-fb-upload-filesize',
    'x-fb-upload-offset',
    'x-fb-upload-retry-count',
    'x-start-offset',
}


async def _upload_request_trace_metadata(request: Any) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    try:
        headers: Dict[str, Any] = {}
        all_headers = getattr(request, 'all_headers', None)
        if callable(all_headers):
            maybe_headers = all_headers()
            headers = await maybe_headers if asyncio.iscoroutine(maybe_headers) else maybe_headers
        if not headers:
            headers = getattr(request, 'headers', {}) or {}
        normalized = {str(key).lower(): str(value) for key, value in dict(headers).items()}
        selected: Dict[str, str] = {}
        for key in sorted(_UPLOAD_TRACE_HEADER_KEYS):
            value = normalized.get(key)
            if value is None:
                continue
            if key in {'x-fb-fb-dtsg', 'x-fb-lsd'}:
                selected[key] = '<present>' if value else '<empty>'
            elif key == 'referer':
                selected[key] = _safe_log_url(value)
            elif key == 'content-type':
                selected[key] = re.sub(r'boundary=[^;]+', 'boundary=<present>', value, flags=re.I)[:160]
            elif key == 'x-entity-name':
                selected[key] = Path(value).name[:120]
            else:
                selected[key] = value[:160]
        if selected:
            metadata['headers'] = selected
    except Exception:
        pass
    try:
        post_data = getattr(request, 'post_data', None)
        if callable(post_data):
            post_data = post_data()
        if asyncio.iscoroutine(post_data):
            post_data = await post_data
        if isinstance(post_data, str) and post_data:
            post_summary: Dict[str, Any] = {'length': len(post_data)}
            field_names: List[str] = []
            seen_fields: Set[str] = set()
            for field_name in re.findall(r'name="([^"]+)"', post_data):
                if field_name in seen_fields:
                    continue
                seen_fields.add(field_name)
                field_names.append(field_name[:80])
                if len(field_names) >= 40:
                    break
            if field_names:
                post_summary['field_names'] = field_names
            filenames = re.findall(r'filename="([^"]*)"', post_data)
            if filenames:
                post_summary['file_count'] = len(filenames)
                post_summary['file_exts'] = sorted({
                    Path(filename).suffix.lower()[:12] or '<none>'
                    for filename in filenames
                })
            metadata['post'] = post_summary
    except Exception:
        pass
    return metadata


def _upload_attempt_trace_summary(events: List[Dict[str, Any]], max_events: int = 18, limit: int = 2400) -> str:
    if not events:
        return ''
    recent_events = events[-max(1, max_events):]
    payload: Dict[str, Any] = {'events': recent_events}
    omitted = max(0, len(events) - len(recent_events))
    if omitted:
        payload['events_omitted'] = omitted
    try:
        text = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    except Exception:
        text = str(payload)
    return _sanitize_upload_error_detail(text, limit=limit)


_MEDIA_UPLOAD_VISIBLE_ERROR_RE = re.compile(
    r"Can't Read Files|couldn'?t be uploaded|file(?:s)? can'?t be uploaded|"
    r'Photos should be less than|Videos should be less than|unsupported file|'
    r'تعذر تحميل|لا يمكن تحميل|ملف غير مدعوم',
    re.I,
)


def _is_media_upload_rejection(detail: str) -> bool:
    text = str(detail or '')
    return bool(_MEDIA_UPLOAD_VISIBLE_ERROR_RE.search(text)) or bool(
        re.search(
            r"your file can'?t be uploaded|facebook rejected the selected|"
            r"photos couldn'?t be uploaded|videos couldn'?t be uploaded|"
            r"file can'?t be uploaded|unsupported file",
            text,
            re.I,
        )
    )


def _is_media_upload_readiness_timeout(detail: str) -> bool:
    return bool(re.search(r'media upload was not ready after|upload could not be verified', str(detail or ''), re.I))


async def _visible_media_upload_error_detail(page: Page) -> str:
    try:
        dialogs = await page.locator("div[role='dialog']").all()
    except Exception:
        dialogs = []
    for dialog in reversed(dialogs):
        text = await _locator_compact_text(dialog, timeout=700)
        if text and _MEDIA_UPLOAD_VISIBLE_ERROR_RE.search(text):
            return _sanitize_upload_error_detail(text)
    return ''


async def _dismiss_media_upload_error_dialog(page: Page) -> bool:
    dismissed = False
    try:
        dialogs = await page.locator("div[role='dialog']").all()
    except Exception:
        dialogs = []
    for dialog in reversed(dialogs):
        try:
            text = await _locator_compact_text(dialog, timeout=500)
        except Exception:
            text = ''
        if not text or not _MEDIA_UPLOAD_VISIBLE_ERROR_RE.search(text):
            continue
        for candidate in (
            lambda: dialog.get_by_role('button', name=re.compile(r'^(OK|Close|Done|موافق|حسنًا|إغلاق)$', re.I)).last,
            lambda: dialog.locator("[aria-label='Close'], [aria-label='إغلاق']").last,
            lambda: dialog.locator("div[role='button']:has-text('OK')").last,
            lambda: dialog.locator("div[role='button']:has-text('Close')").last,
        ):
            try:
                button = candidate()
                if await button.is_visible(timeout=500):
                    await button.click(timeout=1500)
                    dismissed = True
                    break
            except Exception:
                continue
        if dismissed:
            await asyncio.sleep(0.3)
            break
    return dismissed


async def _clear_failed_media_upload_state(page: Page) -> None:
    await _dismiss_media_upload_error_dialog(page)
    try:
        dialog = await _find_composer_context(page)
    except Exception:
        dialog = page.locator("body").first
    for candidate in (
        lambda: dialog.get_by_role('button', name=re.compile(r'Remove post attachment|Remove photo|Remove video|إزالة', re.I)).first,
        lambda: dialog.locator("[aria-label*='Remove'][role='button']").first,
        lambda: dialog.locator("div[role='button']:has-text('Remove post attachment')").first,
    ):
        try:
            button = candidate()
            if await button.is_visible(timeout=500):
                await button.click(timeout=1500)
                await asyncio.sleep(0.3)
                return
        except Exception:
            continue


async def _enable_fast_discovery_mode(context: BrowserContext, page: Page) -> None:
    if not PAGE_DISCOVERY_RESOURCE_BLOCKING:
        return

    blocked_url_markers = (
        '/ads/',
        '/ad_center/',
        '/ajax/bz',
        '/audience_network/',
        '/marketplace/',
        '/reel/',
        '/reels/',
        '/watch/',
        'doubleclick',
        'googlesyndication',
        'google-analytics',
    )
    blocked_resource_types = {'font', 'image', 'media'}

    async def _route_handler(route: Any) -> None:
        try:
            request = route.request
            lowered_url = str(request.url or '').lower()
            if request.resource_type in blocked_resource_types or any(marker in lowered_url for marker in blocked_url_markers):
                await route.abort()
                return
            await route.continue_()
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass

    try:
        await context.route('**/*', _route_handler)
        await page.add_init_script(
            """
            (() => {
                const style = document.createElement('style');
                style.textContent = '*{animation-duration:0s!important;transition-duration:0s!important;scroll-behavior:auto!important}';
                document.documentElement.appendChild(style);
            })();
            """
        )
        logger.info('PAGE_DISCOVERY stage="fast_mode_enabled" block_resources=true')
    except Exception as exc:
        logger.debug(f'Could not enable fast page-discovery mode: {exc}')


async def _enable_fast_posting_mode(context: BrowserContext, page: Page) -> None:
    if not POST_RESOURCE_BLOCKING_ENABLED:
        return
    if bool(getattr(cast(Any, context), '_fb_post_fast_mode_enabled', False)):
        return

    blocked_url_markers = (
        '/ads/',
        '/ad_center/',
        '/audience_network/',
        '/marketplace/',
        '/reel/',
        '/reels/',
        '/watch/',
        'doubleclick',
        'googlesyndication',
        'google-analytics',
        'googleadservices',
        'facebook.com/tr',
    )
    blocked_resource_types = {'font'}

    async def _route_handler(route: Any) -> None:
        try:
            request = route.request
            lowered_url = str(request.url or '').lower()
            if request.resource_type in blocked_resource_types or any(marker in lowered_url for marker in blocked_url_markers):
                await route.abort()
                return
            await route.continue_()
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass

    try:
        await context.route('**/*', _route_handler)
        setattr(cast(Any, context), '_fb_post_fast_mode_enabled', True)
        await page.add_init_script(
            """
            (() => {
                const style = document.createElement('style');
                style.textContent = '*{animation-duration:0s!important;transition-duration:0s!important;scroll-behavior:auto!important}';
                document.documentElement.appendChild(style);
            })();
            """
        )
        logger.info('POST_FAST_MODE stage="enabled" blocked_resource_types=font')
    except Exception as exc:
        logger.debug(f'Could not enable fast posting mode: {exc}')


def _attach_upload_response_tracker(page: Page, label: str) -> Tuple[Dict[str, Any], Callable[[], None]]:
    state: Dict[str, Any] = {
        'seen': 0,
        'success': 0,
        'completed_success': 0,
        'failed': 0,
        'last_status': 0,
        'last_error': '',
        'last_success_url': '',
        'logical_error': False,
        'events': [],
        'events_truncated': 0,
    }

    async def _on_response(response: Any) -> None:
        try:
            url = str(response.url or '')
            if not _is_media_upload_url(url):
                return
            status = int(response.status)
            state['seen'] = int(state.get('seen') or 0) + 1
            event: Dict[str, Any] = {
                'status': status,
                'url': _safe_log_url(url),
                'blocking': _is_blocking_upload_failure_url(url),
                'completed': _is_completed_upload_success_url(url),
            }
            endpoint_kind = _upload_endpoint_kind(url)
            if endpoint_kind:
                event['endpoint'] = endpoint_kind
            try:
                request = response.request
                event['method'] = str(getattr(request, 'method', '') or '')
                event['resource'] = str(getattr(request, 'resource_type', '') or '')
                request_metadata = await _upload_request_trace_metadata(request)
                if request_metadata:
                    event['request'] = request_metadata
            except Exception:
                pass
            if status >= 400:
                if _is_blocking_upload_failure_url(url):
                    error_detail = await _read_upload_response_error_detail(response)
                    if error_detail:
                        event['error'] = _sanitize_upload_error_detail(error_detail, limit=180)
                    state['failed'] = int(state.get('failed') or 0) + 1
                    state['last_status'] = status
                    state['last_error'] = (
                        f'{status} {_safe_log_url(url)}: {error_detail}'
                        if error_detail
                        else f'{status} {_safe_log_url(url)}'
                    )
                logger.warning(
                    f'UPLOAD_NETWORK label="{label}" status={status} '
                    f'url={_safe_log_url(url)} error="{_upload_tracker_failure_detail(state)}"'
                )
            elif 200 <= status < 300:
                state['success'] = int(state.get('success') or 0) + 1
                if _is_completed_upload_success_url(url):
                    state['completed_success'] = int(state.get('completed_success') or 0) + 1
                    state['last_success_url'] = _safe_log_url(url)
                    body_summary = await _read_upload_response_success_summary(response)
                    if body_summary:
                        event['body'] = body_summary
                        body_error = str(body_summary.get('error') or '').strip()
                        if body_error:
                            state['failed'] = int(state.get('failed') or 0) + 1
                            state['last_status'] = status
                            state['last_error'] = f'{status} {_safe_log_url(url)}: {body_error}'
                            state['logical_error'] = True
                logger.info(
                    f'UPLOAD_NETWORK label="{label}" status={status} '
                    f'url={_safe_log_url(url)}'
                )
            _append_upload_tracker_event(state, event)
        except Exception:
            pass

    def _listener(response: Any) -> None:
        asyncio.create_task(_on_response(response))

    page.on('response', _listener)

    def _detach() -> None:
        try:
            page.remove_listener('response', _listener)
        except Exception:
            pass

    return state, _detach


def _strip_facebook_json_prefix(text: str) -> str:
    cleaned = (text or '').strip()
    if cleaned.startswith('for (;;);'):
        cleaned = cleaned[len('for (;;);'):].lstrip()
    return cleaned


def _parse_graphql_payload(text: str) -> Any:
    cleaned = _strip_facebook_json_prefix(text)
    if not cleaned:
        raise ValueError('empty GraphQL response body')
    try:
        return json.loads(cleaned)
    except JSONDecodeError:
        # Some Facebook endpoints return newline-delimited envelopes. Parse the
        # first JSON-looking line rather than treating a valid publish as opaque.
        for line in cleaned.splitlines():
            candidate = _strip_facebook_json_prefix(line)
            if not candidate or not candidate.startswith(('{', '[')):
                continue
            return json.loads(candidate)
        raise


def _extract_first_graphql_error(payload: Any) -> str:
    if isinstance(payload, dict):
        errors = payload.get('errors')
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                message = str(first.get('message') or first.get('summary') or '').strip()
                code = str(first.get('code') or '').strip()
                if message and code:
                    return f'{message} (code {code})'
                return message or str(first)[:240]
            return str(first)[:240]
        for value in payload.values():
            nested_error = _extract_first_graphql_error(value)
            if nested_error:
                return nested_error
    elif isinstance(payload, list):
        for item in payload:
            nested_error = _extract_first_graphql_error(item)
            if nested_error:
                return nested_error
    return ''


def _extract_graphql_post_id(payload: Any) -> str:
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized_key = str(key).lower()
            if normalized_key in {'post_id', 'postid'} and value:
                return str(value)
            nested_post_id = _extract_graphql_post_id(value)
            if nested_post_id:
                return nested_post_id
    elif isinstance(payload, list):
        for item in payload:
            nested_post_id = _extract_graphql_post_id(item)
            if nested_post_id:
                return nested_post_id
    return ''


async def _graphql_friendly_name_from_response(response: Any) -> str:
    try:
        request_obj = response.request
    except Exception:
        request_obj = None
    if request_obj is None:
        return ''

    headers: Dict[str, str] = {}
    try:
        raw_headers = getattr(request_obj, 'headers', {}) or {}
        headers.update({str(k).lower(): str(v) for k, v in dict(raw_headers).items()})
    except Exception:
        pass
    try:
        raw_headers = await request_obj.all_headers()
        headers.update({str(k).lower(): str(v) for k, v in dict(raw_headers or {}).items()})
    except Exception:
        pass

    friendly_name = headers.get('x-fb-friendly-name', '').strip()
    if friendly_name:
        return friendly_name

    try:
        post_data = getattr(request_obj, 'post_data', '') or ''
        parsed = parse_qs(post_data)
        for key in ('fb_api_req_friendly_name', 'friendly_name', 'fb_api_caller_class'):
            values = parsed.get(key) or []
            if values:
                return str(values[0]).strip()
    except Exception:
        pass
    return ''


class _FacebookPostNetworkMonitor:
    """Watch Facebook GraphQL responses for the post-creation mutation."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self._done = asyncio.Event()
        self._tasks: Set[asyncio.Task[Any]] = set()
        self._started = False
        self._stopped = False
        self.mutation_seen = False
        self.status_seen = False
        self.result: Optional[bool] = None
        self.detail = ''

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.page.on('response', self._listener)

    def stop(self) -> None:
        if not self._started or self._stopped:
            return
        self._stopped = True
        try:
            self.page.remove_listener('response', self._listener)
        except Exception:
            pass
        for task in list(self._tasks):
            if not task.done():
                task.cancel()

    def _listener(self, response: Any) -> None:
        if self._stopped:
            return
        try:
            task = asyncio.create_task(self._handle_response(response))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except Exception:
            pass

    def _finish(self, result: bool, detail: str) -> None:
        if self.result is not None:
            return
        self.result = result
        self.detail = detail
        self._done.set()

    async def _handle_response(self, response: Any) -> None:
        try:
            if '/api/graphql' not in str(response.url or ''):
                return
            friendly_name = await _graphql_friendly_name_from_response(response)
            is_create_mutation = 'ComposerStoryCreateMutation' in friendly_name
            is_status_query = 'fetchComposerPostCreationStatusQuery' in friendly_name
            if not is_create_mutation and not is_status_query:
                return
            if is_create_mutation:
                self.mutation_seen = True
            if is_status_query:
                self.status_seen = True

            status = int(getattr(response, 'status', 0) or 0)
            if status >= 400:
                if is_create_mutation or self.mutation_seen:
                    self._finish(False, f'Facebook GraphQL publish request failed with HTTP {status}')
                return

            try:
                payload = _parse_graphql_payload(await response.text())
            except Exception as exc:
                self.detail = f'Facebook GraphQL publish response was not readable: {exc}'
                return

            error_detail = _extract_first_graphql_error(payload)
            if error_detail:
                if is_create_mutation or self.mutation_seen:
                    self._finish(False, f'Facebook GraphQL publish error: {error_detail}')
                return

            post_id = _extract_graphql_post_id(payload)
            if post_id:
                if is_create_mutation or self.mutation_seen:
                    self._finish(True, f'network publish confirmed by GraphQL post_id={post_id}')
                elif is_status_query:
                    self.detail = 'Facebook GraphQL publish status appeared before the publish mutation'
                return

            if is_create_mutation:
                self.detail = 'Facebook GraphQL publish mutation returned no post_id'
            elif is_status_query and not self.detail:
                self.detail = 'Facebook GraphQL publish status returned no post_id'
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(f'GraphQL publish monitor ignored response parse error: {exc}')

    async def wait(self, timeout_ms: int) -> Tuple[Optional[bool], str]:
        try:
            await asyncio.wait_for(self._done.wait(), timeout=timeout_ms / 1000)
        except asyncio.TimeoutError:
            if self.mutation_seen:
                return None, self.detail or 'Facebook GraphQL publish mutation was seen but no post_id was returned'
            return None, 'Facebook GraphQL publish mutation was not observed'
        if self.result is not None:
            return self.result, self.detail
        if self.mutation_seen:
            return None, self.detail or 'Facebook GraphQL publish mutation did not produce a post_id'
        return None, self.detail or 'Facebook GraphQL publish mutation was not observed'


def _start_post_network_monitor(page: Page) -> Optional[_FacebookPostNetworkMonitor]:
    if not POST_NETWORK_CONFIRMATION_ENABLED:
        return None
    monitor = _FacebookPostNetworkMonitor(page)
    monitor.start()
    return monitor


async def _await_post_network_confirmation(
    network_monitor: Optional[_FacebookPostNetworkMonitor],
) -> Tuple[Optional[bool], str]:
    if network_monitor is None:
        return None, 'network confirmation disabled'
    try:
        return await network_monitor.wait(POST_NETWORK_CONFIRMATION_TIMEOUT_MS)
    finally:
        network_monitor.stop()


async def _facebook_posting_in_progress_visible(page: Page) -> bool:
    try:
        dialogs = await page.locator("div[role='dialog']").all()
    except Exception:
        dialogs = []
    for dialog in reversed(dialogs):
        try:
            state = await dialog.evaluate(
                """
                dialog => {
                    const visible = element => {
                        if (!element) return false;
                        const rect = element.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    };
                    const normalize = value => (value || '').replace(/\\s+/g, ' ').trim();
                    const postingRe = /^(Posting|Publishing|Sharing|جار(?:ي)?\\s*النشر|يتم\\s*النشر|نشر\\s*جار)$/i;
                    const actionRe = /^(Post|Publish|Share|نشر|مشاركة)$/i;
                    const exactPosting = Array.from(dialog.querySelectorAll('*')).some(element => {
                        if (!visible(element)) return false;
                        return postingRe.test(normalize(element.innerText || element.textContent || ''));
                    });
                    const busy = Boolean(dialog.querySelector('[role="progressbar"], [aria-busy="true"]'));
                    const disabledAction = Array.from(dialog.querySelectorAll('button, div[role="button"], a[role="button"]')).some(element => {
                        if (!visible(element)) return false;
                        const text = normalize(
                            element.innerText ||
                            element.getAttribute('aria-label') ||
                            element.getAttribute('title') ||
                            element.getAttribute('value') ||
                            ''
                        );
                        if (!actionRe.test(text)) return false;
                        return Boolean(
                            element.disabled ||
                            element.getAttribute('disabled') !== null ||
                            element.getAttribute('aria-disabled') === 'true' ||
                            element.closest('[aria-disabled="true"]') ||
                            element.closest('[disabled]')
                        );
                    });
                    return { exactPosting, busy, disabledAction };
                }
                """
            )
            if not state.get('exactPosting') or not (state.get('busy') or state.get('disabledAction')):
                continue
            return True
        except Exception:
            continue
    try:
        live_text = await page.locator("[role='status'], [aria-live], [role='progressbar']").evaluate_all(
            """
            elements => elements
                .filter(element => {
                    const rect = element.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                })
                .map(element => (element.innerText || element.textContent || '').replace(/\\s+/g, ' ').trim())
                .join('\\n')
            """
        )
        return bool(_FACEBOOK_POSTING_IN_PROGRESS_RE.search(str(live_text or '')))
    except Exception:
        return False


async def _wait_for_facebook_posting_to_settle(
    page: Page,
    timeout_ms: int = POST_PUBLISH_IN_PROGRESS_TIMEOUT_MS,
) -> Tuple[bool, str]:
    deadline = asyncio.get_running_loop().time() + (max(1, timeout_ms) / 1000)
    saw_posting_overlay = False
    next_security_check = 0.0

    while asyncio.get_running_loop().time() < deadline:
        now = asyncio.get_running_loop().time()
        if now >= next_security_check:
            security_detail = await _facebook_navigation_fatal_block_detail(page)
            if security_detail:
                return False, security_detail
            next_security_check = now + 3.0

        if await _dismiss_post_publish_blocking_popups(page):
            if saw_posting_overlay:
                await asyncio.sleep(0.5)
                continue

        if await _facebook_posting_in_progress_visible(page):
            saw_posting_overlay = True
            await asyncio.sleep(0.75)
            continue

        if saw_posting_overlay:
            return True, 'Facebook posting overlay cleared'
        return True, 'Facebook posting overlay was not visible'

    if saw_posting_overlay:
        return False, (
            f'Facebook still showed the Posting overlay after {timeout_ms / 1000:.0f}s. '
            'The publish click may still be processing.'
        )
    return True, 'Facebook posting overlay was not visible'


def _adaptive_verify_timeout_ms(post_type: str, has_media: bool) -> int:
    """Return a publish-confirmation timeout tuned to the post payload."""
    normalized_post_type = str(post_type or 'post').strip().lower()
    if normalized_post_type == 'video':
        return 25000
    if normalized_post_type == 'image' or has_media:
        return 16000
    return 9000


async def _verify_post_published(
    page: Page,
    *,
    caption: str = '',
    post_type: str = 'post',
    timeout_ms: int = 12000,
    accept_publish_click: bool = False,
) -> Tuple[bool, str]:
    """Verify Facebook accepted the publish click without retrying duplicate-prone routes."""
    normalized_caption = re.sub(r'\s+', ' ', (caption or '').strip())
    effective_timeout_ms = timeout_ms
    if POST_ACCEPT_PUBLISH_CLICK_NO_ERROR_AS_SUCCESS:
        effective_timeout_ms = min(timeout_ms, POST_PUBLISH_NO_ERROR_GRACE_MS)
    try:
        handle = await page.wait_for_function(
            """([caption, postType, acceptComposerClose, acceptPublishClick]) => {
                const normalize = value => (value || '').replace(/\\s+/g, ' ').trim();
                const visible = element => Boolean(element && element.offsetParent !== null);
                const disabled = element => Boolean(
                    element.disabled ||
                    element.getAttribute('disabled') !== null ||
                    element.getAttribute('aria-disabled') === 'true' ||
                    element.closest('[aria-disabled="true"]') ||
                    element.closest('[disabled]')
                );
                const dialogs = Array.from(document.querySelectorAll('div[role="dialog"]'));
                const composerVisible = dialogs.some(dialog => {
                    if (!visible(dialog)) {
                        return false;
                    }
                    const dialogText = normalize(dialog.innerText || '');
                    const hasTextbox = Boolean(dialog.querySelector(
                        "[contenteditable='true'][role='textbox'], textarea, div[role='textbox']"
                    ));
                    const hasPublishControl = /\\b(Post|Publish|Share|Send|Next)\\b|نشر|مشاركة|التالي/i.test(dialogText);
                    const hasComposerText = /Create post|Write something|What's on your mind|Create a post|إنشاء منشور|اكتب/i.test(dialogText);
                    return hasTextbox && (hasPublishControl || hasComposerText);
                });
                const bodyText = document.body.innerText || '';
                const visibleDialogText = dialogs
                    .filter(visible)
                    .map(dialog => normalize(dialog.innerText || ''))
                    .join('\\n');
                const alertText = Array.from(document.querySelectorAll('[role="alert"], [aria-live]'))
                    .filter(visible)
                    .map(el => normalize(el.innerText || ''))
                    .join('\\n');
                const activeMessageText = `${visibleDialogText}\\n${alertText}`;
                const errorText = /couldn.?t post|couldn.?t upload|upload failed|try again|something went wrong|not published|failed to publish|تعذر|فشل|خطأ/i.test(activeMessageText);
                const currentUrl = window.location.href || '';
                const securityText =
                    /checkpoint|\\/nt\\/screen|recover\\/initiate|login_identify/i.test(currentUrl) ||
                    /checkpoint|confirm your identity|temporarily blocked|account restricted|unusual activity|trusted device|locked your account|unlock your account|log in with another device|may have been hacked|we can.?t match the device|تحقق من هويتك|تم تقييد|قيود|تم قفل/i.test(activeMessageText);
                if (securityText) {
                    return 'security_text';
                }
                if (errorText) {
                    return 'error_text';
                }
                const publishPattern = /^(Post|Publish|Share|Send|Post now|Publish now|نشر|انشر|مشاركة)$/i;
                const actionablePublishVisible = dialogs
                    .filter(visible)
                    .flatMap(dialog => Array.from(dialog.querySelectorAll('button, div[role="button"], a[role="button"]')))
                    .some(element => {
                        if (!visible(element) || disabled(element)) {
                            return false;
                        }
                        const text = normalize(
                            element.innerText ||
                            element.getAttribute('aria-label') ||
                            element.getAttribute('title') ||
                            element.getAttribute('value') ||
                            ''
                        );
                        return publishPattern.test(text);
                    });
                if (
                    /post published|posted|shared|your post is now published|your post has been published|تم النشر|تمت المشاركة|تم نشر منشورك/i.test(bodyText)
                    && !composerVisible
                ) {
                    return 'success_text';
                }
                if (
                    postType === 'video'
                    && !composerVisible
                    && /processing|being processed|video is processing|video.*processing|جاري.*معالجة|معالجة.*فيديو/i.test(bodyText)
                ) {
                    return 'video_processing';
                }
                if (caption && !composerVisible) {
                    const visibleText = Array.from(document.querySelectorAll('body *'))
                        .filter(el => el.offsetParent !== null && !el.closest('div[role="dialog"]'))
                        .map(el => normalize(el.innerText || ''))
                        .join('\\n');
                    if (visibleText.includes(caption)) {
                        return 'caption_visible';
                    }
                }
                if (acceptComposerClose && !composerVisible) {
                    return 'composer_closed';
                }
                if (postType === 'video' && acceptPublishClick && !actionablePublishVisible) {
                    return 'video_publish_click_accepted';
                }
                return false;
            }""",
            arg=[
                normalized_caption,
                post_type,
                POST_ACCEPT_COMPOSER_CLOSE_AS_SUCCESS,
                accept_publish_click,
            ],
            timeout=effective_timeout_ms,
        )
        try:
            outcome = str(await handle.json_value() or '')
        except Exception:
            outcome = ''
        if outcome == 'security_text':
            return False, 'Facebook showed a checkpoint/account restriction after the publish click'
        if outcome == 'error_text':
            return False, 'Facebook showed a posting error after the publish click'
        if outcome == 'composer_closed':
            return True, 'publish accepted; composer closed without a visible error'
        if outcome == 'video_processing':
            return True, 'video publish accepted; Facebook is processing the video'
        if outcome == 'video_publish_click_accepted':
            return True, 'video publish accepted after final publish click; no active Facebook error was visible'
        if outcome == 'caption_visible':
            return True, 'publish confirmation detected by caption visibility'
        return True, 'publish confirmation detected'
    except Exception as exc:
        exc_text = str(exc).lower()
        if any(marker in exc_text for marker in ('target page', 'browser has been closed', 'page closed', 'crash')):
            return False, f'publish confirmation could not be checked because the browser page closed: {exc}'
        if POST_ACCEPT_PUBLISH_CLICK_NO_ERROR_AS_SUCCESS:
            failure_detail = await _publish_click_visible_failure_detail(page)
            if failure_detail:
                return False, failure_detail
            return True, (
                f'publish click accepted; no visible Facebook error after {effective_timeout_ms}ms'
            )
        return False, 'publish confirmation was not detected'


async def _verify_target_feed_after_publish(
    page: Page,
    *,
    target_url: str = '',
    caption: str = '',
    page_name: str = '',
    timeout_ms: Optional[int] = None,
) -> Tuple[bool, str]:
    if not POST_TARGET_FEED_CONFIRMATION_ENABLED:
        return False, 'target page feed verification is disabled'
    normalized_caption = re.sub(r'\s+', ' ', (caption or '').strip())
    if not target_url:
        return False, 'target page feed verification skipped because no target URL was available'
    if not normalized_caption:
        return False, 'target page feed verification skipped because the post has no caption to match'

    effective_timeout_ms = timeout_ms or POST_TARGET_FEED_CONFIRMATION_TIMEOUT_MS
    try:
        logger.info(f'Verifying publish on target page feed: {_safe_log_url(target_url)}')
        await page.goto(target_url, wait_until='domcontentloaded', timeout=max(12000, effective_timeout_ms + 6000))
        fatal_detail = await _facebook_navigation_fatal_block_detail(page)
        if fatal_detail:
            return False, f'target page feed verification blocked: {fatal_detail[:180]}'
        await _wait_for_facebook_ui_ready(page, timeout=min(5000, effective_timeout_ms))
        await _dismiss_common_facebook_popups(page)

        handle = await page.wait_for_function(
            """([caption, pageName]) => {
                const normalize = value => (value || '').replace(/\\s+/g, ' ').trim();
                const visible = element => {
                    if (!element) return false;
                    const rect = element.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0 && element.offsetParent !== null;
                };
                const normalizedCaption = normalize(caption);
                const captionLower = normalizedCaption.toLowerCase();
                const normalizedPage = normalize(pageName).toLowerCase();
                const captionIsShort = normalizedCaption.length < 8;
                const recentPattern = /(?:just now|\\bnow\\b|\\b[\\d٠-٩]+\\s*(?:s|sec|secs|second|seconds|m|min|mins|minute|minutes)\\b|الآن|[\\d٠-٩]+\\s*(?:ث|د)|دقيقة|دقائق)/i;
                const articleTexts = Array.from(document.querySelectorAll('[role="article"], div[data-pagelet*="FeedUnit"], div[data-ad-preview="message"]'))
                    .filter(visible)
                    .map(element => normalize(element.innerText || element.textContent || ''))
                    .filter(Boolean);
                for (const text of articleTexts.slice(0, 12)) {
                    const lower = text.toLowerCase();
                    if (!lower.includes(captionLower)) {
                        continue;
                    }
                    if (normalizedPage && !lower.includes(normalizedPage)) {
                        continue;
                    }
                    if (recentPattern.test(text)) {
                        return 'recent_article_caption_visible';
                    }
                    if (!captionIsShort) {
                        return 'article_caption_visible';
                    }
                }
                if (!captionIsShort) {
                    const visibleText = Array.from(document.querySelectorAll('body *'))
                        .filter(element => visible(element) && !element.closest('div[role="dialog"]'))
                        .map(element => normalize(element.innerText || element.textContent || ''))
                        .join('\\n');
                    if (visibleText.toLowerCase().includes(captionLower)) {
                        return 'target_feed_caption_visible';
                    }
                }
                return false;
            }""",
            arg=[normalized_caption, page_name or _derive_page_name(target_url, '')],
            timeout=effective_timeout_ms,
        )
        outcome = str(await handle.json_value() or '')
        if outcome == 'recent_article_caption_visible':
            return True, 'publish confirmation detected on target page feed by recent caption visibility'
        if outcome == 'article_caption_visible':
            return True, 'publish confirmation detected on target page feed by post caption visibility'
        if outcome == 'target_feed_caption_visible':
            return True, 'publish confirmation detected on target page feed by caption visibility'
        return False, 'target page feed check did not find the published caption'
    except Exception as exc:
        exc_text = str(exc).lower()
        if any(marker in exc_text for marker in ('target page', 'browser has been closed', 'page closed', 'crash')):
            return False, f'target page feed verification could not run because the browser page closed: {exc}'
        return False, 'target page feed check did not find the published caption'


async def _verify_post_published_with_target_fallback(
    page: Page,
    *,
    caption: str = '',
    post_type: str = 'post',
    timeout_ms: int = 12000,
    accept_publish_click: bool = False,
    target_url: str = '',
    page_name: str = '',
) -> Tuple[bool, str]:
    verified, verify_reason = await _verify_post_published(
        page,
        caption=caption,
        post_type=post_type,
        timeout_ms=timeout_ms,
        accept_publish_click=accept_publish_click,
    )
    if verified:
        return True, verify_reason
    if _is_facebook_security_failure(verify_reason):
        return False, verify_reason

    feed_verified, feed_reason = await _verify_target_feed_after_publish(
        page,
        target_url=target_url,
        caption=caption,
        page_name=page_name,
    )
    if feed_verified:
        return True, f'{feed_reason}; UI confirmation fallback reason: {verify_reason}'
    return False, f'{verify_reason}; target feed check: {feed_reason}'


def _initial_ui_confirmation_is_fatal(detail: str) -> bool:
    text = str(detail or '').lower()
    return (
        'posting error' in text
        or 'checkpoint' in text
        or 'account restriction' in text
        or 'security' in text
        or 'facebook showed a posting error' in text
    )


async def _await_initial_publish_confirmation(
    page: Page,
    network_monitor: Optional[_FacebookPostNetworkMonitor],
    *,
    caption: str = '',
    post_type: str = 'post',
) -> Tuple[Optional[bool], str]:
    """
    Race the network confirmation with the same safe UI signal we already
    accept later. This avoids waiting the full GraphQL timeout when Facebook
    closes the composer immediately after accepting the publish click.
    """
    if network_monitor is None:
        return None, 'network confirmation disabled'
    if not POST_INITIAL_UI_CONFIRMATION_ENABLED:
        return await _await_post_network_confirmation(network_monitor)

    network_task = asyncio.create_task(_await_post_network_confirmation(network_monitor))
    ui_task = asyncio.create_task(
        _verify_post_published(
            page,
            caption=caption,
            post_type=post_type,
            timeout_ms=POST_INITIAL_UI_CONFIRMATION_TIMEOUT_MS,
            accept_publish_click=False,
        )
    )

    try:
        done, pending = await asyncio.wait(
            {network_task, ui_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if ui_task in done:
            ui_success, ui_detail = await ui_task
            if ui_success:
                network_task.cancel()
                await asyncio.gather(network_task, return_exceptions=True)
                return True, f'quick UI publish confirmation: {ui_detail}'
            if _initial_ui_confirmation_is_fatal(ui_detail):
                network_task.cancel()
                await asyncio.gather(network_task, return_exceptions=True)
                return False, ui_detail
            network_result, network_detail = await network_task
            return network_result, network_detail

        network_result, network_detail = await network_task
        if pending:
            ui_task.cancel()
            await asyncio.gather(ui_task, return_exceptions=True)
        return network_result, network_detail
    finally:
        for task in (network_task, ui_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(network_task, ui_task, return_exceptions=True)


async def _confirm_post_published(
    page: Page,
    *,
    caption: str = '',
    post_type: str = 'post',
    timeout_ms: int = 12000,
    accept_publish_click: bool = False,
    network_monitor: Optional[_FacebookPostNetworkMonitor] = None,
    target_url: str = '',
    page_name: str = '',
) -> Tuple[bool, str]:
    if network_monitor is not None:
        network_result, network_detail = await _await_initial_publish_confirmation(
            page,
            network_monitor,
            caption=caption,
            post_type=post_type,
        )
        if network_result is not None:
            return bool(network_result), network_detail
        logger.info(f'Network publish confirmation unavailable; falling back to UI verification: {network_detail}')

    return await _verify_post_published_with_target_fallback(
        page,
        caption=caption,
        post_type=post_type,
        timeout_ms=timeout_ms,
        accept_publish_click=accept_publish_click,
        target_url=target_url,
        page_name=page_name,
    )


def _with_diagnostic(message: str, diagnostic_path: str) -> str:
    return f'{message}\nDiagnostic: {diagnostic_path}' if diagnostic_path else message


def _publish_sent_unconfirmed(message: str, diagnostic_path: str) -> str:
    detail = (
        f'{PUBLISH_SENT_UNCONFIRMED_MARKER}: {message} '
        'The bot will not retry another route because the publish click may already have created the post.'
    )
    return _with_diagnostic(detail, diagnostic_path)


def _is_publish_sent_unconfirmed(result: str) -> bool:
    return PUBLISH_SENT_UNCONFIRMED_MARKER in str(result or '')


_FACEBOOK_SECURITY_FAILURE_RE = re.compile(
    r'locked|checkpoint|account restricted|temporarily blocked|confirm your identity|'
    r'unusual activity|trusted device|suspended|unlock|hacked|recover/initiate|'
    r'login_identify|قفل|تحقق|تأكيد|مخترق',
    re.I,
)
_FACEBOOK_SECURITY_TEXT_RE = re.compile(
    r'account (?:has been )?locked|we locked your account|unlock your account|'
    r'may have been hacked|log in with another device|we can.?t match the device|'
    r'why can.?t i use this device|confirm your identity|security checkpoint|'
    r'unusual activity|temporarily blocked|account restricted|trusted device|'
    r'تم قفل|إلغاء قفل حسابك|قد يكون مخترق|نقطة تحقق|تأكيد هويتك|'
    r'نشاط غير معتاد|قيود',
    re.I,
)
_FACEBOOK_POSTING_IN_PROGRESS_RE = re.compile(
    r'(?:^|\s)(Posting|Publishing|Sharing|جار(?:ي)?\s*النشر|يتم\s*النشر|نشر\s*جار)(?:\s|$)',
    re.I,
)


def _security_relevant_detail(detail: str) -> str:
    text = str(detail or '')
    if PUBLISH_SENT_UNCONFIRMED_MARKER in text:
        # Only the original page result should decide cooldown. Ignore later
        # batch-skip boilerplate generated after the first page result.
        text = text.split(';', 1)[0]
    return text


def _safe_log_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        if parsed.path == '/profile.php':
            query = parse_qs(parsed.query)
            page_id = (query.get('id') or [''])[0]
            return f'{parsed.scheme}://{parsed.netloc}/profile.php?id={page_id}' if page_id else f'{parsed.scheme}://{parsed.netloc}/profile.php'
        return f'{parsed.scheme}://{parsed.netloc}{parsed.path}'
    except Exception:
        return '<url unavailable>'


def _post_step(page_label: str, stage: str, detail: str = '') -> None:
    suffix = f' | {detail}' if detail else ''
    logger.info(f'POST_STEP page="{page_label or "Unknown page"}" stage="{stage}"{suffix}')


def _is_ad_flow_url(url: str) -> bool:
    lowered = (url or '').lower()
    return any(part in lowered for part in _AD_FLOW_URL_PARTS)


def _is_logged_out_url(url: str) -> bool:
    lowered = (url or '').lower()
    return any(part in lowered for part in _LOGGED_OUT_URL_PARTS)


def _facebook_posts_routes(target_url: str) -> List[str]:
    parsed = urlparse(target_url)

    def unique(routes: List[str]) -> List[str]:
        ordered: List[str] = []
        for route in routes:
            if route and route not in ordered:
                ordered.append(route)
        return ordered

    # Generate direct composer modal route options for speed optimization
    if parsed.path == '/profile.php':
        query_modal = parse_qs(parsed.query)
        query_modal['modal'] = ['composer']
        composer_query = urlencode(query_modal, doseq=True)
        composer_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', composer_query, ''))

        query_posts = parse_qs(parsed.query)
        query_posts['sk'] = ['posts']
        posts_query = urlencode(query_posts, doseq=True)
        posts_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', posts_query, ''))

        if POST_COMPOSER_ENTRY_MODE in {'target', 'base', 'page', 'single'}:
            return unique([target_url, composer_url, posts_url])
        if POST_COMPOSER_ENTRY_MODE in {'posts', 'timeline'}:
            return unique([posts_url, target_url, composer_url])
        return unique([composer_url, target_url, posts_url])

    clean_url = target_url.rstrip('/')
    if '?' in target_url:
        composer_url = f"{target_url}&modal=composer"
    else:
        composer_url = f"{clean_url}/?modal=composer"

    posts_url = f'{clean_url}/posts'
    sk_posts_url = f'{clean_url}/?sk=posts'
    if POST_COMPOSER_ENTRY_MODE in {'target', 'base', 'page', 'single'}:
        return unique([target_url, composer_url, posts_url, sk_posts_url])
    if POST_COMPOSER_ENTRY_MODE in {'posts', 'timeline'}:
        return unique([posts_url, sk_posts_url, target_url, composer_url])
    return unique([composer_url, target_url, posts_url, sk_posts_url])


def _redis_client() -> Optional[Any]:
    global _REDIS_CLIENT
    if _REDIS_CLIENT is not None:
        return _REDIS_CLIENT
    if not REDIS_URL or redis is None:
        return None
    _REDIS_CLIENT = redis.from_url(REDIS_URL, socket_connect_timeout=5, socket_timeout=5)
    return _REDIS_CLIENT


def _try_acquire_post_operation_slot() -> bool:
    client = _redis_client()
    if client is None:
        return True
    try:
        current = int(client.incr(POST_OPERATION_SLOT_KEY))
        client.expire(POST_OPERATION_SLOT_KEY, POST_OPERATION_SLOT_TTL_SECONDS)
        if current <= POST_CONCURRENCY:
            return True
        client.decr(POST_OPERATION_SLOT_KEY)
        return False
    except Exception as exc:
        logger.warning(f'Posting operation slot unavailable, proceeding without shared slot: {exc}')
        return True


def _reset_stale_post_operation_slot() -> bool:
    client = _redis_client()
    if client is None:
        return False
    try:
        ttl = int(client.ttl(POST_OPERATION_SLOT_KEY))
        current = int(client.get(POST_OPERATION_SLOT_KEY) or 0)
        if current > 0 and ttl < 0:
            client.delete(POST_OPERATION_SLOT_KEY)
            logger.warning(f'Reset stale posting operation slot counter: count={current}, ttl={ttl}')
            return True
    except Exception as exc:
        logger.warning(f'Failed to inspect posting operation slot: {exc}')
    return False


def _acquire_post_operation_slot_blocking() -> bool:
    deadline = time.monotonic() + POST_OPERATION_SLOT_WAIT_SECONDS
    while time.monotonic() < deadline:
        if _try_acquire_post_operation_slot():
            return True
        _reset_stale_post_operation_slot()
        time.sleep(2)
    return False


def _release_post_operation_slot() -> None:
    client = _redis_client()
    if client is None:
        return
    try:
        current = int(client.decr(POST_OPERATION_SLOT_KEY))
        if current <= 0:
            client.delete(POST_OPERATION_SLOT_KEY)
    except Exception as exc:
        logger.warning(f'Failed to release posting operation slot: {exc}')


def _run_with_post_operation_slot(factory: Callable[[], Any], timeout_result: Any) -> Any:
    if not POST_WORKER_USE_OPERATION_SLOT:
        return factory()
    operation_slot_acquired = _acquire_post_operation_slot_blocking()
    if not operation_slot_acquired:
        return timeout_result
    try:
        return factory()
    finally:
        _release_post_operation_slot()


def _post_account_lock_name(account_lock_key: str) -> str:
    digest = hashlib.sha256(account_lock_key.encode('utf-8')).hexdigest()
    return f'fb-post-account-lock:{digest}'


def _redis_lock_metadata_key(lock_name: str) -> str:
    return f'{lock_name}:meta'


def _current_rq_job_id() -> str:
    if get_current_job is None:
        return ''
    try:
        job = get_current_job()
        return str(getattr(job, 'id', '') or '') if job is not None else ''
    except Exception:
        return ''


def _current_rq_job_status() -> str:
    if get_current_job is None:
        return ''
    try:
        job = get_current_job()
        if job is None:
            return ''
        status = getattr(job, 'get_status', None)
        if callable(status):
            return str(status() or '')
        return str(getattr(job, 'status', '') or '')
    except Exception:
        return ''


def _redis_lock_owner_metadata(owner_kind: str, detail: str = '') -> Dict[str, Any]:
    now = time.time()
    return {
        'owner_kind': owner_kind,
        'detail': str(detail or '')[:240],
        'job_id': _current_rq_job_id(),
        'job_status': _current_rq_job_status(),
        'pid': os.getpid(),
        'thread_id': threading.get_ident(),
        'host': socket.gethostname(),
        'started_at': now,
        'last_heartbeat': now,
    }


def _write_redis_lock_metadata(
    client: Any,
    lock_name: str,
    metadata: Dict[str, Any],
    ttl_seconds: int,
) -> None:
    metadata['last_heartbeat'] = time.time()
    metadata['job_status'] = _current_rq_job_status()
    metadata_ttl = max(POST_LOCK_METADATA_TTL_SECONDS, ttl_seconds + 60)
    client.setex(
        _redis_lock_metadata_key(lock_name),
        metadata_ttl,
        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
    )


def _start_redis_lock_heartbeat(
    client: Any,
    lock_name: str,
    owner_kind: str,
    detail: str,
    ttl_seconds: int,
) -> Tuple[threading.Event, str]:
    stop_event = threading.Event()
    metadata = _redis_lock_owner_metadata(owner_kind, detail)
    metadata_key = _redis_lock_metadata_key(lock_name)

    try:
        _write_redis_lock_metadata(client, lock_name, metadata, ttl_seconds)
    except Exception as exc:
        logger.warning(f'Could not write Redis lock metadata for {lock_name}: {exc}')

    def heartbeat() -> None:
        while not stop_event.wait(POST_LOCK_HEARTBEAT_INTERVAL_SECONDS):
            try:
                _write_redis_lock_metadata(client, lock_name, metadata, ttl_seconds)
            except Exception as exc:
                logger.debug(f'Redis lock heartbeat failed for {lock_name}: {exc}')

    thread = threading.Thread(
        target=heartbeat,
        name=f'fb-lock-heartbeat-{hashlib.sha1(lock_name.encode()).hexdigest()[:8]}',
        daemon=True,
    )
    thread.start()
    return stop_event, metadata_key


def _stop_redis_lock_heartbeat(heartbeat: Optional[Tuple[threading.Event, str]], client: Optional[Any] = None) -> None:
    if heartbeat is None:
        return
    stop_event, metadata_key = heartbeat
    try:
        stop_event.set()
    except Exception:
        pass
    if client is not None:
        try:
            client.delete(metadata_key)
        except Exception as exc:
            logger.debug(f'Could not delete Redis lock metadata {metadata_key}: {exc}')


async def _release_redis_lock_with_metadata(
    resource: Any,
    heartbeat: Optional[Tuple[threading.Event, str]],
    client: Optional[Any],
) -> None:
    if heartbeat is not None:
        try:
            heartbeat[0].set()
        except Exception:
            pass
    await asyncio.to_thread(resource.release)
    _stop_redis_lock_heartbeat(heartbeat, client)


def _release_redis_lock_with_metadata_sync(
    resource: Any,
    heartbeat: Optional[Tuple[threading.Event, str]],
    client: Optional[Any],
) -> None:
    if heartbeat is not None:
        try:
            heartbeat[0].set()
        except Exception:
            pass
    resource.release()
    _stop_redis_lock_heartbeat(heartbeat, client)


def _read_redis_lock_metadata(client: Any, lock_name: str) -> Dict[str, Any]:
    try:
        raw_value = client.get(_redis_lock_metadata_key(lock_name))
    except Exception:
        return {}
    if not raw_value:
        return {}
    try:
        if isinstance(raw_value, bytes):
            raw_value = raw_value.decode('utf-8', errors='ignore')
        payload = json.loads(str(raw_value))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _metadata_heartbeat_age_seconds(metadata: Dict[str, Any]) -> Optional[float]:
    try:
        heartbeat_at = float(metadata.get('last_heartbeat') or metadata.get('started_at') or 0)
    except (TypeError, ValueError):
        return None
    if heartbeat_at <= 0:
        return None
    return max(0.0, time.time() - heartbeat_at)


def _post_account_advisory_lock_id(account_lock_key: str) -> int:
    digest = hashlib.sha256(account_lock_key.encode('utf-8')).digest()
    return int.from_bytes(digest[:8], 'big', signed=True)


def _acquire_postgres_account_lock(account_lock_key: str, wait_seconds: Optional[int] = None) -> Optional[Any]:
    if not DATABASE_URL or psycopg is None:
        return None

    lock_id = _post_account_advisory_lock_id(account_lock_key)
    deadline = time.monotonic() + max(1, wait_seconds if wait_seconds is not None else POST_ACCOUNT_LOCK_WAIT_SECONDS)
    conn = None
    try:
        conn = psycopg.connect(DATABASE_URL, connect_timeout=10)
        while time.monotonic() < deadline:
            with conn.cursor() as cursor:
                cursor.execute('SELECT pg_try_advisory_lock(%s)', (lock_id,))
                acquired = cursor.fetchone()
            if acquired and acquired[0]:
                logger.info(f'PostgreSQL account publish lock acquired: {lock_id}')
                return conn
            time.sleep(2)
    except Exception as exc:
        logger.warning(f'PostgreSQL account publish lock unavailable: {exc}')
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    return None


def _redis_lock_ttl_seconds(client: Any, lock_name: str) -> Optional[float]:
    try:
        ttl_ms = int(client.pttl(lock_name))
    except Exception:
        return None
    if ttl_ms <= 0:
        return None
    return ttl_ms / 1000.0


def _worker_heartbeat_age_seconds(worker: Any) -> Optional[float]:
    try:
        heartbeat_value: Any = getattr(worker, 'last_heartbeat', None)
        if callable(heartbeat_value):
            heartbeat_value = heartbeat_value()
        if heartbeat_value is None:
            return None
        if isinstance(heartbeat_value, datetime):
            if heartbeat_value.tzinfo is None:
                heartbeat_value = heartbeat_value.replace(tzinfo=timezone.utc)
            heartbeat_ts = float(heartbeat_value.timestamp())
        elif hasattr(heartbeat_value, 'timestamp'):
            heartbeat_ts = float(cast(Any, heartbeat_value).timestamp())
        else:
            heartbeat_ts = float(cast(Any, heartbeat_value))
        return max(0.0, time.time() - heartbeat_ts)
    except Exception:
        return None


def _worker_has_recent_heartbeat(worker: Any) -> bool:
    age = _worker_heartbeat_age_seconds(worker)
    # If RQ does not expose a heartbeat, keep the old safe behavior and treat it
    # as active instead of accidentally clearing a live worker's lock.
    return age is None or age <= POST_LOCK_STALE_HEARTBEAT_SECONDS


def _active_rq_job_ids_excluding_current(client: Any) -> Optional[Set[str]]:
    if Worker is None:
        return None
    current_job_id = _current_rq_job_id()
    active_job_ids: Set[str] = set()
    try:
        for worker in Worker.all(connection=client):
            try:
                job_id = str(worker.get_current_job_id() or '')
            except Exception:
                job_id = ''
            if job_id and not _worker_has_recent_heartbeat(worker):
                logger.warning(
                    'Ignoring stale RQ worker heartbeat while checking Redis lock cleanup: '
                    f'worker={getattr(worker, "name", "")} job_id={job_id} '
                    f'heartbeat_age={_worker_heartbeat_age_seconds(worker)}'
                )
                continue
            if job_id and job_id != current_job_id:
                active_job_ids.add(job_id)
    except Exception as exc:
        logger.warning(f'Could not inspect active RQ jobs before stale lock cleanup: {exc}')
        return None
    return active_job_ids


def _rq_job_status_by_id(client: Any, job_id: str) -> str:
    if not job_id or Job is None:
        return ''
    try:
        job = Job.fetch(job_id, connection=client)
        status = getattr(job, 'get_status', None)
        if callable(status):
            return str(status() or '')
        return str(getattr(job, 'status', '') or '')
    except Exception:
        return ''


def _rq_job_has_recent_worker(client: Any, job_id: str) -> bool:
    if not job_id or Worker is None:
        return False
    try:
        for worker in Worker.all(connection=client):
            try:
                worker_job_id = str(worker.get_current_job_id() or '')
            except Exception:
                worker_job_id = ''
            if worker_job_id == job_id and _worker_has_recent_heartbeat(worker):
                return True
    except Exception as exc:
        logger.warning(f'Could not inspect RQ worker owner for Redis lock cleanup: {exc}')
    return False


def _break_stale_redis_lock_if_safe(client: Any, lock_name: str, reason: str) -> bool:
    if not POST_BREAK_STALE_REDIS_LOCKS:
        return False
    ttl_seconds = _redis_lock_ttl_seconds(client, lock_name)
    if ttl_seconds is None:
        return False
    metadata = _read_redis_lock_metadata(client, lock_name)
    if metadata:
        heartbeat_age = _metadata_heartbeat_age_seconds(metadata)
        owner_job_id = str(metadata.get('job_id') or '').strip()
        owner_kind = str(metadata.get('owner_kind') or 'unknown')
        if heartbeat_age is not None and heartbeat_age <= POST_LOCK_STALE_HEARTBEAT_SECONDS:
            logger.info(
                f'Not clearing Redis lock {lock_name}; owner heartbeat is fresh: '
                f'owner={owner_kind} job_id={owner_job_id or "-"} '
                f'heartbeat_age={heartbeat_age:.0f}s ttl={ttl_seconds:.0f}s'
            )
            return False
        if owner_job_id and _rq_job_has_recent_worker(client, owner_job_id):
            logger.info(
                f'Not clearing Redis lock {lock_name}; owner RQ job still has a recent worker: '
                f'job_id={owner_job_id} owner={owner_kind}'
            )
            return False
        job_status = _rq_job_status_by_id(client, owner_job_id)
        logger.warning(
            f'Redis lock {lock_name} appears stale by metadata: reason={reason}; '
            f'owner={owner_kind}; job_id={owner_job_id or "-"}; status={job_status or "-"}; '
            f'heartbeat_age={heartbeat_age if heartbeat_age is not None else -1:.0f}s; '
            f'ttl={ttl_seconds:.0f}s'
        )
    else:
        logger.info(f'Redis lock {lock_name} has no owner metadata; falling back to active RQ worker scan.')
    active_job_ids = _active_rq_job_ids_excluding_current(client)
    if active_job_ids is None and not metadata:
        return False
    if active_job_ids is None:
        active_job_ids = set()
    if active_job_ids and not metadata:
        logger.info(
            f'Not clearing Redis lock {lock_name}; active RQ job(s) still running: '
            f'{", ".join(sorted(active_job_ids)[:5])}'
        )
        return False
    try:
        deleted = int(client.delete(lock_name))
    except Exception as exc:
        logger.warning(f'Could not clear stale Redis lock {lock_name}: {exc}')
        return False
    if deleted:
        try:
            client.delete(_redis_lock_metadata_key(lock_name))
        except Exception:
            pass
        logger.warning(
            f'Cleared stale Redis lock {lock_name}; reason={reason}; '
            f'previous_ttl={ttl_seconds:.0f}s; '
            f'active_jobs={len(active_job_ids)} metadata={bool(metadata)}'
        )
        return True
    return False


def _release_postgres_account_lock(conn: Any, account_lock_key: str) -> None:
    lock_id = _post_account_advisory_lock_id(account_lock_key)
    try:
        with conn.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_unlock(%s)', (lock_id,))
        logger.info(f'PostgreSQL account publish lock released: {lock_id}')
    except Exception as exc:
        logger.warning(f'Failed to release PostgreSQL account publish lock: {exc}')
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _cookie_value_from_json(cookies_json: str, name: str) -> str:
    try:
        cookies = json.loads(cookies_json)
    except Exception:
        return ''
    if not isinstance(cookies, list):
        return ''
    for cookie in cookies:
        if isinstance(cookie, dict) and str(cookie.get('name') or '') == name:
            return str(cookie.get('value') or '').strip()
    return ''


def _cookie_session_key(cookies_json: str) -> str:
    c_user = _cookie_value_from_json(cookies_json, 'c_user')
    xs = _cookie_value_from_json(cookies_json, 'xs')
    datr = _cookie_value_from_json(cookies_json, 'datr')
    if c_user:
        seed = f'c_user:{c_user}:xs:{xs[:32]}'
    elif datr:
        seed = f'datr:{datr}'
    else:
        seed = cookies_json[:4096]
    return hashlib.sha256(seed.encode('utf-8')).hexdigest()


def _local_cookie_session_lock(lock_key: str) -> threading.Lock:
    with _COOKIE_SESSION_LOCKS_GUARD:
        lock = _COOKIE_SESSION_LOCKS.get(lock_key)
        if lock is None:
            lock = threading.Lock()
            _COOKIE_SESSION_LOCKS[lock_key] = lock
        return lock


async def _acquire_cookie_session_guard(
    cookies_json: str,
    label: str,
    wait_seconds: Optional[int] = None,
    enforce_min_interval: bool = True,
) -> Optional[Tuple[Any, ...]]:
    if not POST_COOKIE_SESSION_LOCK_ENABLED:
        return None

    lock_wait_seconds = max(
        0,
        POST_COOKIE_SESSION_LOCK_WAIT_SECONDS if wait_seconds is None else wait_seconds,
    )
    lock_key = f'cookie-session:{_cookie_session_key(cookies_json)}'
    lock_name = _post_account_lock_name(lock_key)
    lock_ttl = max(
        60,
        POST_ACCOUNT_LOCK_TTL_SECONDS,
        POST_COOKIE_SESSION_LOCK_TTL_SECONDS,
        FACEBOOK_POST_TIMEOUT_SECONDS + 300,
    )
    client = _redis_client()

    if client is not None:
        redis_lock = client.lock(
            lock_name,
            timeout=lock_ttl,
            blocking_timeout=lock_wait_seconds,
            sleep=2,
        )
        acquired = await asyncio.to_thread(redis_lock.acquire, blocking=True)
        if not acquired:
            if _break_stale_redis_lock_if_safe(client, lock_name, f'{label} cookie session lock wait timed out'):
                redis_lock = client.lock(
                    lock_name,
                    timeout=lock_ttl,
                    blocking_timeout=0,
                    sleep=0.1,
                )
                acquired = await asyncio.to_thread(redis_lock.acquire, blocking=False)
        if not acquired:
            ttl_seconds = _redis_lock_ttl_seconds(client, lock_name)
            ttl_detail = f' Redis lock TTL remaining ~{ttl_seconds:.0f}s.' if ttl_seconds else ''
            raise TimeoutError(
                f'Timed out after {lock_wait_seconds}s waiting for this Facebook cookie session '
                f'to finish another posting job.{ttl_detail}'
            )
        logger.info(f'Cookie session lock acquired via Redis for {label}: {lock_name}')
        heartbeat = _start_redis_lock_heartbeat(
            client,
            lock_name,
            'cookie_session',
            label,
            lock_ttl,
        )
        guard = ('redis', redis_lock, lock_key, heartbeat)
        await _ensure_cookie_session_can_run(cookies_json, label, guard, enforce_min_interval=enforce_min_interval)
        return guard

    pg_conn = await asyncio.to_thread(_acquire_postgres_account_lock, lock_key, lock_wait_seconds)
    if pg_conn is not None:
        logger.info(f'Cookie session lock acquired via PostgreSQL for {label}')
        guard = ('postgres', pg_conn, lock_key)
        await _ensure_cookie_session_can_run(cookies_json, label, guard, enforce_min_interval=enforce_min_interval)
        return guard

    local_lock = _local_cookie_session_lock(lock_key)
    acquired = await asyncio.to_thread(local_lock.acquire, True, lock_wait_seconds)
    if not acquired:
        raise TimeoutError(
            f'Timed out after {lock_wait_seconds}s waiting for this Facebook cookie session '
            'to finish another local posting job.'
        )
    logger.info(f'Cookie session lock acquired locally for {label}')
    guard = ('local', local_lock, lock_key)
    await _ensure_cookie_session_can_run(cookies_json, label, guard, enforce_min_interval=enforce_min_interval)
    return guard


async def _release_cookie_session_guard(guard: Optional[Tuple[Any, ...]]) -> None:
    if guard is None:
        return
    kind, resource, lock_key = guard[:3]
    heartbeat = guard[3] if len(guard) > 3 else None
    try:
        if kind == 'redis':
            await _release_redis_lock_with_metadata(resource, heartbeat, _redis_client())
        elif kind == 'postgres':
            await asyncio.to_thread(_release_postgres_account_lock, resource, lock_key)
        elif kind == 'local':
            resource.release()
        logger.info(f'Cookie session lock released: kind={kind}')
    except Exception as exc:
        logger.warning(f'Failed to release cookie session lock: {exc}')


async def _ensure_cookie_session_can_run(
    cookies_json: str,
    label: str,
    guard: Optional[Tuple[Any, ...]],
    enforce_min_interval: bool = True,
) -> None:
    if not POST_COOKIE_SESSION_TRACKING_ENABLED:
        return
    can_use, reason = await asyncio.to_thread(
        session_manager.can_use_session,
        cookies_json,
        min_interval_seconds=POST_COOKIE_MIN_INTERVAL_SECONDS if enforce_min_interval else 0,
        security_cooldown_seconds=POST_COOKIE_SECURITY_COOLDOWN_SECONDS,
    )
    if can_use:
        return
    await _release_cookie_session_guard(guard)
    raise RuntimeError(f'Facebook cookie session is cooling down for {label}: {reason}')


def _mark_cookie_session_used(cookies_json: str, success: bool, detail: str = '') -> None:
    if not POST_COOKIE_SESSION_TRACKING_ENABLED:
        return
    session_manager.mark_session_used(cookies_json, success, detail)


def _session_result_detail(results: List[Dict[str, Any]]) -> str:
    failed = [item for item in results if not item.get('success')]
    if not failed:
        return ''
    return '; '.join(
        f"{str(item.get('page') or 'Unknown page')}: {str(item.get('result') or '')[:160]}"
        for item in failed[:3]
    )


def _normalize_same_site(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    key = re.sub(r'[\s-]+', '_', text).lower()
    normalized = _SAMESITE_ALIASES.get(key)
    return normalized or None


def _coerce_cookie_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {'1', 'true', 'yes', 'y'}
    return bool(value)


def _coerce_cookie_expires(value: Any) -> Optional[float]:
    if value in (None, ''):
        return None
    try:
        expires = float(value)
    except (TypeError, ValueError):
        return None
    if expires <= 0:
        return None
    return expires


def normalize_facebook_cookies(cookies: Any) -> List[Dict[str, Any]]:
    """Return cookies in the strict shape accepted by Playwright."""
    if not isinstance(cookies, list):
        raise ValueError('Cookies must be a JSON array.')

    normalized: List[Dict[str, Any]] = []
    for item in cookies:
        if not isinstance(item, dict):
            continue

        name = str(item.get('name') or '').strip()
        if not name:
            continue

        cookie: Dict[str, Any] = {
            'name': name,
            'value': str(item.get('value') or ''),
            'domain': str(item.get('domain') or '.facebook.com'),
            'path': str(item.get('path') or '/'),
        }

        expires = _coerce_cookie_expires(item.get('expires', item.get('expirationDate')))
        if expires is not None:
            cookie['expires'] = expires

        for bool_key in ('httpOnly', 'secure'):
            if bool_key in item and item[bool_key] is not None:
                cookie[bool_key] = _coerce_cookie_bool(item[bool_key])

        same_site = _normalize_same_site(item.get('sameSite'))
        if same_site:
            cookie['sameSite'] = same_site

        normalized.append({key: value for key, value in cookie.items() if key in _PLAYWRIGHT_COOKIE_KEYS})

    if not normalized:
        raise ValueError('No valid cookies found.')
    return normalized


def _clean_facebook_account_name(candidate: str) -> str:
    cleaned = ' '.join((candidate or '').split())
    # Strip notification badge prefix, e.g., (9) Facebook or (10) Mohammed Ali
    cleaned = re.sub(r'^\(\d+\)\s*', '', cleaned)
    # Strip standard Facebook suffixes
    cleaned = re.sub(r'\s*[\-|\|]\s*Facebook\s*$', '', cleaned, flags=re.I).strip()
    return cleaned[:80]


def _normalize_account_name_candidate(candidate: str) -> str:
    return _clean_facebook_account_name(candidate).casefold()


def _account_name_candidates_agree(left: str, right: str) -> bool:
    left_norm = _normalize_account_name_candidate(left)
    right_norm = _normalize_account_name_candidate(right)
    if not left_norm or not right_norm:
        return False
    return left_norm == right_norm or left_norm in right_norm or right_norm in left_norm


def _is_good_account_name(candidate: str) -> bool:
    cleaned = _clean_facebook_account_name(candidate)
    if not cleaned:
        return False
    lowered = cleaned.lower()
    rejected = {
        'facebook',
        'log in',
        'login',
        'manage page',
        'pages',
        'home',
        'watch',
        'notifications',
        'chats',
        'chat',
        'messenger',
        'friends',
        'groups',
        'marketplace',
        'gaming',
        'saved',
        'memories',
        'events',
        'feeds',
        'news feed',
    }
    if lowered in rejected:
        return False
    if lowered.startswith(('log in ', 'manage page', 'facebook -')):
        return False
    return True


async def _page_looks_logged_out(page: Page) -> bool:
    """Detect logged-out shells that still render public Facebook content."""
    try:
        if _is_logged_out_url(page.url):
            return True
        return bool(
            await page.locator(
                "input[name='email'], input[name='pass'], "
                "a[href*='/login'], a[href*='/reg'], "
                "a[aria-label='Log In'], a[aria-label='Create new account']"
            ).evaluate_all(
                """
                elements => elements.some(element => {
                    const rect = element.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                })
                """
            )
        )
    except Exception:
        return False


async def _facebook_security_block_detail(page: Page) -> str:
    """Return a user-actionable reason when Facebook blocks the session."""
    try:
        current_url = page.url.lower()
    except Exception:
        current_url = ''
    if any(marker in current_url for marker in ('checkpoint', '/nt/screen', 'recover/initiate', 'login_identify')):
        return (
            'Facebook security checkpoint or account lock detected. '
            'Stop automation and unlock the account manually from a trusted device, then re-add fresh cookies.'
        )
    try:
        text = await page.locator('body').evaluate(
            "el => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 3000)"
        )
    except Exception:
        text = ''
    if _FACEBOOK_SECURITY_TEXT_RE.search(text):
        return (
            'Facebook locked or checkpointed this account. '
            'Use a previously trusted device/browser to unlock it manually, then re-add fresh cookies.'
        )
    return ''


async def _facebook_navigation_fatal_block_detail(page: Page) -> str:
    """Fast post-navigation check before waiting for composer controls."""
    try:
        current_url = page.url.lower()
    except Exception:
        current_url = ''
    if any(marker in current_url for marker in ('checkpoint', '/nt/screen', 'recover/initiate', 'login_identify')):
        return (
            'Facebook security checkpoint or account lock detected. '
            'Stop automation and unlock the account manually from a trusted device, then re-add fresh cookies.'
        )
    if _is_logged_out_url(current_url):
        return 'Cookies expired or invalid. Login page detected.'

    try:
        body_text = await page.locator('body').inner_text(timeout=POST_NAVIGATION_FATAL_CHECK_TIMEOUT_MS)
    except Exception:
        body_text = ''
    compact_text = re.sub(r'\s+', ' ', body_text or '')[:4000]
    if _FACEBOOK_SECURITY_TEXT_RE.search(compact_text):
        return (
            'Facebook locked or checkpointed this account. '
            'Use a previously trusted device/browser to unlock it manually, then re-add fresh cookies.'
        )
    if re.search(r'\b(log in|login|forgot password|create new account)\b|تسجيل الدخول|إنشاء حساب', compact_text, re.I):
        if await _page_looks_logged_out(page):
            return 'Cookies expired or invalid. Login page detected.'
    return ''


async def _publish_click_visible_failure_detail(page: Page) -> str:
    fatal_detail = await _facebook_navigation_fatal_block_detail(page)
    if fatal_detail:
        return fatal_detail
    try:
        text = await page.locator('[role="alert"], [aria-live], div[role="dialog"]').evaluate_all(
            """
            elements => elements
                .filter(element => {
                    const rect = element.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                })
                .map(element => (element.innerText || element.textContent || '').replace(/\\s+/g, ' ').trim())
                .join('\\n')
                .slice(0, 6000)
            """
        )
    except Exception:
        text = ''
    if re.search(
        r'couldn.?t post|couldn.?t upload|upload failed|try again|something went wrong|'
        r'not published|failed to publish|تعذر|فشل|خطأ',
        text,
        re.I,
    ):
        return 'Facebook showed a posting or upload error after the publish click'
    return ''


async def _resume_facebook_cookie_session(page: Page) -> None:
    """Handle Facebook's remembered-profile interstitial after cookie injection."""
    try:
        clicked = await _click_first_available(
            page,
            [
                lambda: page.get_by_role('button', name=re.compile(r'^Continue\b|متابعة', re.I)),
                "div[role='button']:has-text('Continue')",
                "button:has-text('Continue')",
            ],
            timeout=3000,
        )
        if clicked:
            await _wait_for_facebook_ui_ready(page, timeout=8000)
    except Exception:
        pass


async def _read_account_name_candidates(page: Page) -> List[str]:
    candidates: List[str] = []
    try:
        title = await page.title()
        if title:
            candidates.append(title)
    except Exception:
        pass

    selectors = (
        "meta[property='og:title']",
        "[role='main'] h1",
        "div[role='button'][aria-label*='profile' i]",
        "div[role='button'][aria-label*='Profile' i]",
        "h1",
        "a[aria-label][href*='profile.php']",
        "a[aria-label][href*='/me']",
    )
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if selector.startswith('meta'):
                text = await locator.get_attribute('content', timeout=3000)
            else:
                text = await locator.inner_text(timeout=3000)
                if not text:
                    text = await locator.get_attribute('aria-label', timeout=1000)
            if text:
                # Clean up header account button prefixes to isolate exact profile name
                for prefix in ("Your profile, ", "Profile picture of ", "ملفك الشخصي، ", "صورة الملف الشخصي لـ "):
                    if prefix in text:
                        text = text.replace(prefix, "")
                candidates.append(text.strip())
        except Exception:
            continue
    return candidates


def _is_missing_browser_error(exc: Exception) -> bool:
    text = str(exc)
    return 'Executable doesn' in text and 'playwright install' in text


async def _run_blocking(func: Any, *args: Any) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args))


async def _install_playwright_browser_once() -> bool:
    global _PLAYWRIGHT_BROWSER_INSTALL_ATTEMPTED
    if _PLAYWRIGHT_BROWSER_INSTALL_ATTEMPTED:
        return False
    _PLAYWRIGHT_BROWSER_INSTALL_ATTEMPTED = True

    def run_install(browsers_path: str) -> Tuple[subprocess.CompletedProcess, str]:
        env = os.environ.copy()
        env['PLAYWRIGHT_BROWSERS_PATH'] = browsers_path
        result = subprocess.run(
            [sys.executable, '-m', 'playwright', 'install', 'chromium'],
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        return result, browsers_path

    browser_path = os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '/ms-playwright')
    logger.warning(
        f'Playwright Chromium executable missing at runtime; attempting emergency install. '
        f'Primary path: {browser_path}. '
        'This indicates the deployment image was built without the expected browser.'
    )

    # Try primary browser path first
    try:
        result, used_path = await _run_blocking(run_install, browser_path)
        if result.returncode == 0:
            logger.info(f'✓ Playwright Chromium runtime install completed at {used_path}')
            os.environ['PLAYWRIGHT_BROWSERS_PATH'] = used_path
            return True

        # Check if it's a permission denied error
        error_output = result.stderr + result.stdout
        if 'EACCES' in error_output or 'permission denied' in error_output.lower():
            logger.warning(
                f'✗ Permission denied at {browser_path}. Attempting fallback to /tmp...'
            )
            # Try fallback temporary directory
            fallback_path = '/tmp/playwright_browsers'
            try:
                os.makedirs(fallback_path, mode=0o777, exist_ok=True)
                logger.info(f'Created fallback browser cache at {fallback_path}')
                result, used_path = await _run_blocking(run_install, fallback_path)
                if result.returncode == 0:
                    logger.info(f'✓ Playwright Chromium installed at fallback path: {used_path}')
                    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = used_path
                    return True
            except Exception as fallback_exc:
                logger.error(f'Fallback installation also failed: {fallback_exc}')

        logger.error(
            'Playwright Chromium runtime install failed: '
            f'stdout={result.stdout[-500:]} stderr={result.stderr[-500:]}'
        )
        return False
    except Exception as exc:
        logger.error(f'Playwright Chromium runtime install crashed: {exc}')
        return False


def _download_media_to_path(url: str, path: str) -> None:
    downloaded = 0
    request = urllib_request.Request(
        url,
        headers={'User-Agent': 'Mozilla/5.0 (compatible; FBAutomationBot/1.0)'},
    )
    with urllib_request.urlopen(request, timeout=MEDIA_DOWNLOAD_TIMEOUT) as response:
        with open(path, 'wb') as target:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > MAX_MEDIA_BYTES:
                    raise ValueError(f'Media exceeds {MAX_MEDIA_BYTES // (1024 * 1024)} MB limit')
                target.write(chunk)


def _media_source_to_path(media_url: str, path: str) -> None:
    source = (media_url or '').strip()
    if not source:
        raise ValueError('Media URL/path is empty')
    parsed = urlparse(source)
    if parsed.scheme in ('http', 'https'):
        _download_media_to_path(source, path)
        return
    if parsed.scheme == 'file':
        source_path = Path(unquote(parsed.path))
    else:
        source_path = Path(source).expanduser()
    if not source_path.is_file():
        raise ValueError(f'Media file not found: {source}')
    if source_path.stat().st_size > MAX_MEDIA_BYTES:
        raise ValueError(f'Media exceeds {MAX_MEDIA_BYTES // (1024 * 1024)} MB limit')
    shutil.copyfile(str(source_path), path)


def _local_media_source_path(media_url: str) -> Optional[str]:
    source = (media_url or '').strip()
    if not source:
        return None
    parsed = urlparse(source)
    if parsed.scheme in ('http', 'https'):
        return None
    if parsed.scheme == 'file':
        source_path = Path(unquote(parsed.path))
    else:
        source_path = Path(source).expanduser()
    if not source_path.is_file():
        return None
    if source_path.stat().st_size > MAX_MEDIA_BYTES:
        raise ValueError(f'Media exceeds {MAX_MEDIA_BYTES // (1024 * 1024)} MB limit')
    return str(source_path)


_FACEBOOK_IMAGE_FORMAT_INFO: Dict[str, Tuple[str, str]] = {
    'jpeg': ('.jpg', 'image/jpeg'),
    'png': ('.png', 'image/png'),
    'gif': ('.gif', 'image/gif'),
    'webp': ('.webp', 'image/webp'),
    'tiff': ('.tif', 'image/tiff'),
    'heif': ('.heic', 'image/heif'),
}
_FACEBOOK_IMAGE_SUFFIXES: Dict[str, Set[str]] = {
    'jpeg': {'.jpg', '.jpeg'},
    'png': {'.png'},
    'gif': {'.gif'},
    'webp': {'.webp'},
    'tiff': {'.tif', '.tiff'},
    'heif': {'.heic', '.heif', '.heic'},
}
_VIDEO_MIME_BY_SUFFIX: Dict[str, str] = {
    '.mp4': 'video/mp4',
    '.m4v': 'video/mp4',
    '.mov': 'video/quicktime',
    '.webm': 'video/webm',
    '.avi': 'video/x-msvideo',
    '.mkv': 'video/x-matroska',
}


def _sniff_image_format(path: str) -> str:
    try:
        with open(path, 'rb') as source:
            header = source.read(64)
    except Exception:
        return ''
    if header.startswith(b'\xff\xd8\xff'):
        return 'jpeg'
    if header.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if header.startswith((b'GIF87a', b'GIF89a')):
        return 'gif'
    if len(header) >= 12 and header[:4] == b'RIFF' and header[8:12] == b'WEBP':
        return 'webp'
    if header.startswith((b'II*\x00', b'MM\x00*')):
        return 'tiff'
    if len(header) >= 12 and header[4:8] == b'ftyp':
        brand = header[8:12].lower()
        if brand in {b'heic', b'heix', b'hevc', b'hevx', b'mif1', b'msf1'}:
            return 'heif'
        if brand == b'avif':
            return 'avif'
    if header.startswith(b'BM'):
        return 'bmp'
    return ''


def _facebook_image_upload_info(format_name: str) -> Optional[Tuple[str, str]]:
    return _FACEBOOK_IMAGE_FORMAT_INFO.get((format_name or '').lower())


def _safe_upload_stem(path: str) -> str:
    stem = Path(path).stem or 'upload'
    stem = re.sub(r'[^A-Za-z0-9._-]+', '_', stem).strip('._-')
    return stem or 'upload'


def _upload_file_digest(path: str) -> str:
    digest = hashlib.sha1()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()[:12]


def _upload_display_name(path: str, post_type: str, suffix: str, digest: Optional[str] = None) -> str:
    if not digest:
        try:
            digest = _upload_file_digest(path)
        except Exception:
            digest = hashlib.sha1(str(path).encode('utf-8', errors='ignore')).hexdigest()[:12]
    media_type = 'video' if post_type == 'video' else 'image'
    return f'facebook_{media_type}_{digest}{suffix}'


def _stable_browser_upload_path(source_path: str, post_type: str) -> Tuple[str, bool]:
    suffix = Path(source_path).suffix.lower()
    if post_type == 'image':
        format_name = _sniff_image_format(source_path)
        info = _facebook_image_upload_info(format_name)
        if info:
            suffix = info[0]
    elif suffix not in _VIDEO_MIME_BY_SUFFIX:
        suffix = _media_file_suffix(post_type, source_path)

    target_path = Path(_media_upload_staging_dir()) / _upload_display_name(source_path, post_type, suffix)
    try:
        if Path(source_path).resolve() == target_path.resolve():
            return str(target_path), False
    except Exception:
        pass
    try:
        if target_path.exists() and target_path.stat().st_size == Path(source_path).stat().st_size:
            return str(target_path), False
    except Exception:
        pass
    shutil.copyfile(source_path, str(target_path))
    return str(target_path), True


def _staged_copy_with_suffix(source_path: str, suffix: str) -> str:
    target_path = _new_staged_media_path('image', source_path, suffix=suffix)
    try:
        shutil.copyfile(source_path, target_path)
        return target_path
    except Exception:
        if os.path.exists(target_path):
            try:
                os.unlink(target_path)
            except Exception:
                pass
        raise


def _load_pillow_modules() -> Tuple[Any, Any]:
    try:
        image_module = importlib.import_module('PIL.Image')
        image_ops_module = importlib.import_module('PIL.ImageOps')
        return image_module, image_ops_module
    except Exception as exc:
        raise ValueError(
            'Pillow is required to validate/re-encode images rejected by Facebook. '
            'Install Pillow>=10.0.0 in the active Python environment.'
        ) from exc


def _image_has_alpha(image: Any) -> bool:
    try:
        if image.mode in {'RGBA', 'LA'}:
            return True
        return image.mode == 'P' and 'transparency' in image.info
    except Exception:
        return False


def _resize_image_for_facebook(image: Any) -> Any:
    try:
        width, height = image.size
        pixel_count = int(width) * int(height)
        if pixel_count <= FACEBOOK_IMAGE_REENCODE_MAX_PIXELS:
            return image
        scale = (FACEBOOK_IMAGE_REENCODE_MAX_PIXELS / float(pixel_count)) ** 0.5
        target_size = (max(1, int(width * scale)), max(1, int(height * scale)))
        try:
            Image, _ImageOps = _load_pillow_modules()
            resampling = getattr(Image, 'Resampling', Image)
            resample = getattr(resampling, 'LANCZOS', 1)
        except Exception:
            resample = 1
        return image.resize(target_size, resample)
    except Exception:
        return image


def _facebook_jpeg_structure_error(data: bytes) -> str:
    if len(data) < 4:
        return 'File too small'
    if data[:2] != b'\xff\xd8':
        return 'Missing SOI marker'
    if data[-2:] != b'\xff\xd9':
        return 'Missing EOI marker'

    has_sof = False
    has_sos = False
    has_dqt = False
    pos = 2
    while pos < len(data) - 1:
        if data[pos] != 0xFF:
            pos += 1
            continue
        while pos < len(data) - 1 and data[pos + 1] == 0xFF:
            pos += 1
        marker = data[pos + 1]
        if marker == 0x00:
            pos += 2
            continue
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if marker == 0xDB:
            has_dqt = True
        elif marker in {0xC0, 0xC1, 0xC3, 0xC9, 0xCB}:
            has_sof = True
        elif marker in {0xC2, 0xCA}:
            return f'Progressive JPEG marker FF{marker:02X} detected'
        elif marker == 0xDA:
            has_sos = True
            break

        if pos + 3 >= len(data):
            return f'Truncated marker FF{marker:02X}'
        segment_length = struct.unpack('>H', data[pos + 2:pos + 4])[0]
        if segment_length < 2:
            return f'Invalid segment length at marker FF{marker:02X}'
        pos += 2 + segment_length

    if not has_sof:
        return 'Missing baseline SOF marker'
    if not has_sos:
        return 'Missing SOS marker'
    if not has_dqt:
        return 'Missing DQT marker'
    return ''


def _validate_facebook_jpeg_bytes(data: bytes) -> None:
    structure_error = _facebook_jpeg_structure_error(data)
    if structure_error:
        raise ValueError(f'JPEG validation failed: {structure_error}')
    try:
        Image, _ImageOps = _load_pillow_modules()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if image.width <= 0 or image.height <= 0:
                raise ValueError('decoded image has invalid dimensions')
            image.getpixel((0, 0))
    except Exception as exc:
        raise ValueError(f'JPEG decode validation failed: {exc}') from exc


def _facebook_sanitized_jpeg_bytes(source_path: str, *, quality: Optional[int] = None) -> Tuple[bytes, Dict[str, Any]]:
    Image, ImageOps = _load_pillow_modules()
    configured_quality = int(quality if quality is not None else FACEBOOK_IMAGE_REENCODE_QUALITY)
    configured_quality = min(95, max(60, configured_quality))
    mutations: List[str] = []
    original_format = ''
    original_mode = ''
    color_space = ''

    with Image.open(source_path) as original:
        original_format = str(original.format or 'UNKNOWN')
        original_mode = str(original.mode or '')
        image = ImageOps.exif_transpose(original)
        color_space = str(image.mode or original_mode)
        image = _resize_image_for_facebook(image)
        if image.width > 4096 or image.height > 4096:
            resampling = getattr(getattr(Image, 'Resampling', Image), 'LANCZOS', 1)
            image.thumbnail((4096, 4096), resampling)
            mutations.append('resized_to_max_4096')
        if image.width < 50 or image.height < 50:
            scale = max(50 / max(1, image.width), 50 / max(1, image.height))
            resampling = getattr(getattr(Image, 'Resampling', Image), 'LANCZOS', 1)
            image = image.resize((max(50, int(image.width * scale)), max(50, int(image.height * scale))), resampling)
            mutations.append('upscaled_to_min_50')
        if image.mode == 'CMYK':
            image = image.convert('RGB')
            mutations.append('cmyk_to_rgb')
        elif image.mode in {'LAB', 'YCbCr', 'I', 'F'}:
            image = image.convert('RGB')
            mutations.append(f'{original_mode.lower()}_to_rgb')
        elif image.mode in {'RGBA', 'LA'} or (image.mode == 'P' and 'transparency' in image.info):
            rgba = image.convert('RGBA')
            flattened = cast(Any, Image).new('RGB', rgba.size, (255, 255, 255))
            flattened.paste(rgba, mask=rgba.getchannel('A'))
            image = flattened
            mutations.append('alpha_flattened_on_white')
        elif image.mode != 'RGB':
            image = image.convert('RGB')
            mutations.append(f'{original_mode.lower()}_to_rgb')
        else:
            image = image.copy()

    def encode(current_quality: int, *, optimize: bool = False) -> bytes:
        buffer = io.BytesIO()
        image.save(
            buffer,
            format='JPEG',
            quality=current_quality,
            optimize=optimize,
            progressive=False,
            subsampling='4:2:0',
        )
        return buffer.getvalue()

    data = b''
    final_quality = configured_quality
    for candidate_quality in (configured_quality, 88, 82, 76, 70, 64, 60):
        final_quality = min(candidate_quality, configured_quality)
        data = encode(final_quality, optimize=False)
        if len(data) <= min(FACEBOOK_IMAGE_MAX_BYTES, 9 * 1024 * 1024):
            break
    else:
        data = encode(60, optimize=False)
        final_quality = 60

    if len(data) > FACEBOOK_IMAGE_MAX_BYTES:
        raise ValueError(f'Sanitized JPEG exceeds Facebook limit: {len(data)} bytes')
    _validate_facebook_jpeg_bytes(data)
    return data, {
        'source_format': original_format,
        'source_mode': original_mode,
        'color_space': color_space,
        'output_format': 'JPEG',
        'quality': final_quality,
        'size_bytes': len(data),
        'mutations': mutations,
    }


def _save_facebook_sanitized_jpeg_copy(source_path: str) -> str:
    target_path = _new_staged_media_path('image', source_path, suffix='.jpg')
    try:
        data, metadata = _facebook_sanitized_jpeg_bytes(source_path)
        Path(target_path).write_bytes(data)
        logger.info(
            'Facebook image sanitizer: '
            f'{metadata.get("source_format")}:{metadata.get("source_mode")} -> JPEG '
            f'{metadata.get("size_bytes")} bytes quality={metadata.get("quality")} '
            f'mutations={metadata.get("mutations")}'
        )
        return target_path
    except Exception:
        if os.path.exists(target_path):
            try:
                os.unlink(target_path)
            except Exception:
                pass
        raise


def _save_image_under_facebook_limit(image: Any, target_path: str, *, preserve_alpha: bool) -> bool:
    if preserve_alpha:
        try:
            png_image = image.convert('RGBA') if image.mode != 'RGBA' else image
            png_image.save(target_path, format='PNG', optimize=True)
            if os.path.exists(target_path) and os.path.getsize(target_path) <= FACEBOOK_IMAGE_MAX_BYTES:
                return True
        except Exception as exc:
            logger.debug(f'Could not save transparent PNG for Facebook upload: {exc}')

    try:
        if image.mode in {'RGBA', 'LA'}:
            background = image.convert('RGBA')
            Image, _ImageOps = _load_pillow_modules()
            flattened = cast(Any, Image).new('RGB', background.size, (255, 255, 255))
            flattened.paste(background, mask=background.getchannel('A'))
            image = flattened
        elif image.mode != 'RGB':
            image = image.convert('RGB')
        quality = min(95, max(60, FACEBOOK_IMAGE_REENCODE_QUALITY))
        for current_quality in (quality, 88, 82, 76, 70, 64):
            image.save(
                target_path,
                format='JPEG',
                quality=min(current_quality, quality),
                optimize=True,
                progressive=False,
            )
            if os.path.exists(target_path) and os.path.getsize(target_path) <= FACEBOOK_IMAGE_MAX_BYTES:
                return True
    except Exception as exc:
        logger.debug(f'Could not save JPEG for Facebook upload: {exc}')
    return os.path.exists(target_path) and os.path.getsize(target_path) > 0


def _save_facebook_jpeg_copy(source_path: str) -> str:
    if FACEBOOK_IMAGE_FORCE_SANITIZE_UPLOAD:
        return _save_facebook_sanitized_jpeg_copy(source_path)

    Image, ImageOps = _load_pillow_modules()
    target_path = _new_staged_media_path('image', source_path, suffix='.jpg')
    try:
        with Image.open(source_path) as image:
            image = ImageOps.exif_transpose(image)
            image = _resize_image_for_facebook(image)
            if not _save_image_under_facebook_limit(image, target_path, preserve_alpha=False):
                raise ValueError('Could not re-encode image as JPEG for Facebook upload.')
            if os.path.getsize(target_path) > FACEBOOK_IMAGE_MAX_BYTES:
                raise ValueError(
                    f'JPEG image remains larger than Facebook limit after re-encoding: '
                    f'{os.path.getsize(target_path)} bytes'
                )
            return target_path
    except Exception:
        if os.path.exists(target_path):
            try:
                os.unlink(target_path)
            except Exception:
                pass
        raise


def _save_facebook_png_copy(source_path: str) -> str:
    Image, ImageOps = _load_pillow_modules()
    target_path = _new_staged_media_path('image', source_path, suffix='.png')
    try:
        with Image.open(source_path) as image:
            image = ImageOps.exif_transpose(image)
            image = _resize_image_for_facebook(image)
            if image.mode not in {'RGB', 'RGBA'}:
                image = image.convert('RGBA' if _image_has_alpha(image) else 'RGB')
            image.save(target_path, format='PNG', optimize=True)
            if os.path.getsize(target_path) > FACEBOOK_IMAGE_MAX_BYTES:
                if not _save_image_under_facebook_limit(image, target_path, preserve_alpha=False):
                    raise ValueError('Could not shrink PNG retry image under Facebook upload limit.')
            if os.path.getsize(target_path) > FACEBOOK_IMAGE_MAX_BYTES:
                raise ValueError(
                    f'PNG image remains larger than Facebook limit after re-encoding: '
                    f'{os.path.getsize(target_path)} bytes'
                )
            return target_path
    except Exception:
        if os.path.exists(target_path):
            try:
                os.unlink(target_path)
            except Exception:
                pass
        raise


def _ensure_facebook_safe_image(source_path: str, *, prefer_jpeg: Optional[bool] = None) -> Tuple[str, bool]:
    if not os.path.exists(source_path):
        raise ValueError(f'Image file not found before upload: {Path(source_path).name}')
    if os.path.getsize(source_path) <= 0:
        raise ValueError(f'Image file is empty before upload: {Path(source_path).name}')

    sniffed_format = _sniff_image_format(source_path)
    suffix = Path(source_path).suffix.lower()
    upload_info = _facebook_image_upload_info(sniffed_format)
    if FACEBOOK_IMAGE_FORCE_SANITIZE_UPLOAD and sniffed_format != 'gif':
        return _save_facebook_sanitized_jpeg_copy(source_path), True

    should_prefer_jpeg = FACEBOOK_IMAGE_PREFER_JPEG_UPLOAD if prefer_jpeg is None else prefer_jpeg
    if should_prefer_jpeg and sniffed_format and sniffed_format not in {'jpeg', 'gif'}:
        try:
            return _save_facebook_jpeg_copy(source_path), True
        except Exception as exc:
            logger.warning(f'Could not create preferred JPEG upload copy; falling back to safe image validation: {exc}')
    if (
        upload_info
        and os.path.getsize(source_path) <= FACEBOOK_IMAGE_MAX_BYTES
        and suffix in _FACEBOOK_IMAGE_SUFFIXES.get(sniffed_format, set())
    ):
        return source_path, False
    if upload_info and os.path.getsize(source_path) <= FACEBOOK_IMAGE_MAX_BYTES:
        return _staged_copy_with_suffix(source_path, upload_info[0]), True

    try:
        Image, ImageOps = _load_pillow_modules()
        with Image.open(source_path) as image:
            image = ImageOps.exif_transpose(image)
            image = _resize_image_for_facebook(image)
            preserve_alpha = _image_has_alpha(image)
            suffix = '.png' if preserve_alpha else '.jpg'
            target_path = _new_staged_media_path('image', source_path, suffix=suffix)
            try:
                if not _save_image_under_facebook_limit(image, target_path, preserve_alpha=preserve_alpha):
                    raise ValueError('Could not re-encode image into a Facebook-safe file.')
                if os.path.getsize(target_path) > FACEBOOK_IMAGE_MAX_BYTES:
                    raise ValueError(
                        f'Image remains larger than Facebook limit after re-encoding: '
                        f'{os.path.getsize(target_path)} bytes'
                    )
                return target_path, True
            except Exception:
                if os.path.exists(target_path):
                    try:
                        os.unlink(target_path)
                    except Exception:
                        pass
                raise
    except Exception as exc:
        if sniffed_format and sniffed_format not in _FACEBOOK_IMAGE_FORMAT_INFO:
            raise ValueError(f'Unsupported image format for Facebook upload: {sniffed_format}') from exc
        raise


def _facebook_jpeg_upload_variant(source_path: str) -> Optional[str]:
    if not FACEBOOK_IMAGE_RETRY_JPEG_ON_REJECTION:
        return None
    if _sniff_image_format(source_path) == 'jpeg' and Path(source_path).suffix.lower() in {'.jpg', '.jpeg'}:
        return None
    try:
        return _save_facebook_jpeg_copy(source_path)
    except Exception as exc:
        logger.warning(f'Could not create JPEG retry upload variant: {exc}')
        return None


def _facebook_png_upload_variant(source_path: str) -> Optional[str]:
    try:
        return _save_facebook_png_copy(source_path)
    except Exception as exc:
        logger.warning(f'Could not create PNG retry upload variant: {exc}')
        return None


def _playwright_upload_file_payload(path: str, post_type: str) -> Dict[str, Any]:
    suffix = Path(path).suffix.lower()
    if post_type == 'image':
        format_name = _sniff_image_format(path)
        info = _facebook_image_upload_info(format_name)
        if not info:
            raise ValueError(f'Unsupported image format for Facebook upload: {format_name or suffix or "unknown"}')
        expected_suffix, mime_type = info
        if suffix not in _FACEBOOK_IMAGE_SUFFIXES.get(format_name, set()):
            suffix = expected_suffix
    else:
        mime_type = _VIDEO_MIME_BY_SUFFIX.get(suffix, 'application/octet-stream')
        if suffix not in _VIDEO_MIME_BY_SUFFIX:
            suffix = _media_file_suffix(post_type, path)
    buffer = Path(path).read_bytes()
    digest = hashlib.sha1(buffer).hexdigest()[:12]
    return {
        'name': _upload_display_name(path, post_type, suffix, digest=digest),
        'mimeType': mime_type,
        'buffer': buffer,
    }


def _media_upload_staging_dir() -> str:
    configured = os.getenv('FACEBOOK_UPLOAD_STAGING_DIR', '').strip()
    candidates: List[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    snap_common = Path.home() / 'snap' / 'chromium' / 'common'
    if snap_common.exists() and not FACEBOOK_SKIP_SNAP_CHROMIUM:
        candidates.append(snap_common / 'FacebookUploadStaging')
    candidates.append(Path(tempfile.gettempdir()) / 'fb_automation_uploads')

    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return str(candidate)
        except Exception as exc:
            logger.debug(f'Could not use upload staging directory {candidate}: {exc}')
    return tempfile.gettempdir()


def _new_staged_media_path(post_type: str, media_url: str = '', *, suffix: Optional[str] = None) -> str:
    with tempfile.NamedTemporaryFile(
        delete=False,
        suffix=suffix or _media_file_suffix(post_type, media_url),
        dir=_media_upload_staging_dir(),
    ) as tmp:
        return tmp.name


def _normalize_image_upload_source(source_path: str, target_path: str) -> bool:
    try:
        Image, ImageOps = _load_pillow_modules()
    except Exception:
        return False
    try:
        with Image.open(source_path) as image:
            image = ImageOps.exif_transpose(image)
            if image.mode != 'RGB':
                image = image.convert('RGB')
            image.save(target_path, format='JPEG', quality=92, optimize=True)
        return os.path.exists(target_path) and os.path.getsize(target_path) > 0
    except Exception as exc:
        logger.debug(f'Could not normalize image for Facebook upload: {exc}')
        return False


def _stage_media_for_browser_upload(post_type: str, media_url: str) -> Tuple[str, bool]:
    source = (media_url or '').strip()
    if not source:
        raise ValueError('Media URL/path is empty')

    local_source = _local_media_source_path(source)
    source_for_copy = local_source or source
    if post_type == 'image':
        raw_suffix = _media_file_suffix(post_type, source)
        target_path = _new_staged_media_path(post_type, source, suffix=raw_suffix)
        try:
            _media_source_to_path(source_for_copy, target_path)
            safe_path, safe_created = _ensure_facebook_safe_image(target_path)
            if safe_path != target_path:
                try:
                    os.unlink(target_path)
                except Exception:
                    pass
                return safe_path, safe_created
            return target_path, True
        except Exception:
            if os.path.exists(target_path):
                try:
                    os.unlink(target_path)
                except Exception:
                    pass
            raise

    target_path = _new_staged_media_path(post_type, source)
    try:
        _media_source_to_path(source_for_copy, target_path)
        return target_path, True
    except Exception:
        if os.path.exists(target_path):
            try:
                os.unlink(target_path)
            except Exception:
                pass
        raise


def _facebook_context_options() -> Dict[str, Any]:
    context_options: Dict[str, Any] = {
        'user_agent': FACEBOOK_BROWSER_USER_AGENT,
        'locale': FACEBOOK_BROWSER_LOCALE,
        'timezone_id': FACEBOOK_BROWSER_TIMEZONE,
        'color_scheme': 'light',
        'ignore_https_errors': True,
        'java_script_enabled': True,
        'bypass_csp': False,
        'extra_http_headers': {
            'Accept-Language': FACEBOOK_BROWSER_ACCEPT_LANGUAGE,
        },
    }
    if not HEADLESS:
        context_options['no_viewport'] = True
    else:
        context_options['viewport'] = {'width': 1920, 'height': 1080}
        context_options['device_scale_factor'] = 1
    return context_options


async def _new_facebook_context(browser: Browser) -> BrowserContext:
    context_options = _facebook_context_options()
    try:
        context = await browser.new_context(**context_options)
    except Exception as exc:
        if 'timezone' not in str(exc).lower():
            raise
        logger.warning(
            f'Invalid FACEBOOK_BROWSER_TIMEZONE="{FACEBOOK_BROWSER_TIMEZONE}"; '
            'falling back to browser default timezone.'
        )
        context_options.pop('timezone_id', None)
        context = await browser.new_context(**context_options)
    await context.add_init_script(
        """
        (() => {
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true });
            for (const key of [
                '__webdriver_evaluate', '__selenium_evaluate', '__webdriver_unwrapped',
                '__driver_unwrapped', '__webdriver_script_fn', '__driver_evaluate',
                '__selenium_unwrapped', '__fxdriver_unwrapped', '__fxdriver_evaluate',
                '_Selenium_IDE_Recorder', '_selenium', 'calledSelenium',
                '__nightmare', '__phantomas', 'domAutomation', 'domAutomationController',
            ]) {
                try {
                    Object.defineProperty(window, key, { get: () => undefined, configurable: true });
                    delete window[key];
                } catch (_) {}
            }
            window.chrome = window.chrome || {};
            window.chrome.runtime = window.chrome.runtime || {
                connect: () => ({ onMessage: { addListener: () => {} } }),
                sendMessage: () => {},
                onMessage: { addListener: () => {} },
                id: undefined,
            };
        })();
        """
    )
    try:
        await context.grant_permissions(
            ['clipboard-read', 'clipboard-write'],
            origin='https://www.facebook.com',
        )
    except Exception as exc:
        logger.debug(f'Could not grant Facebook clipboard permissions: {exc}')
    return context


async def launch_browser_session(cookies_json: str) -> Tuple[Any, Browser, BrowserContext, Page]:
    """Launch a stealth Playwright browser with the given cookies."""
    # Log the browser path configuration for debugging
    browser_path = os.environ.get('PLAYWRIGHT_BROWSERS_PATH', 'default')
    logger.debug(f'Browser launch: PLAYWRIGHT_BROWSERS_PATH={browser_path}, HEADLESS={HEADLESS}')

    async def start_playwright_and_browser() -> Tuple[Any, Browser]:
        playwright = await async_playwright().start()
        launch_args = [
            '--disable-blink-features=AutomationControlled',
            '--disable-dev-shm-usage',
            '--disable-default-apps',
            '--disable-extensions',
            '--disable-component-extensions-with-background-pages',
            '--disable-hang-monitor',
            '--disable-popup-blocking',
            '--disable-prompt-on-repost',
            '--disable-sync',
            '--disable-translate',
            '--metrics-recording-only',
            '--no-first-run',
            '--no-default-browser-check',
            '--password-store=basic',
            '--use-mock-keychain',
            '--profile-directory=Default',
            '--guest',
        ]
        if FACEBOOK_BROWSER_NO_SANDBOX:
            launch_args.extend(['--no-sandbox', '--disable-setuid-sandbox'])
        if HEADLESS and FACEBOOK_UPLOAD_SAFE_BROWSER_ENABLED:
            launch_args.append('--headless=new')
        if not HEADLESS:
            launch_args.append('--start-maximized')

        # Try to find a local system Chromium or Google Chrome binary on Linux
        executable_path = None
        if FACEBOOK_BROWSER_EXECUTABLE:
            configured_path = Path(FACEBOOK_BROWSER_EXECUTABLE).expanduser()
            if configured_path.exists():
                executable_path = str(configured_path)
                logger.info(f'Using configured browser executable: {executable_path}')
            else:
                logger.warning(f'FACEBOOK_BROWSER_EXECUTABLE does not exist: {configured_path}')
        common_paths = [
            '/usr/bin/google-chrome',
            '/usr/bin/google-chrome-stable',
            '/usr/bin/chromium',
            '/usr/bin/chromium-browser',
            '/snap/bin/chromium',
            '/snap/bin/google-chrome',
            '/var/lib/flatpak/exports/bin/org.chromium.Chromium'
        ]
        for path in common_paths:
            if executable_path:
                break
            if os.path.exists(path):
                if FACEBOOK_SKIP_SNAP_CHROMIUM and path.startswith('/snap/'):
                    logger.info(f'Skipping Snap Chromium for automation file-upload compatibility: {path}')
                    continue
                executable_path = path
                logger.info(f'Found and using local system browser at: {executable_path}')
                break

        try:
            if executable_path:
                browser = await playwright.chromium.launch(
                    headless=HEADLESS,
                    args=launch_args,
                    executable_path=executable_path
                )
            else:
                logger.info('No compatible local system browser found, falling back to default Playwright Chromium.')
                browser = await playwright.chromium.launch(
                    headless=HEADLESS,
                    args=launch_args
                )
            return playwright, browser
        except Exception:
            await playwright.stop()
            raise

    try:
        playwright, browser = await start_playwright_and_browser()
    except Exception as exc:
        if _is_missing_browser_error(exc) and await _install_playwright_browser_once():
            try:
                playwright, browser = await start_playwright_and_browser()
            except Exception as retry_exc:
                raise RuntimeError(
                    'Playwright Chromium is not installed and runtime installation did not make it available. '
                    'Redeploy with `python install_playwright.py` during build.'
                ) from retry_exc
        else:
            if _is_missing_browser_error(exc):
                raise RuntimeError(
                    'Playwright Chromium is not installed. Redeploy with browser installation enabled.'
                ) from exc
            raise

    context = await _new_facebook_context(browser)

    try:
        cookies = json.loads(cookies_json)
        cookies = normalize_facebook_cookies(cookies)
        await context.add_cookies(cast(Any, cookies))
    except Exception as e:
        logger.error(f"Failed to inject cookies: {e}")
        await browser.close()
        await playwright.stop()
        raise RuntimeError('Failed to inject Facebook cookies. Re-add the account with a fresh cookie export.') from e

    page = await context.new_page()
    if FACEBOOK_STEALTH_ASYNC_ENABLED:
        await stealth_async(page)
    else:
        logger.info('Browser stealth_async skipped to preserve Facebook file-upload behavior.')
    try:
        _attach_network_monitoring(page, 'browser_session')
    except Exception as exc:
        logger.debug(f'Could not attach network monitoring: {exc}')

    return playwright, browser, context, page


def _facebook_host(host: str) -> bool:
    clean_host = host.split('@')[-1].split(':')[0].lower()
    return clean_host == 'facebook.com' or clean_host.endswith('.facebook.com') or clean_host == 'fb.com'


def _normalize_discovered_page_url(raw_url: str) -> Optional[str]:
    parsed = urlparse(raw_url)
    if parsed.scheme not in {'http', 'https'} or not _facebook_host(parsed.netloc):
        return None
    lowered_url = raw_url.lower()
    if any(
        blocked in lowered_url
        for blocked in (
            '/business/',
            '/business_suite/',
            '/professional_dashboard',
            '/ad_center/',
            '/promote',
            '/ads/',
            '/ad_campaign',
            '/careers',
            '/help/',
            '/login',
            '/reg',
            '/settings',
            '/privacy',
            '/latest/',
            '/watch/',
            '/marketplace/',
        )
    ):
        return None

    path_parts = [unquote(part) for part in parsed.path.split('/') if part]
    if not path_parts:
        return None

    first_part = path_parts[0].lower()
    if first_part == 'profile.php':
        query = parse_qs(parsed.query)
        page_id = query.get('id', [''])[0].strip()
        if page_id:
            return f'https://www.facebook.com/profile.php?id={page_id}'
        return None

    if first_part in {'pages', 'pg'}:
        if len(path_parts) >= 2 and path_parts[1].lower() not in {
            'create',
            'creation',
            'category',
            'feed',
            'liked',
            'manager',
            'your_pages',
        }:
            keep = path_parts[:3] if len(path_parts) >= 3 and path_parts[2].isdigit() else path_parts[:2]
            return urlunparse(('https', 'www.facebook.com', '/' + '/'.join(keep), '', '', ''))
        return None

    if first_part in _RESERVED_FACEBOOK_PATHS:
        return None
    if len(path_parts) >= 2 and path_parts[1].lower() in _BLOCKED_PAGE_SUBPATHS:
        return None
    if first_part.startswith('recover') or first_part.startswith('share'):
        return None

    return urlunparse(('https', 'www.facebook.com', '/' + path_parts[0], '', '', ''))


def _page_id_from_url(page_url: str) -> str:
    try:
        parsed = urlparse(page_url)
        if parsed.path == '/profile.php':
            return (parse_qs(parsed.query).get('id') or [''])[0].strip()
    except Exception:
        pass
    return ''


def _is_blocked_discovered_page_name(name: str) -> bool:
    lower_text = ' '.join((name or '').lower().split())
    if not lower_text:
        return True
    if lower_text in _BLOCKED_DISCOVERY_TEXTS:
        return True
    if lower_text.startswith(('meta business', 'professional dashboard', 'promote ', 'boost ', 'advertise ')):
        return True
    if any(
        skip in lower_text
        for skip in (
            'create new page',
            'create page',
            'pages you may like',
            'ترويج',
            'روّج',
            'إعلان',
            'اعلان',
            'الإجراءات لهذا المنشور',
            'إجراءات هذا المنشور',
        )
    ):
        return True
    return False


def _page_name_from_discovered_link(raw_text: str, page_url: str) -> str:
    text = ' '.join((raw_text or '').split())
    wrapper_patterns = (
        r'^\s*Profile picture (?:for|of)\s+(.+?)\s*$',
        r'^\s*(.+?)\s+profile picture\s*$',
        r'^\s*Photo de profil de\s+(.+?)\s*$',
        r'^\s*Foto del perfil de\s+(.+?)\s*$',
        r'^\s*Foto de perfil de\s+(.+?)\s*$',
        r'^\s*صورة\s+(?:ال)?ملف\s+(.+?)\s+الشخصي\s*$',
        r'^\s*صورة\s+(?:ال)?ملف\s+(?:الشخصي\s+)?(?:لـ|ل)\s*(.+?)\s*$',
        r'^\s*(.+?)\s+صورة\s+(?:ال)?ملف(?:ه|ها)?\s+الشخصي\s*$',
    )
    for pattern in wrapper_patterns:
        match = re.match(pattern, text, re.I)
        if match:
            text = match.group(1).strip()
            break
    text = re.sub(r'\s+(?:الشخصي|profile)$', '', text, flags=re.I).strip()
    if text and len(text) <= 80:
        return text

    parsed = urlparse(page_url)
    if parsed.path == '/profile.php':
        return ''

    parts = [unquote(part) for part in parsed.path.split('/') if part]
    if not parts:
        return ''
    candidate = parts[-1].strip()
    if candidate.lower() in {
        'ad_center',
        'ads',
        'promote',
        'business',
        'business_suite',
        'professional_dashboard',
        'your_pages',
        'pages',
        'pg',
    }:
        return ''
    return candidate


def _clean_discovered_page_name(raw_name: str) -> str:
    text = ' '.join((raw_name or '').split()).strip()
    if not text:
        return ''
    blocked_exact = {
        'ترويج',
        'روّج',
        'إعلان',
        'اعلان',
        'الإجراءات لهذا المنشور',
        'إجراءات هذا المنشور',
        'Boost',
        'Promote',
        'Advertise',
        'Ad Center',
        'Create ad',
        'Actions for this post',
        'Manage posts',
        'Post',
    }
    if text in blocked_exact:
        return ''
    text = re.sub(
        r'^(?:Profile picture for|Profile picture of|Photo de profil de|Foto del perfil de|Foto de perfil de|'
        r'صورة\s+(?:ال)?ملف\s+|صورة\s+(?:ال)?ملف\s+(?:الشخصي\s+)?(?:لـ|ل)\s*|'
        r'صفحة\s+|الصفحة\s+)\s*',
        '',
        text,
        flags=re.I,
    )
    text = re.sub(r'\s+(?:profile picture|photo de profil|foto del perfil|foto de perfil|صورة\s+(?:ال)?ملف(?:ه|ها)?\s+الشخصي)$', '', text, flags=re.I)
    text = re.sub(r'^\d+\s*-\s*', '', text).strip()
    if text.isdigit():
        return ''
    if _is_blocked_discovered_page_name(text):
        return ''
    return text


def _looks_like_numeric_identifier(text: str) -> bool:
    digits = ''.join(ch for ch in (text or '') if ch.isdigit())
    return len(digits) >= 6 and digits == ''.join(ch for ch in (text or '').strip() if ch.isdigit())


def _prefer_discovered_page_name(candidate: str, page_url: str) -> str:
    cleaned = _clean_discovered_page_name(candidate)
    if cleaned and not cleaned.isdigit() and not _looks_like_numeric_identifier(cleaned):
        return cleaned
    return ''


def _discovered_page_name_score(name: str, page_url: str) -> int:
    cleaned = _clean_discovered_page_name(name)
    if _is_blocked_discovered_page_name(cleaned):
        return -1

    parsed = urlparse(page_url)
    fallback_name = _page_name_from_discovered_link('', page_url).lower()
    lowered = cleaned.lower()
    score = 10
    if any(marker in lowered for marker in ('profile picture', 'photo de profil', 'foto del perfil', 'foto de perfil', 'صورة ملف', 'صورة الملف')):
        score += 50
    if fallback_name and lowered != fallback_name:
        score += 20
    if len(cleaned) >= 3:
        score += min(len(cleaned), 30)
    if parsed.path == '/profile.php':
        score += 10
    return score


async def _extract_page_links(page: Page) -> List[Dict[str, str]]:
    anchors = await page.locator('a[href]').evaluate_all(
        """
        anchors => anchors.map(anchor => {
            const rect = anchor.getBoundingClientRect();
            const text = (
                anchor.innerText ||
                anchor.getAttribute('aria-label') ||
                anchor.textContent ||
                ''
            ).trim();
            const imageLabel = (
                anchor.querySelector('svg[aria-label]')?.getAttribute('aria-label') ||
                anchor.querySelector('img[alt]')?.getAttribute('alt') ||
                anchor.querySelector('img[aria-label]')?.getAttribute('aria-label') ||
                ''
            ).trim();
            return {
                href: anchor.href,
                text,
                imageLabel,
                visible: rect.width > 0 && rect.height > 0,
            };
        })
        """
    )

    grouped: Dict[str, Dict[str, Any]] = {}
    for anchor in anchors:
        if not isinstance(anchor, dict) or not anchor.get('visible'):
            continue
        raw_url = str(anchor.get('href') or '')
        page_url = _normalize_discovered_page_url(raw_url)
        if not page_url:
            continue
        candidates = [
            str(anchor.get('text') or ''),
            str(anchor.get('imageLabel') or ''),
        ]
        cleaned_name = ''
        score = -1
        for raw_text in candidates:
            candidate = _prefer_discovered_page_name(_page_name_from_discovered_link(raw_text, page_url), page_url)
            candidate_score = _discovered_page_name_score(candidate, page_url)
            if candidate_score > score:
                cleaned_name = candidate
                score = candidate_score
        if score < 0:
            continue
        current = grouped.get(page_url)
        if current is None or score > int(current.get('score', -1)):
            grouped[page_url] = {
                'id': _page_id_from_url(page_url) or page_url,
                'url': page_url,
                'name': cleaned_name,
                'score': score,
            }

    pages = []
    for item in grouped.values():
        item.pop('score', None)
        pages.append(item)
    return pages


def _is_valid_discovered_page_item(item: Dict[str, str]) -> bool:
    name = _clean_discovered_page_name(str(item.get('name') or ''))
    if not name:
        return False
    if _looks_like_numeric_identifier(name):
        return False
    if _is_blocked_discovered_page_name(name):
        return False
    return True


async def _extract_page_cards(page: Page) -> List[Dict[str, str]]:
    """Prefer page-management cards when Facebook exposes them."""
    cards = await page.locator("div[role='main'] a[href*='profile.php?id='], div[role='main'] a[href^='https://www.facebook.com/profile.php?id=']").evaluate_all(
        """
        anchors => anchors.map(anchor => {
            const href = anchor.href;
            const label = (
                anchor.innerText ||
                anchor.getAttribute('aria-label') ||
                anchor.textContent ||
                ''
            ).trim();
            const ariaLabel = (anchor.getAttribute('aria-label') || '').trim();
            const parent = anchor.closest("[role='article'], [aria-label], div");
            const parentText = parent ? (parent.innerText || '').trim() : '';
            const siblingLabels = [];
            if (parent) {
                const siblingAnchors = parent.querySelectorAll("a[href]");
                siblingAnchors.forEach(sibling => {
                    if (sibling === anchor) return;
                    const siblingText = (
                        sibling.innerText ||
                        sibling.getAttribute('aria-label') ||
                        sibling.textContent ||
                        ''
                    ).trim();
                    if (siblingText) siblingLabels.push(siblingText);
                });
            }
            return { href, label, ariaLabel, parentText, siblingLabels };
        })
        """
    )

    grouped: Dict[str, Dict[str, Any]] = {}
    for card in cards:
        if not isinstance(card, dict):
            continue
        page_url = _normalize_discovered_page_url(str(card.get('href') or ''))
        if not page_url:
            continue

        labels = [
            str(card.get('label') or ''),
            str(card.get('ariaLabel') or ''),
            str(card.get('parentText') or '').splitlines()[0] if card.get('parentText') else '',
            *[str(value) for value in cast(List[Any], card.get('siblingLabels') or [])[:3]],
        ]
        for label in labels:
            cleaned_name = _prefer_discovered_page_name(_page_name_from_discovered_link(label, page_url), page_url)
            score = _discovered_page_name_score(cleaned_name, page_url)
            if score < 0:
                continue
            current = grouped.get(page_url)
            if current is None or score > int(current.get('score', -1)):
                grouped[page_url] = {
                    'id': _page_id_from_url(page_url) or page_url,
                    'url': page_url,
                    'name': cleaned_name,
                    'score': score,
                }

    pages = []
    for item in grouped.values():
        item.pop('score', None)
        if _is_valid_discovered_page_item(item):
            pages.append(item)
    return pages


def _page_item_from_graphql_candidate(candidate: Dict[str, Any]) -> Optional[Dict[str, str]]:
    if not isinstance(candidate, dict):
        return None

    name = ''
    for key in ('name', 'title', 'page_name', 'profile_name', 'text'):
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            name = value.strip()
            break

    url = ''
    for key in ('url', 'href', 'page_url', 'profile_url', 'canonical_url'):
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            url = value.strip()
            break

    raw_page_id = ''
    for key in ('page_id', 'profile_id', 'actor_id', 'id'):
        value = candidate.get(key)
        if isinstance(value, int):
            raw_page_id = str(value)
            break
        if isinstance(value, str) and value.strip().isdigit():
            raw_page_id = value.strip()
            break

    if not url:
        for key in ('uri', 'id', 'page_id', 'profile_id'):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                raw_id = value.strip()
                if raw_id.isdigit():
                    raw_page_id = raw_page_id or raw_id
                    url = f'https://www.facebook.com/profile.php?id={raw_id}'
                elif raw_id.startswith('http'):
                    url = raw_id
                break

    if not url:
        return None

    normalized_url = _normalize_discovered_page_url(url)
    if not normalized_url:
        return None

    cleaned_name = _clean_discovered_page_name(_prefer_discovered_page_name(_page_name_from_discovered_link(name, normalized_url), normalized_url))
    if not cleaned_name:
        return None

    if _looks_like_numeric_identifier(cleaned_name) or _is_blocked_discovered_page_name(cleaned_name):
        return None

    return {
        'id': raw_page_id or _page_id_from_url(normalized_url) or normalized_url,
        'url': normalized_url,
        'name': cleaned_name,
    }


def _walk_graphql_candidates(node: Any, results: List[Dict[str, str]], seen_urls: Set[str]) -> None:
    if isinstance(node, dict):
        candidate = _page_item_from_graphql_candidate(node)
        if candidate:
            url = candidate['url']
            if url not in seen_urls:
                seen_urls.add(url)
                results.append(candidate)
        for value in node.values():
            _walk_graphql_candidates(value, results, seen_urls)
        return
    if isinstance(node, list):
        for item in node:
            _walk_graphql_candidates(item, results, seen_urls)


def _extract_pages_from_graphql_payload(payload: Any) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    seen_urls: Set[str] = set()
    _walk_graphql_candidates(payload, results, seen_urls)
    return results


async def discover_facebook_pages(cookies_json: str) -> Tuple[bool, List[Dict[str, str]], str]:
    """Discover pages visible to the current Facebook cookie session."""
    started_at = asyncio.get_running_loop().time()
    session_guard: Optional[Tuple[Any, ...]] = None
    try:
        session_guard = await _acquire_cookie_session_guard(
            cookies_json,
            'page discovery',
            wait_seconds=POST_DISCOVERY_COOKIE_LOCK_WAIT_SECONDS,
            enforce_min_interval=False,
        )
        playwright, browser, context, page = await launch_browser_session(cookies_json)
    except TimeoutError as exc:
        await _release_cookie_session_guard(session_guard)
        detail = (
            'Facebook cookie session is currently busy with another posting job. '
            'Page discovery was skipped to avoid interrupting the active Facebook session. '
            f'{exc}'
        )
        logger.info(f'Page discovery skipped because cookie session is busy: {exc}')
        return False, [], detail
    except Exception as exc:
        await _release_cookie_session_guard(session_guard)
        logger.error(f'Could not launch browser for page discovery: {exc}')
        return False, [], str(exc)
    discovered: Dict[str, Dict[str, str]] = {}

    try:
        await _enable_fast_discovery_mode(context, page)
        graphql_pages: List[Dict[str, str]] = []
        graphql_seen_urls: Set[str] = set()
        graphql_ready = asyncio.Event()
        graphql_debug_hits = 0
        graphql_sampled = 0

        async def _capture_graphql_pages(response: Any) -> None:
            nonlocal graphql_debug_hits, graphql_sampled
            try:
                url = str(response.url or '')
                lowered_url = url.lower()
                if '/graphql' not in lowered_url and 'relay' not in lowered_url:
                    return
                graphql_sampled += 1
                if graphql_sampled > 20 and graphql_ready.is_set():
                    return
                body_text = await response.text()
                if not body_text or len(body_text) < 80:
                    return
                lowered = body_text.lower()
                interesting = (
                    'pagescometlaunchpointunifiedquerypageslistredesignedquery',
                    'pagecometlaunchpointleftnavmenurootquery',
                    'pages_you_manage',
                    'pages list',
                    'pageslist',
                    'launchpoint',
                )
                if not any(marker in lowered for marker in interesting):
                    return
                graphql_debug_hits += 1
                try:
                    payload = json.loads(body_text)
                except JSONDecodeError:
                    return

                page_candidates = _extract_pages_from_graphql_payload(payload)
                for item in page_candidates:
                    if item['url'] not in graphql_seen_urls:
                        graphql_seen_urls.add(item['url'])
                        graphql_pages.append(item)
                if page_candidates:
                    graphql_ready.set()
                    logger.info(
                        f'PAGE_DISCOVERY stage="graphql_capture" added={len(page_candidates)} '
                        f'total={len(graphql_pages)} source={url[:120]}'
                    )
            except Exception as exc:
                logger.debug(f'PAGE_DISCOVERY graphql capture skipped: {exc}')

        page.on('response', lambda response: asyncio.create_task(_capture_graphql_pages(response)))

        for discovery_url in _PAGE_DISCOVERY_URLS:
            try:
                nav_started = asyncio.get_running_loop().time()
                logger.info(f'PAGE_DISCOVERY stage="navigate" url="{discovery_url}"')
                await page.goto(
                    discovery_url,
                    wait_until=cast(Any, PAGE_DISCOVERY_WAIT_UNTIL),
                    timeout=PAGE_DISCOVERY_TIMEOUT * 1000,
                )
                try:
                    await asyncio.wait_for(graphql_ready.wait(), timeout=PAGE_DISCOVERY_GRAPHQL_WAIT_SECONDS)
                except asyncio.TimeoutError:
                    pass
                try:
                    if not graphql_pages:
                        await page.wait_for_selector('div[role="main"], h1', timeout=1500)
                except Exception:
                    if not graphql_pages:
                        await _wait_for_facebook_ui_ready(page, timeout=500)
                await _resume_facebook_cookie_session(page)
                logger.info(
                    f'PAGE_DISCOVERY stage="navigate_complete" url="{discovery_url}" '
                    f'elapsed={asyncio.get_running_loop().time() - nav_started:.1f}s'
                )
            except Exception as exc:
                logger.warning(f'Facebook page discovery navigation failed: {exc}')
                continue

            if await _page_looks_logged_out(page):
                diagnostic_path = await _save_diagnostics(page, 'page_discovery_login_or_checkpoint')
                return False, [], _with_diagnostic('Facebook session is not logged in or needs verification.', diagnostic_path)
            security_detail = await _facebook_security_block_detail(page)
            if security_detail:
                await asyncio.to_thread(_mark_cookie_session_used, cookies_json, False, security_detail)
                diagnostic_path = await _save_diagnostics(page, 'page_discovery_security_checkpoint')
                return False, [], _with_diagnostic(security_detail, diagnostic_path)

            # Prefer GraphQL-captured pages first; fall back to DOM only if needed.
            page_count_before = len(discovered)
            extract_started = asyncio.get_running_loop().time()
            if graphql_pages:
                for item in graphql_pages:
                    discovered[item['url']] = item
                logger.info(
                    f'PAGE_DISCOVERY stage="graphql_used" added={len(discovered) - page_count_before} '
                    f'total={len(discovered)} hits={graphql_debug_hits}'
                )
            else:
                for item in await _extract_page_cards(page):
                    discovered[item['url']] = item
                logger.info(
                    f'PAGE_DISCOVERY stage="extract_cards" added={len(discovered) - page_count_before} '
                    f'total={len(discovered)} elapsed={asyncio.get_running_loop().time() - extract_started:.1f}s'
                )

            # Fallback: one limited scroll pass, still using only stable labels.
            if len(discovered) < 2:
                extract_started = asyncio.get_running_loop().time()
                page_count_before = len(discovered)
                if graphql_pages:
                    for item in graphql_pages:
                        discovered[item['url']] = item
                else:
                    for item in await _extract_page_cards(page):
                        discovered[item['url']] = item
                    if not discovered:
                        for item in await _extract_page_links(page):
                            discovered[item['url']] = item
                if len(discovered) > page_count_before:
                    logger.info(
                        f'PAGE_DISCOVERY stage="extract" added={len(discovered) - page_count_before} '
                        f'total={len(discovered)} elapsed={asyncio.get_running_loop().time() - extract_started:.1f}s'
                    )
                await page.evaluate('window.scrollBy(0, document.body.scrollHeight)')
                await _smart_wait(
                    lambda: page.evaluate('document.readyState === "complete"'),
                    timeout_ms=300,
                    check_interval_ms=100,
                )
                if len(discovered) >= 2:
                    break

            if discovered:
                break

        pages = list(discovered.values())
        pages = [item for item in pages if _is_valid_discovered_page_item(item)]
        pages.sort(key=lambda item: item.get('name', '').lower())
        if not pages:
            diagnostic_path = await _save_diagnostics(page, 'page_discovery_no_pages')
            return False, [], _with_diagnostic('No managed pages were found in the Facebook pages list.', diagnostic_path)
        logger.info(
            f'PAGE_DISCOVERY stage="finished" pages={len(pages)} '
            f'elapsed={asyncio.get_running_loop().time() - started_at:.1f}s'
        )
        return True, pages, ''
    except Exception as exc:
        logger.error(f'Facebook page discovery failed: {exc}')
        if _is_facebook_security_failure(str(exc)):
            await asyncio.to_thread(_mark_cookie_session_used, cookies_json, False, str(exc))
        diagnostic_path = await _save_diagnostics(page, 'page_discovery_exception')
        return False, [], _with_diagnostic(str(exc), diagnostic_path)
    finally:
        try:
            await browser.close()
        finally:
            try:
                await playwright.stop()
            finally:
                await _release_cookie_session_guard(session_guard)


def _validate_facebook_session_fast(cookies_json: str) -> Tuple[bool, str]:
    """
    Fast session validation via HTTP request (~1-2s instead of ~30-40s).
    Checks if the cookies can access Facebook without being redirected to login.
    No browser launch required.
    """
    import requests as _requests

    c_user = _cookie_value_from_json(cookies_json, 'c_user')
    if not c_user:
        return False, 'No c_user cookie found'

    try:
        cookies_list = json.loads(cookies_json)
    except Exception:
        return False, 'Invalid cookies JSON'

    cookie_dict = {}
    for cookie in cookies_list:
        if isinstance(cookie, dict):
            name = str(cookie.get('name', '')).strip()
            value = str(cookie.get('value', '')).strip()
            if name and value:
                cookie_dict[name] = value

    if not cookie_dict.get('c_user') or not cookie_dict.get('xs'):
        return False, 'Missing essential cookies (c_user, xs)'

    headers = {
        'User-Agent': FACEBOOK_BROWSER_USER_AGENT,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': FACEBOOK_BROWSER_ACCEPT_LANGUAGE,
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
    }

    try:
        resp = _requests.get(
            'https://www.facebook.com/me',
            cookies=cookie_dict,
            headers=headers,
            timeout=10,
            allow_redirects=True,
        )

        final_url = resp.url.lower()
        from urllib.parse import urlparse
        final_path = urlparse(final_url).path.strip('/')
        html_text = resp.text[:200000]
        html_snippet = html_text[:10000].lower()
        login_indicators = [
            'id="loginform"',
            'name="email"',
            'name="pass"',
            'name="login"',
            'data-testid="royal_login_button"',
            'href="/login/"',
        ]

        # Checkpoint = account needs verification
        if 'checkpoint' in final_url:
            return False, 'Facebook checkpoint or verification required'

        # Disabled/suspended account
        if 'disabled' in final_url or 'suspended' in final_url:
            return False, 'Account appears disabled or suspended'

        # A direct login URL is definitive, but a bare homepage can be an ambiguous
        # Facebook redirect. Treat homepage as inconclusive unless the body exposes
        # visible login-form markers, so cookie checks do not burn valid sessions.
        if 'login' in final_url:
            return False, 'Cookies expired (redirected to login)'
        if not final_path:
            if any(indicator in html_snippet for indicator in login_indicators):
                return False, 'Cookies expired (login form detected on Facebook homepage)'
            return False, 'Facebook returned homepage without auth failure markers'

        if resp.status_code != 200:
            return False, f'Facebook returned HTTP {resp.status_code}'

        # If we reached a profile page, session is likely valid.
        # Check the HTML for login indicators just to be sure.
        if any(indicator in html_snippet for indicator in login_indicators):
            return False, 'Cookies expired (login form detected in page)'

        auth_marker_patterns = [
            r'"USER_ID"\s*:\s*"' + re.escape(c_user) + r'"',
            r'"ACCOUNT_ID"\s*:\s*"' + re.escape(c_user) + r'"',
            r'"actorID"\s*:\s*"' + re.escape(c_user) + r'"',
            r'"actor_id"\s*:\s*"?' + re.escape(c_user) + r'"?',
            r'"userID"\s*:\s*"' + re.escape(c_user) + r'"',
            r'__user=' + re.escape(c_user) + r'\b',
        ]
        if not any(re.search(pattern, html_text, re.I) for pattern in auth_marker_patterns):
            logger.info(
                'Fast session validation accepted a non-login Facebook response without strict user markers: '
                f'path={final_path[:80]} c_user_suffix={c_user[-4:]}'
            )
            return True, (
                'Session appears valid. Facebook served the account page, but did not expose strict '
                'authenticated user markers in the HTTP response.'
            )

        logger.info(f'⚡ Fast session validation passed for c_user_suffix={c_user[-4:]}')
        return True, 'Session is valid.'

    except _requests.Timeout:
        return False, 'HTTP request timed out'
    except Exception as exc:
        return False, f'HTTP validation error: {exc}'


async def validate_facebook_session(cookies_json: str) -> Tuple[bool, str]:
    """Check whether saved Facebook cookies still open an authenticated session.

    Uses a fast HTTP-based check first (~1-2s). Falls back to full
    Playwright browser only if the fast check is inconclusive.
    """
    # ── Fast path: HTTP request (no browser needed) ──
    try:
        loop = asyncio.get_running_loop()
        ok, detail = await loop.run_in_executor(
            None, _validate_facebook_session_fast, cookies_json
        )
        # If the fast check gave a definitive answer, use it
        if ok:
            return True, detail
        # If it's a clear failure (login redirect, checkpoint), trust it
        definitive_failure_keywords = [
            'expired',
            'login',
            'checkpoint',
            'disabled',
            'suspended',
            'no c_user',
            'invalid cookies',
            'missing essential cookies',
        ]
        if any(keyword in detail.lower() for keyword in definitive_failure_keywords):
            return False, detail
        if not FACEBOOK_BROWSER_VALIDATION_FALLBACK_ENABLED:
            logger.info(
                'Fast session validation was inconclusive; browser fallback is disabled '
                'to preserve the Facebook cookie session.'
            )
            return False, (
                'Session check inconclusive; browser validation skipped to protect cookie session. '
                f'HTTP detail: {detail}'
            )
        # Otherwise (ambiguous), fall through to Playwright
        logger.info(f'⚡ Fast session validation inconclusive: {detail}; falling back to browser')
    except Exception as exc:
        if not FACEBOOK_BROWSER_VALIDATION_FALLBACK_ENABLED:
            logger.info(
                f'Fast session validation exception and browser fallback disabled: {exc}'
            )
            return False, (
                'Session check inconclusive; browser validation skipped to protect cookie session. '
                f'HTTP error: {exc}'
            )
        logger.info(f'⚡ Fast session validation exception: {exc}; falling back to browser')

    # ── Slow fallback: Playwright browser ──
    session_guard: Optional[Tuple[Any, ...]] = None
    try:
        session_guard = await _acquire_cookie_session_guard(
            cookies_json,
            'session validation',
            enforce_min_interval=False,
        )
        playwright, browser, context, page = await launch_browser_session(cookies_json)
    except Exception as exc:
        await _release_cookie_session_guard(session_guard)
        logger.error(f'Could not launch browser for session validation: {exc}')
        return False, str(exc)

    try:
        await page.goto('https://www.facebook.com/me', wait_until='domcontentloaded', timeout=PAGE_DISCOVERY_TIMEOUT * 1000)
        await _wait_for_facebook_ui_ready(page, timeout=2500)
        await _resume_facebook_cookie_session(page)

        current_url = page.url.lower()
        security_detail = await _facebook_security_block_detail(page)
        if security_detail:
            await asyncio.to_thread(_mark_cookie_session_used, cookies_json, False, security_detail)
            diagnostic_path = await _save_diagnostics(page, 'session_validation_security_checkpoint')
            return False, _with_diagnostic(security_detail, diagnostic_path)
        if await _page_looks_logged_out(page):
            diagnostic_path = await _save_diagnostics(page, 'session_validation_login')
            return False, _with_diagnostic('Facebook redirected to login. Cookies are expired or incomplete.', diagnostic_path)
        if 'checkpoint' in current_url:
            await asyncio.to_thread(_mark_cookie_session_used, cookies_json, False, 'Facebook checkpoint or verification is required.')
            diagnostic_path = await _save_diagnostics(page, 'session_validation_checkpoint')
            return False, _with_diagnostic('Facebook checkpoint or verification is required.', diagnostic_path)

        title = (await page.title()).lower()
        if 'log in' in title or 'login' in title:
            diagnostic_path = await _save_diagnostics(page, 'session_validation_login_title')
            return False, _with_diagnostic('Facebook showed the login page. Cookies are expired or incomplete.', diagnostic_path)

        return True, 'Session is valid.'
    except Exception as exc:
        logger.warning(f'Facebook session validation failed: {exc}')
        if _is_facebook_security_failure(str(exc)):
            await asyncio.to_thread(_mark_cookie_session_used, cookies_json, False, str(exc))
        diagnostic_path = await _save_diagnostics(page, 'session_validation_exception')
        return False, _with_diagnostic(str(exc), diagnostic_path)
    finally:
        try:
            await browser.close()
        finally:
            try:
                await playwright.stop()
            finally:
                await _release_cookie_session_guard(session_guard)


def _get_facebook_account_name_fast(cookies_json: str) -> Tuple[bool, str, str]:
    """
    Fast account name lookup via HTTP request (~1-2s instead of ~40s).
    Uses the requests library to fetch the Facebook profile page
    and extract the account name from the HTML title/meta tags.
    No browser launch required.
    """
    import requests as _requests

    c_user = _cookie_value_from_json(cookies_json, 'c_user')
    if not c_user:
        logger.info('⚡ Fast HTTP lookup: no c_user cookie found, skipping')
        return False, '', 'No c_user cookie found'

    # Build cookie dict from JSON
    try:
        cookies_list = json.loads(cookies_json)
    except Exception:
        return False, '', 'Invalid cookies JSON'

    cookie_dict = {}
    for cookie in cookies_list:
        if isinstance(cookie, dict):
            name = str(cookie.get('name', '')).strip()
            value = str(cookie.get('value', '')).strip()
            if name and value:
                cookie_dict[name] = value

    if not cookie_dict.get('c_user') or not cookie_dict.get('xs'):
        logger.info('⚡ Fast HTTP lookup: missing c_user or xs cookie, skipping')
        return False, '', 'Missing essential cookies (c_user, xs)'

    headers = {
        'User-Agent': FACEBOOK_BROWSER_USER_AGENT,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': FACEBOOK_BROWSER_ACCEPT_LANGUAGE,
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
    }

    # Try multiple URLs for resilience
    urls_to_try = [
        f'https://www.facebook.com/profile.php?id={c_user}',
        f'https://m.facebook.com/profile.php?id={c_user}',
        'https://www.facebook.com/me',
    ]

    last_error = ''
    for url in urls_to_try:
        try:
            logger.info(f'⚡ Fast HTTP lookup: trying {url}')
            resp = _requests.get(
                url,
                cookies=cookie_dict,
                headers=headers,
                timeout=10,
                allow_redirects=True,
            )

            logger.info(f'⚡ Fast HTTP lookup: status={resp.status_code} final_url={resp.url[:100]}')

            if resp.status_code != 200:
                last_error = f'Facebook returned HTTP {resp.status_code}'
                continue

            html = resp.text

            # Check for login redirect / checkpoint
            if '/login' in resp.url or 'checkpoint' in resp.url:
                return False, '', 'Cookies expired (redirected to login)'

            raw_title = ''

            # ── Strategy 1: Extract from <title> tag ──
            title_match = re.search(r'<title[^>]*>([^<]+)</title>', html, re.I)
            if title_match:
                raw_title = title_match.group(1).strip()
                logger.info(f'⚡ Fast HTTP lookup: raw title = "{raw_title[:60]}"')
                name = re.split(r'\s*[|\-–—]\s*(?:Facebook|فيسبوك)', raw_title, maxsplit=1)[0].strip()
                name = name.replace('&amp;', '&').replace('&#039;', "'").replace('&quot;', '"').replace('&#x27;', "'")
                if name and name.lower() != 'facebook' and _is_good_account_name(name):
                    logger.info(f'⚡ Fast HTTP name lookup succeeded: "{name}" (from title)')
                    return True, _clean_facebook_account_name(name), ''

            # ── Strategy 2: Extract from og:title meta tag ──
            og_match = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)', html, re.I)
            if og_match:
                name = og_match.group(1).strip()
                name = re.split(r'\s*[|\-–—]\s*(?:Facebook|فيسبوك)', name, maxsplit=1)[0].strip()
                name = name.replace('&amp;', '&').replace('&#039;', "'").replace('&quot;', '"').replace('&#x27;', "'")
                if name and name.lower() != 'facebook' and _is_good_account_name(name):
                    logger.info(f'⚡ Fast HTTP name lookup succeeded: "{name}" (from og:title)')
                    return True, _clean_facebook_account_name(name), ''

            # ── Strategy 3: Extract from profile URL slug ──
            # Facebook redirects /profile.php?id=123 → /firstname.lastname.N/
            # e.g. /mohammed.shabana.5/ → "Mohammed Shabana"
            final_path = urlparse(resp.url).path.strip('/')
            if final_path and final_path != 'profile.php' and '/' not in final_path:
                # Convert "mohammed.shabana.5" → "Mohammed Shabana"
                slug_parts = final_path.split('.')
                # Remove trailing numeric suffixes (e.g. ".5", ".123")
                name_parts = [p for p in slug_parts if not p.isdigit()]
                if name_parts:
                    slug_name = ' '.join(p.capitalize() for p in name_parts)
                    if len(slug_name) >= 3 and _is_good_account_name(slug_name):
                        # Slug names are weak: only accept them if they agree with a stronger signal.
                        stronger_candidates = []
                        if 'raw_title' in locals():
                            stronger_candidates.append(raw_title)
                        if 'og_match' in locals() and og_match:
                            stronger_candidates.append(og_match.group(1))
                        if any(_account_name_candidates_agree(slug_name, candidate) for candidate in stronger_candidates):
                            logger.info(f'⚡ Fast HTTP name lookup succeeded: "{slug_name}" (from URL slug "{final_path}")')
                            return True, _clean_facebook_account_name(slug_name), ''
                        logger.info(f'⚡ Fast HTTP lookup: URL slug name "{slug_name}" rejected (no stronger match)')
                    else:
                        logger.info(f'⚡ Fast HTTP lookup: URL slug name "{slug_name}" rejected')

            # ── Strategy 4: Only the most specific JSON pattern — name next to c_user ID ──
            # This is safe because it anchors the match to the user's own ID
            id_name_match = re.search(
                r'"id"\s*:\s*"' + re.escape(c_user) + r'"[^}]{0,80}"name"\s*:\s*"([^"]{2,50})"',
                html,
            )
            if not id_name_match:
                id_name_match = re.search(
                    r'"name"\s*:\s*"([^"]{2,50})"[^}]{0,80}"id"\s*:\s*"' + re.escape(c_user) + r'"',
                    html,
                )
            if id_name_match:
                name = id_name_match.group(1).strip()
                try:
                    name = name.encode().decode('unicode_escape')
                except Exception:
                    pass
                # Extra safety: reject anything that looks like code/tech
                _TECH_RE = re.compile(r'Worker|Bundle|Module|Script|WAWeb|webpack|\.js|\.css|http|function', re.I)
                if name and _is_good_account_name(name) and not _TECH_RE.search(name):
                    logger.info(f'⚡ Fast HTTP name lookup succeeded: "{name}" (from JSON near c_user ID)')
                    return True, _clean_facebook_account_name(name), ''
                else:
                    logger.info(f'⚡ Fast HTTP lookup: JSON name "{name}" rejected')

            last_error = f'Could not extract name from {url}'

        except _requests.Timeout:
            last_error = f'HTTP request timed out for {url}'
            logger.info(f'⚡ Fast HTTP lookup: timed out for {url}')
        except Exception as exc:
            last_error = f'HTTP error for {url}: {exc}'
            logger.info(f'⚡ Fast HTTP lookup: error for {url}: {exc}')

    logger.info(f'⚡ Fast HTTP lookup: all strategies exhausted. Last error: {last_error}')
    return False, '', last_error


async def get_facebook_account_name(cookies_json: str) -> Tuple[bool, str, str]:
    """Resolve a display name from the authenticated Facebook cookie session.
    
    Uses a fast HTTP-based lookup first (~1-2s). Falls back to full
    Playwright browser only if the fast path fails.
    """
    # ── Fast path: HTTP request (no browser needed) ──
    try:
        loop = asyncio.get_running_loop()
        ok, name, error = await loop.run_in_executor(
            None, _get_facebook_account_name_fast, cookies_json
        )
        if ok and name:
            return True, name, ''
        if not FACEBOOK_BROWSER_ACCOUNT_LOOKUP_FALLBACK_ENABLED:
            logger.info(
                f'⚡ Fast HTTP name lookup did not succeed and browser fallback is disabled '
                f'to preserve the Facebook cookie session: {error}'
            )
            return False, '', (
                f'{error}. Browser account-name lookup is disabled to protect the cookie session. '
                'Use cookies whose Facebook profile name can be read via HTTP, or temporarily enable '
                'FACEBOOK_BROWSER_ACCOUNT_LOOKUP_FALLBACK_ENABLED.'
            )
        logger.info(f'⚡ Fast HTTP name lookup did not succeed: {error}; falling back to browser')
    except Exception as exc:
        if not FACEBOOK_BROWSER_ACCOUNT_LOOKUP_FALLBACK_ENABLED:
            logger.info(
                f'⚡ Fast HTTP name lookup exception and browser fallback disabled: {exc}'
            )
            return False, '', (
                f'HTTP account-name lookup failed: {exc}. Browser account-name lookup is disabled '
                'to protect the cookie session.'
            )
        logger.info(f'⚡ Fast HTTP name lookup exception: {exc}; falling back to browser')

    # ── Slow fallback: Playwright browser ──
    session_guard: Optional[Tuple[Any, ...]] = None
    try:
        session_guard = await _acquire_cookie_session_guard(
            cookies_json,
            'account name lookup',
            enforce_min_interval=False,
        )
        playwright, browser, context, page = await launch_browser_session(cookies_json)
    except Exception as exc:
        await _release_cookie_session_guard(session_guard)
        logger.error(f'Could not launch browser for account name lookup: {exc}')
        return False, '', str(exc)

    try:
        c_user = _cookie_value_from_json(cookies_json, 'c_user')
        lookup_urls = []
        if c_user:
            lookup_urls.extend([
                f'https://www.facebook.com/profile.php?id={c_user}',
                f'https://m.facebook.com/profile.php?id={c_user}',
            ])
        lookup_urls.append('https://www.facebook.com/me')

        saw_login = False
        saw_checkpoint = False
        all_candidates: List[str] = []
        for lookup_url in lookup_urls:
            try:
                await page.goto(lookup_url, wait_until='domcontentloaded', timeout=PAGE_DISCOVERY_TIMEOUT * 1000)
                try:
                    await page.wait_for_selector('h1', timeout=3000)
                except Exception:
                    await _wait_for_facebook_ui_ready(page, timeout=500)
                await _resume_facebook_cookie_session(page)
            except Exception:
                continue

            current_url = page.url.lower()
            security_detail = await _facebook_security_block_detail(page)
            if security_detail:
                await asyncio.to_thread(_mark_cookie_session_used, cookies_json, False, security_detail)
                diagnostic_path = await _save_diagnostics(page, 'account_name_security_checkpoint')
                return False, '', _with_diagnostic(security_detail, diagnostic_path)
            if await _page_looks_logged_out(page):
                saw_login = True
                continue
            if 'checkpoint' in current_url:
                saw_checkpoint = True
                continue

            candidates = await _read_account_name_candidates(page)
            all_candidates.extend(candidates)
            for candidate in candidates:
                if _is_good_account_name(candidate):
                    return True, _clean_facebook_account_name(candidate), ''

        if saw_checkpoint:
            await asyncio.to_thread(_mark_cookie_session_used, cookies_json, False, 'Facebook checkpoint or verification is required.')
            diagnostic_path = await _save_diagnostics(page, 'account_name_checkpoint')
            return False, '', _with_diagnostic('Facebook checkpoint or verification is required.', diagnostic_path)
        if saw_login:
            diagnostic_path = await _save_diagnostics(page, 'account_name_login')
            return False, '', _with_diagnostic('Facebook redirected to login. Cookies are expired or incomplete.', diagnostic_path)

        logger.warning(f'Facebook account name candidates rejected: {all_candidates[:8]}')

        diagnostic_path = await _save_diagnostics(page, 'account_name_not_found')
        return False, '', _with_diagnostic('Could not read the account name from Facebook.', diagnostic_path)
    except Exception as exc:
        logger.warning(f'Facebook account name lookup failed: {exc}')
        if _is_facebook_security_failure(str(exc)):
            await asyncio.to_thread(_mark_cookie_session_used, cookies_json, False, str(exc))
        diagnostic_path = await _save_diagnostics(page, 'account_name_exception')
        return False, '', _with_diagnostic(str(exc), diagnostic_path)
    finally:
        try:
            await browser.close()
        finally:
            try:
                await playwright.stop()
            finally:
                await _release_cookie_session_guard(session_guard)


async def _click_first_available(page_or_locator: Any, candidates: List[Any], timeout: int = 5000) -> bool:
    deadline = asyncio.get_running_loop().time() + (timeout / 1000)
    for candidate in candidates:
        remaining_ms = int((deadline - asyncio.get_running_loop().time()) * 1000)
        if remaining_ms <= 0:
            return False
        attempt_timeout = max(250, min(remaining_ms, 1500))
        try:
            locator: Any = candidate() if callable(candidate) else page_or_locator.locator(candidate)
            target = locator.first
            await target.wait_for(state='visible', timeout=attempt_timeout)
            await target.click(timeout=min(attempt_timeout, 1000))
            return True
        except Exception:
            continue
    return False


async def _click_text_match(page: Page, pattern: re.Pattern, timeout: int = 3000) -> bool:
    """Click a visible element whose text or aria-label matches the pattern."""
    deadline = asyncio.get_running_loop().time() + (timeout / 1000)
    selectors = (
        "div[role='button'], a[role='button'], button, a[href], span, div[aria-label]",
    )

    while asyncio.get_running_loop().time() < deadline:
        try:
            handles = await page.locator(selectors[0]).element_handles()
            for handle in handles[:250]:
                try:
                    text = await handle.evaluate(
                        """
                        element => (
                            element.innerText ||
                            element.getAttribute('aria-label') ||
                            element.textContent ||
                            ''
                        ).trim()
                        """
                    )
                    if not text or not pattern.search(str(text)):
                        continue
                    box = await handle.bounding_box()
                    if not box or box['width'] <= 0 or box['height'] <= 0:
                        continue
                    await handle.click(timeout=2000)
                    return True
                except Exception:
                    continue
        except Exception:
            pass
        await asyncio.sleep(0.2)
    return False


async def _click_enabled_text_match(
    root: Any,
    pattern: re.Pattern,
    timeout: int = 3000,
    reject_pattern: Optional[re.Pattern] = None,
) -> Optional[str]:
    """Click a visible enabled button-like element matching text/aria-label."""
    deadline = asyncio.get_running_loop().time() + (timeout / 1000)
    selector = (
        "div[role='button'], button, a[role='button'], input[type='submit'], "
        "div[aria-label], span[role='button']"
    )

    while asyncio.get_running_loop().time() < deadline:
        try:
            handles = await root.locator(selector).element_handles()
            for handle in handles[:300]:
                try:
                    state = await handle.evaluate(
                        """
                        element => {
                            const style = window.getComputedStyle(element);
                            const text = (
                                element.innerText ||
                                element.getAttribute('aria-label') ||
                                element.getAttribute('value') ||
                                element.textContent ||
                                ''
                            ).trim();
                            const disabled = Boolean(
                                element.disabled ||
                                element.getAttribute('aria-disabled') === 'true' ||
                                element.closest('[aria-disabled="true"]') ||
                                element.closest('[disabled]')
                            );
                            return {
                                text,
                                disabled,
                                visible: style && style.visibility !== 'hidden' && style.display !== 'none',
                            };
                        }
                        """
                    )
                    text = str(state.get('text') or '').strip()
                    if not text or not pattern.search(text):
                        continue
                    if reject_pattern and reject_pattern.search(text):
                        continue
                    if state.get('disabled') or not state.get('visible'):
                        continue
                    box = await handle.bounding_box()
                    if not box or box['width'] <= 0 or box['height'] <= 0:
                        continue
                    await handle.scroll_into_view_if_needed(timeout=1000)
                    await handle.click(timeout=2500)
                    return text
                except Exception:
                    continue
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return None


async def _has_enabled_text_match(
    root: Any,
    pattern: re.Pattern,
    reject_pattern: Optional[re.Pattern] = None,
) -> bool:
    selector = (
        "div[role='button'], button, a[role='button'], input[type='submit'], "
        "div[aria-label], span[role='button']"
    )
    try:
        handles = await root.locator(selector).element_handles()
        for handle in handles[:300]:
            try:
                state = await handle.evaluate(
                    """
                    element => {
                        const style = window.getComputedStyle(element);
                        const text = (
                            element.innerText ||
                            element.getAttribute('aria-label') ||
                            element.getAttribute('value') ||
                            element.textContent ||
                            ''
                        ).trim();
                        const disabled = Boolean(
                            element.disabled ||
                            element.getAttribute('aria-disabled') === 'true' ||
                            element.closest('[aria-disabled="true"]') ||
                            element.closest('[disabled]')
                        );
                        return {
                            text,
                            disabled,
                            visible: style && style.visibility !== 'hidden' && style.display !== 'none',
                        };
                    }
                    """
                )
                text = str(state.get('text') or '').strip()
                if not text or not pattern.search(text):
                    continue
                if reject_pattern and reject_pattern.search(text):
                    continue
                if state.get('disabled') or not state.get('visible'):
                    continue
                box = await handle.bounding_box()
                if box and box['width'] > 0 and box['height'] > 0:
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _upload_ready_timeout_seconds(timeout: float) -> float:
    """Normalize upload-readiness timeouts to seconds.

    MEDIA_UPLOAD_READY_* env values are seconds. Keep values above 1000
    compatible with older millisecond-style callers.
    """
    timeout_value = max(0.1, float(timeout))
    return timeout_value / 1000 if timeout_value > 1000 else timeout_value


def _media_upload_ready_timeout_seconds(post_type: str) -> float:
    return _upload_ready_timeout_seconds(
        MEDIA_UPLOAD_READY_IMAGE_TIMEOUT if post_type == 'image' else MEDIA_UPLOAD_READY_VIDEO_TIMEOUT
    )


def _pages_portal_timeout_seconds(post_type: str, has_media: bool) -> int:
    publish_processing_budget = int((POST_PUBLISH_IN_PROGRESS_TIMEOUT_MS / 1000) + 55)
    base_timeout = min(FACEBOOK_POST_TIMEOUT_SECONDS, max(75, publish_processing_budget))
    if post_type == 'video':
        video_timeout = int(_media_upload_ready_timeout_seconds('video') + POST_SUBMIT_ACTION_TIMEOUT_SECONDS + 50)
        return min(
            FACEBOOK_POST_TIMEOUT_SECONDS,
            max(base_timeout, POST_PAGES_PORTAL_VIDEO_TIMEOUT_SECONDS, video_timeout),
        )
    if post_type == 'image' or has_media:
        image_timeout = int(_media_upload_ready_timeout_seconds('image') + POST_SUBMIT_ACTION_TIMEOUT_SECONDS + 35)
        return min(
            FACEBOOK_POST_TIMEOUT_SECONDS,
            max(base_timeout, POST_PAGES_PORTAL_IMAGE_TIMEOUT_SECONDS, image_timeout),
        )
    return min(FACEBOOK_POST_TIMEOUT_SECONDS, max(base_timeout, POST_PAGES_PORTAL_TEXT_TIMEOUT_SECONDS))


def _batch_page_timeout_seconds(post_type: str, has_media: bool) -> int:
    route_budget = max(POST_DIRECT_COMPOSER_TIMEOUT_SECONDS, POST_DESKTOP_COMPOSER_TIMEOUT_SECONDS)
    portal_budget = _pages_portal_timeout_seconds(post_type, has_media)
    fallback_budget = route_budget + portal_budget + 35
    if post_type == 'video':
        upload_retry_budget = int((_media_upload_ready_timeout_seconds('video') * 2) + POST_SUBMIT_ACTION_TIMEOUT_SECONDS)
        timeout = max(POST_BATCH_PAGE_TIMEOUT_SECONDS, fallback_budget, upload_retry_budget + route_budget + 45)
    elif post_type == 'image' or has_media:
        upload_retry_budget = int((_media_upload_ready_timeout_seconds('image') * 2) + POST_SUBMIT_ACTION_TIMEOUT_SECONDS)
        timeout = max(POST_BATCH_PAGE_TIMEOUT_SECONDS, fallback_budget, upload_retry_budget + route_budget + 30)
    else:
        timeout = max(POST_BATCH_PAGE_TIMEOUT_SECONDS, fallback_budget)
    if POST_BATCH_PAGE_TIMEOUT_MAX_SECONDS > 0:
        return max(60, min(timeout, POST_BATCH_PAGE_TIMEOUT_MAX_SECONDS))
    return timeout


def _batch_account_lock_ttl_seconds(posts: List[Dict[str, Any]]) -> int:
    page_timeout_budget = sum(
        _batch_page_timeout_seconds(
            str(post.get('post_type') or 'post'),
            bool(post.get('media_url')),
        )
        for post in posts
    )
    retry_multiplier = 1 + max(0, POST_PAGE_RECOVERY_RETRY_MAX if POST_PAGE_RECOVERY_RETRY_ENABLED else 0)
    return max(
        60,
        POST_BATCH_ACCOUNT_LOCK_TTL_SECONDS,
        (page_timeout_budget * retry_multiplier) + 120,
    )


async def _wait_for_composer_upload_ready(
    page: Page,
    timeout: float = 45,
    upload_tracker: Optional[Dict[str, Any]] = None,
) -> None:
    """Give Facebook time to enable publishing after media selection."""
    timeout_seconds = _upload_ready_timeout_seconds(timeout)
    started_at = asyncio.get_running_loop().time()
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    error_pattern = re.compile(r"can't be uploaded|couldn.t upload|upload failed|تعذر|فشل|خطأ في التحميل", re.I)
    action_pattern = re.compile(r'^(Next|Continue|Post|Publish|Share|Post now|Publish now|التالي|متابعة|نشر|مشاركة)$', re.I)
    stable_clear_count = 0
    while asyncio.get_running_loop().time() < deadline:
        if POST_UPLOAD_EARLY_FAILURE_DETECTION_ENABLED:
            upload_failure = _upload_tracker_failure_detail(upload_tracker)
            if upload_failure:
                raise RuntimeError(f'Facebook returned an upload error: {upload_failure}')
        if int((upload_tracker or {}).get('completed_success') or 0) > 0:
            logger.info(
                f'Media upload readiness detected by completed upload response after '
                f'{asyncio.get_running_loop().time() - started_at:.1f}s'
            )
            return
        try:
            error_text = await page.locator(
                "text=/can't be uploaded|couldn.t upload|upload failed|تعذر|فشل/i"
            ).first.inner_text(timeout=500)
            if error_text and error_pattern.search(error_text):
                raise RuntimeError(error_text.strip())
        except RuntimeError:
            raise
        except Exception:
            pass
        try:
            dialog = await _find_composer_context(page)

            # Fast path: check if submit buttons are enabled via aria-label
            try:
                for label in ["التالي", "نشر", "Next", "Post", "Publish"]:
                    btn = dialog.locator(f"div[role='button'][aria-label='{label}']").first
                    if await btn.is_visible(timeout=300):
                        disabled = await btn.get_attribute('aria-disabled')
                        if disabled != 'true':
                            logger.info(
                                f'Media upload readiness detected via enabled aria-label button "{label}" '
                                f'after {asyncio.get_running_loop().time() - started_at:.1f}s'
                            )
                            return
            except Exception:
                pass

            if await _has_enabled_text_match(dialog, action_pattern):
                logger.info(
                    f'Media upload readiness detected by enabled composer action after '
                    f'{asyncio.get_running_loop().time() - started_at:.1f}s'
                )
                return
            busy_count = await dialog.locator("[role='progressbar'], [aria-busy='true']").count()
            busy_count += await dialog.get_by_text(
                re.compile(r'Uploading|Processing|تحميل|جار(?:ي)?\s+(?:التحميل|المعالجة)|يتم التحميل|جاري المعالجة', re.I)
            ).count()
            disabled_submit_count = await page.locator(
                "div[aria-disabled='true']:has-text('Next'), div[aria-disabled='true']:has-text('Post'), "
                "div[aria-disabled='true']:has-text('متابعة'), div[aria-disabled='true']:has-text('نشر'), "
                "div[aria-disabled='true'][aria-label='التالي'], div[aria-disabled='true'][aria-label='نشر'], "
                "button[disabled]:has-text('Next'), button[disabled]:has-text('Post'), "
                "button[disabled]:has-text('متابعة'), button[disabled]:has-text('نشر')"
            ).count()
            if busy_count == 0 and disabled_submit_count == 0:
                stable_clear_count += 1
                if stable_clear_count >= 2:
                    logger.info(
                        f'Media upload readiness detected by clear busy state after '
                        f'{asyncio.get_running_loop().time() - started_at:.1f}s'
                    )
                    return
            else:
                stable_clear_count = 0
        except Exception:
            raise
        await asyncio.sleep(0.3)
    raise TimeoutError(f'Media upload was not ready after {timeout_seconds:.1f}s')


async def _count_composer_media(page: Page, post_type: str) -> int:
    media_selector = (
        'video, [aria-label*="video" i], [aria-label*="فيديو" i]'
        if post_type == 'video'
        else (
            'img, [aria-label*="photo" i], [aria-label*="image" i], '
            '[aria-label*="صورة" i], [style*="background-image"]'
        )
    )
    try:
        dialog = await _find_composer_context(page)
        return await dialog.locator(media_selector).count()
    except Exception:
        return 0


async def _count_selected_media_file_inputs(page: Page, post_type: str) -> int:
    """Count matching file inputs that already hold a selected local file."""
    is_video = post_type == 'video'

    async def _count_in(root: Any) -> int:
        total = 0
        file_inputs = await root.locator("input[type='file']").all()
        for file_input in file_inputs:
            try:
                accept_attr = (await file_input.get_attribute('accept') or '').lower()
                if is_video:
                    if 'image' in accept_attr and 'video' not in accept_attr:
                        continue
                elif 'video' in accept_attr and 'image' not in accept_attr:
                    continue
                files_count = await file_input.evaluate('el => el.files ? el.files.length : 0')
                total += int(files_count or 0)
            except Exception:
                continue
        return total

    try:
        dialog = await _find_composer_context(page)
        count = await _count_in(dialog)
        if count:
            return count
    except Exception:
        pass
    try:
        return await _count_in(page)
    except Exception:
        return 0


async def _verify_composer_media_visible(
    page: Page,
    post_type: str,
    *,
    before_count: int = 0,
    timeout: int = 10000,
    allow_existing: bool = False,
) -> bool:
    """Check that Facebook rendered the selected media inside the active composer."""
    deadline = asyncio.get_running_loop().time() + (timeout / 1000)
    while asyncio.get_running_loop().time() < deadline:
        try:
            dialog = await _find_composer_context(page)
            media_count = await _count_composer_media(page, post_type)
            busy_count = await dialog.locator("[role='progressbar'], [aria-busy='true']").count()
            media_visible = media_count > before_count or (allow_existing and media_count > 0)
            if media_visible and busy_count == 0:
                logger.info(
                    f'Media visibility confirmed in composer: '
                    f'type={post_type} before={before_count} after={media_count} '
                    f'allow_existing={allow_existing}'
                )
                return True
        except Exception:
            pass
        await asyncio.sleep(0.3)
    return False


class ButtonFinder:
    """Multi-strategy finder for Facebook composer publish controls."""

    _publish_pattern = re.compile(
        r'^\s*(Post|Publish|Share|Send|Post now|Publish now|'
        r'نشر|انشر|مشاركة|Publier|Publicar|Postar|発表|发布)\s*$',
        re.I,
    )
    _reject_pattern = re.compile(
        r'Add to your post|Photo/video|Feeling|Check in|More|Emoji|'
        r'Cancel|Close|Back|Previous|Next|Audience|Public|إضافة|صورة|فيديو',
        re.I,
    )

    def __init__(self, page: Page):
        self.page = page
        self._last_successful_strategy: Optional[str] = _BUTTON_FINDER_LAST_SUCCESSFUL_STRATEGY

    async def find_publish_button(self, context: Any) -> Optional[Any]:
        global _BUTTON_FINDER_LAST_SUCCESSFUL_STRATEGY

        strategies = [
            ('aria_label_exact', self._find_by_aria_label_exact),
            ('aria_label_pattern', self._find_by_aria_label_pattern),
            ('text_exact', self._find_by_text_exact),
            ('text_pattern', self._find_by_text_pattern),
            ('visual_position', self._find_by_visual_position),
            ('semantic_role', self._find_by_semantic_role),
        ]

        if self._last_successful_strategy:
            for name, strategy in strategies:
                if name != self._last_successful_strategy:
                    continue
                element = await strategy(context)
                if element is not None:
                    _BUTTON_FINDER_LAST_SUCCESSFUL_STRATEGY = name
                    return element
                break

        for name, strategy in strategies:
            element = await strategy(context)
            if element is not None:
                self._last_successful_strategy = name
                _BUTTON_FINDER_LAST_SUCCESSFUL_STRATEGY = name
                logger.info(f'Found publish button via strategy: {name}')
                return element
        return None

    def forget_last_successful_strategy(self) -> None:
        global _BUTTON_FINDER_LAST_SUCCESSFUL_STRATEGY

        if self._last_successful_strategy == _BUTTON_FINDER_LAST_SUCCESSFUL_STRATEGY:
            _BUTTON_FINDER_LAST_SUCCESSFUL_STRATEGY = None
        self._last_successful_strategy = None

    async def _is_actionable(self, element: Any, min_width: int = 30, min_height: int = 16) -> bool:
        try:
            if not await element.is_visible(timeout=500):
                return False
            disabled = await element.evaluate(
                """
                el => Boolean(
                    el.disabled ||
                    el.getAttribute('disabled') !== null ||
                    el.getAttribute('aria-disabled') === 'true' ||
                    el.closest('[aria-disabled="true"]') ||
                    el.closest('[disabled]')
                )
                """
            )
            if disabled:
                return False
            box = await element.bounding_box()
            return bool(box and box['width'] >= min_width and box['height'] >= min_height)
        except Exception:
            return False

    async def _find_by_aria_label_exact(self, context: Any) -> Optional[Any]:
        for label in ('نشر', 'Post', 'Publish', 'Share', 'مشاركة', 'Publier', 'Publicar'):
            try:
                element = context.locator(f"div[role='button'][aria-label='{label}'], button[aria-label='{label}']").first
                if await self._is_actionable(element):
                    return element
            except Exception:
                continue
        return None

    async def _find_by_aria_label_pattern(self, context: Any) -> Optional[Any]:
        try:
            elements = await context.locator("div[role='button'][aria-label], button[aria-label]").all()
            for element in elements[:80]:
                try:
                    label = str(await element.get_attribute('aria-label') or '').strip()
                    if (
                        self._publish_pattern.search(label)
                        and not self._reject_pattern.search(label)
                        and await self._is_actionable(element)
                    ):
                        return element
                except Exception:
                    continue
        except Exception:
            pass
        return None

    async def _find_by_text_exact(self, context: Any) -> Optional[Any]:
        for text in ('Post', 'Publish', 'Share', 'Send', 'نشر', 'انشر', 'مشاركة'):
            try:
                element = context.get_by_text(text, exact=True).first
                if await self._is_actionable(element, min_width=20, min_height=12):
                    return element
            except Exception:
                continue
        return None

    async def _find_by_text_pattern(self, context: Any) -> Optional[Any]:
        try:
            elements = await context.locator("div[role='button'], button, a[role='button']").all()
            for element in elements[:120]:
                try:
                    text = str(await element.inner_text(timeout=300) or '').strip()
                    if (
                        self._publish_pattern.search(text)
                        and not self._reject_pattern.search(text)
                        and await self._is_actionable(element)
                    ):
                        return element
                except Exception:
                    continue
        except Exception:
            pass
        return None

    async def _find_by_visual_position(self, context: Any) -> Optional[Any]:
        try:
            dialog_box = await context.bounding_box()
            if not dialog_box:
                return None
            elements = await context.locator("div[role='button'], button").all()
            candidates: List[Tuple[float, Any]] = []
            dialog_right = dialog_box['x'] + dialog_box['width']
            dialog_bottom = dialog_box['y'] + dialog_box['height']

            for element in elements[:80]:
                try:
                    if not await self._is_actionable(element):
                        continue
                    box = await element.bounding_box()
                    if not box:
                        continue
                    info = await element.evaluate(
                        """
                        el => {
                            const style = window.getComputedStyle(el);
                            return {
                                text: (el.innerText || '').trim(),
                                label: (el.getAttribute('aria-label') || '').trim(),
                                title: (el.getAttribute('title') || '').trim(),
                                testid: (el.getAttribute('data-testid') || '').trim(),
                                backgroundColor: style.backgroundColor || '',
                                color: style.color || '',
                                opacity: parseFloat(style.opacity || '1'),
                                cursor: style.cursor || '',
                                pointerEvents: style.pointerEvents || '',
                            };
                        }
                        """
                    )
                    full_text = ' '.join(
                        str(info.get(key) or '').strip()
                        for key in ('text', 'label', 'title', 'testid')
                        if str(info.get(key) or '').strip()
                    )
                    if full_text and self._reject_pattern.search(full_text):
                        continue

                    publish_text_match = bool(full_text and self._publish_pattern.search(full_text))
                    center_x = box['x'] + box['width'] / 2
                    center_y = box['y'] + box['height'] / 2
                    in_bottom_action_area = (
                        center_x >= dialog_box['x'] + (dialog_box['width'] * 0.45)
                        and center_y >= dialog_box['y'] + (dialog_box['height'] * 0.55)
                    )
                    if not in_bottom_action_area:
                        continue

                    bg_values = [int(value) for value in re.findall(r'\d+', str(info.get('backgroundColor') or ''))[:3]]
                    is_primary_blue = (
                        len(bg_values) >= 3
                        and bg_values[2] >= 120
                        and bg_values[2] >= bg_values[0] + 35
                        and bg_values[2] >= bg_values[1] + 10
                    )
                    is_prominent = (
                        float(info.get('opacity') or 1) >= 0.8
                        and str(info.get('pointerEvents') or '') != 'none'
                        and (
                            str(info.get('cursor') or '') == 'pointer'
                            or is_primary_blue
                        )
                    )
                    if not publish_text_match and not is_primary_blue:
                        continue
                    if not publish_text_match and (box['width'] < 44 or box['height'] < 24 or not is_prominent):
                        continue

                    dist_from_right = max(0.0, dialog_right - (box['x'] + box['width']))
                    dist_from_bottom = max(0.0, dialog_bottom - (box['y'] + box['height']))
                    right_score = 1.0 - min(1.0, dist_from_right / max(dialog_box['width'], 1))
                    bottom_score = 1.0 - min(1.0, dist_from_bottom / max(dialog_box['height'], 1))
                    score = (right_score + bottom_score) / 2
                    if publish_text_match:
                        score += 0.4
                    if is_primary_blue:
                        score += 0.2
                    candidates.append((score, element))
                except Exception:
                    continue
            if candidates:
                candidates.sort(key=lambda item: item[0], reverse=True)
                if candidates[0][0] >= 0.65:
                    return candidates[0][1]
        except Exception:
            pass
        return None

    async def _find_by_semantic_role(self, context: Any) -> Optional[Any]:
        selectors = (
            "button[type='submit']",
            "button[aria-label*='Post' i]",
            "button[aria-label*='Publish' i]",
            "button[aria-label*='نشر' i]",
            "div[role='button'][data-testid*='post' i]",
        )
        for selector in selectors:
            try:
                element = context.locator(selector).first
                if await self._is_actionable(element):
                    return element
            except Exception:
                continue
        return None


async def _find_and_click_publish_button_improved(
    page: Any,
    dialog: Any,
    timeout_ms: int = 15000,
    logger: Any = None,
) -> bool:
    """Click the composer publish button using the centralized finder."""
    finder = ButtonFinder(cast(Page, page))
    button = await finder.find_publish_button(dialog)
    if button is None:
        if logger:
            logger.warning("Could not find publish button on this composer step")
            try:
                buttons = await dialog.locator("button, div[role='button']").all()
                logger.warning(f"Found {len(buttons)} buttons in dialog, none matched publish patterns")
                for index, candidate in enumerate(buttons[:5]):
                    try:
                        text = await candidate.evaluate(
                            "el => (el.innerText || el.getAttribute('aria-label') || '').trim()"
                        )
                        logger.warning(f"  Button {index}: '{text}'")
                    except Exception:
                        pass
            except Exception:
                pass
        return False

    try:
        await button.click(timeout=min(max(timeout_ms, 250), 2000), force=True)
        if logger:
            logger.info('Clicked publish button via ButtonFinder')
        return True
    except Exception as exc:
        finder.forget_last_successful_strategy()
        try:
            if logger:
                logger.debug(f'ButtonFinder found a publish button but click failed: {exc}')
        except Exception:
            pass
    return False


async def _locator_compact_text(locator: Any, timeout: int = 700) -> str:
    try:
        if not await locator.is_visible(timeout=timeout):
            return ''
        return await locator.evaluate(
            "el => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim()"
        )
    except Exception:
        return ''


def _looks_like_profile_switch_dialog_text(text: str) -> bool:
    if not text:
        return False
    has_switch = re.search(
        r'\b(Switch|Switch now|See all profiles|Profile|Account)\b|'
        r'تبديل الملفات الشخصية|تبديل|بدّل|بدل|عرض كل الملفات الشخصية|الملفات الشخصية|الحساب|'
        r'التبديل إلى .+ للاستمتاع',
        text,
        re.I,
    )
    has_composer = re.search(
        r'Create post|Add to your post|What.s on your mind|Post to|Photo/video|Post settings|'
        r'إنشاء منشور|بم تفكر|إضافة إلى منشورك',
        text,
        re.I,
    )
    return bool(has_switch and not has_composer)


def _looks_like_composer_dialog_text(text: str) -> bool:
    if not text:
        return False
    if _looks_like_profile_switch_dialog_text(text):
        return False
    return bool(re.search(
        r'Create post|Add to your post|What.s on your mind|Post to|Share a thought|'
        r'Write something|Photo/video|Post settings|Next|Post|Publish|إنشاء منشور|بم تفكر|'
        r'إضافة إلى منشورك|اكتب شيئًا|اكتب شيئا|صورة/فيديو|الرمز التعبيري|شعور/نشاط|'
        r'إغلاق مربع حوار أداة الإنشاء|التالي|نشر',
        text,
        re.I,
    ))


def _looks_like_unsaved_post_prompt_text(text: str) -> bool:
    if not text:
        return False
    return bool(re.search(
        r'leave (?:page|site)|do you want to leave|discard (?:post|changes)|'
        r'unsaved changes|post (?:isn.?t|is not) finished|without finishing|'
        r'continue editing|keep editing|'
        r'هل تريد مغادرة|مغادرة الصفحة|لن تكتمل|لم تكتمل|لن يكتمل|لم يكتمل|'
        r'دون الإكمال|دون الاكمال|متابعة التعديل|متابعة التحرير|تجاهل المنشور',
        text,
        re.I,
    ))


_UNSAVED_POST_CONTINUE_PATTERN = re.compile(
    r'^\s*(Continue editing|Keep editing|Stay|Cancel|'
    r'متابعة التعديل|متابعة التحرير|تابع التعديل|الاستمرار في التعديل|'
    r'البقاء|ابق في الصفحة|إلغاء|الغاء)\s*$',
    re.I,
)
_UNSAVED_POST_REJECT_PATTERN = re.compile(
    r'^\s*(Leave|Leave page|Discard|Discard post|Delete|'
    r'مغادرة|غادر|تجاهل|حذف)\s*$',
    re.I,
)


def _is_facebook_security_failure(detail: str) -> bool:
    return bool(_FACEBOOK_SECURITY_FAILURE_RE.search(_security_relevant_detail(detail)))


async def _visible_composer_dialog(page: Page) -> Optional[Any]:
    try:
        dialogs = await page.locator("div[role='dialog']").all()
        candidates: List[Tuple[int, Any, str]] = []
        for dialog in dialogs:
            text_content = await _locator_compact_text(dialog)
            if not _looks_like_composer_dialog_text(text_content):
                continue
            score = 0
            if re.search(r'Create post|Add to your post|What.s on your mind|Post to|Share a thought', text_content, re.I):
                score += 80
            if re.search(r'\b(Next|Post|Publish|Share|نشر|التالي)\b', text_content, re.I):
                score += 60
            box = await dialog.bounding_box()
            if box:
                score += min(30, int((box['width'] * box['height']) / 20000))
            candidates.append((score, dialog, text_content[:120]))
        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            logger.debug(f"Visible composer dialog selected: {candidates[0][2]}")
            return candidates[0][1]
    except Exception:
        pass
    return None


async def _composer_dialog_or_inline_textbox_visible(page: Page) -> bool:
    if await _visible_composer_dialog(page):
        return True
    try:
        inline_count = await page.locator(
            "[contenteditable='true'][role='textbox']:not([aria-label*='Search' i]):not([placeholder*='Search' i]), "
            "div[aria-label*=\"What's on your mind\"] [contenteditable='true']"
        ).count()
        return inline_count > 0
    except Exception:
        return False


async def _mobile_composer_textbox_visible(page: Page) -> bool:
    try:
        textboxes = await page.locator(
            "textarea, "
            "[contenteditable='true'][role='textbox'], "
            "[contenteditable='true'], "
            "div[role='textbox'], "
            "div[aria-label*='What\\'s on your mind' i], "
            "div[aria-label*='Write' i]"
        ).all()
        for textbox in textboxes:
            try:
                if (
                    await textbox.is_visible(timeout=300)
                    and not await _mobile_textbox_is_non_post_input(textbox)
                ):
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False


async def _mobile_textbox_is_non_post_input(textbox: Any) -> bool:
    try:
        values = []
        for attr in ('aria-label', 'placeholder', 'name', 'title'):
            try:
                values.append(await textbox.get_attribute(attr) or '')
            except Exception:
                pass
        label_text = ' '.join(values)
        return bool(
            re.search(
                r'Comment as|Write a comment|Leave a comment|Post comment|Search',
                label_text,
                re.I,
            )
        )
    except Exception:
        return False


async def _profile_switch_dialog_visible(page: Page) -> bool:
    try:
        dialogs = await page.locator("div[role='dialog']").all()
        for dialog in reversed(dialogs):
            text_content = await _locator_compact_text(dialog)
            if not _looks_like_profile_switch_dialog_text(text_content):
                continue
            try:
                if await dialog.is_visible(timeout=300):
                    return True
            except Exception:
                return True
    except Exception:
        pass
    return False


async def _resume_unsaved_post_prompt(page: Page) -> bool:
    """Dismiss Facebook's leave/discard prompt by keeping the composer draft open."""
    try:
        dialogs = await page.locator("div[role='dialog']").all()
    except Exception:
        return False

    for dialog in reversed(dialogs):
        text_content = await _locator_compact_text(dialog)
        if not _looks_like_unsaved_post_prompt_text(text_content):
            continue
        try:
            if not await dialog.is_visible(timeout=300):
                continue
        except Exception:
            pass

        clicked_text = await _click_enabled_text_match(
            dialog,
            _UNSAVED_POST_CONTINUE_PATTERN,
            timeout=1600,
            reject_pattern=_UNSAVED_POST_REJECT_PATTERN,
        )
        if not clicked_text:
            for button_text in (
                'متابعة التعديل',
                'متابعة التحرير',
                'Continue editing',
                'Keep editing',
                'Stay',
                'Cancel',
                'الاستمرار في التعديل',
                'البقاء',
                'ابق في الصفحة',
                'إلغاء',
                'الغاء',
            ):
                try:
                    candidate = dialog.get_by_text(re.compile(f'^{re.escape(button_text)}$', re.I)).first
                    if await candidate.is_visible(timeout=400):
                        await candidate.click(timeout=1200)
                        clicked_text = button_text
                        break
                except Exception:
                    continue

        if clicked_text:
            logger.info(f'Facebook unsaved-post prompt detected; clicked "{clicked_text}" to keep editing.')
            await _wait_for_locator_hidden(dialog, timeout_ms=1200)
            await asyncio.sleep(0.2)
            return True

        logger.warning('Facebook unsaved-post prompt detected, but no safe keep-editing control was clickable.')
        return False

    return False


async def _wait_for_profile_switch_to_settle(
    page: Page,
    timeout_ms: int = 5000,
    initial_grace_seconds: float = 0.0,
) -> bool:
    if initial_grace_seconds > 0:
        grace_seconds = min(initial_grace_seconds, max(timeout_ms, 1) / 1000)
        logger.info(f'Waiting {grace_seconds:.1f}s for Facebook profile switch to apply.')
        await asyncio.sleep(grace_seconds)

    async def ui_settled() -> bool:
        if await _profile_switch_dialog_visible(page):
            return False
        try:
            visible_switches = await page.locator(
                "div[role='button']:visible, button:visible, a[role='button']:visible"
            ).evaluate_all(
                """
                elements => elements.filter(element => {
                    const text = (
                        element.innerText ||
                        element.getAttribute('aria-label') ||
                        element.getAttribute('title') ||
                        ''
                    ).replace(/\\s+/g, ' ').trim();
                    return /^(Switch|Switch now|تبديل|تبديل الآن|بدّل|بدّل الآن|بدل|بدل الآن)$/i.test(text);
                }).length
                """
            )
            return int(visible_switches or 0) == 0
        except Exception:
            return False

    if await ui_settled():
        await _wait_for_facebook_ui_ready(page, timeout=min(timeout_ms, 1500))
        return True

    settled = await _smart_wait(
        ui_settled,
        timeout_ms=timeout_ms,
        check_interval_ms=250,
    )
    if not settled:
        logger.warning('Profile switch UI remained open; leaving it for Facebook instead of forcing it closed.')
        return False
    await _wait_for_facebook_ui_ready(page, timeout=min(timeout_ms, 1500))
    await asyncio.sleep(0.8)
    logger.info("Profile switch detected: visible Switch controls disappeared.")
    return True


async def _wait_for_switch_to_settle(
    page: Page,
    timeout_seconds: int = 8,
    initial_grace_seconds: float = 0.0,
) -> bool:
    """Compatibility wrapper for page-actor switch settling."""
    return await _wait_for_profile_switch_to_settle(
        page,
        timeout_ms=max(1, timeout_seconds) * 1000,
        initial_grace_seconds=initial_grace_seconds,
    )


async def _find_composer_context(page: Page) -> Any:
    """
    Find the appropriate context for the composer (modal dialog or inline).

    Handles:
    - Modal dialogs: div[role='dialog'] (standard)
    - Inline composers: divs with 'What's on your mind' text
    - Fallback: the entire page if no specific container found

    Returns: A locator/page object to search within for composer controls
    """
    composer_text_pattern = re.compile(
        r'Create post|Post to|Add to your post|What.s on your mind|Share a thought',
        re.I,
    )
    comment_text_pattern = re.compile(r'Comment as|Post comment|Leave a comment|Write a comment', re.I)

    # Strategy 1: Try modal dialogs (most common). Facebook can keep hidden
    # Messenger/account dialogs in the DOM, and nested dialog-like nodes can expose
    # only "Close composer dialog". Score visible dialogs and prefer the full
    # composer container.
    try:
        dialogs = await page.locator("div[role='dialog']").all()
        scored_dialogs: List[Tuple[int, Any, str]] = []
        for dialog in dialogs:
            try:
                # 1. Structural Check: If the dialog contains the active editor textbox, it is the composer!
                textbox = dialog.locator("[contenteditable='true'][role='textbox']:not([aria-label*='Search' i])").first
                if await textbox.is_visible(timeout=500):
                    text_content = await _locator_compact_text(dialog, timeout=700)
                    if composer_text_pattern.search(text_content) and not comment_text_pattern.search(text_content):
                        logger.debug("Found modal dialog composer structurally via contenteditable textbox.")
                        return dialog

                # 2. Fallback to text check with higher timeout
                text_content = await _locator_compact_text(dialog, timeout=1200)
                if not _looks_like_composer_dialog_text(text_content):
                    continue
                score = 0
                if composer_text_pattern.search(text_content):
                    score += 80
                if re.search(r'\b(Next|Post|Publish|Share|نشر|التالي)\b', text_content, re.I):
                    score += 60
                if re.search(r'Close composer dialog', text_content, re.I):
                    score += 20
                box = await dialog.bounding_box()
                if box:
                    score += min(30, int((box['width'] * box['height']) / 20000))
                scored_dialogs.append((score, dialog, text_content[:120]))
            except Exception:
                continue
        if scored_dialogs:
            scored_dialogs.sort(key=lambda item: item[0], reverse=True)
            logger.debug(f"Found modal dialog composer by text: {scored_dialogs[0][2]}")
            return scored_dialogs[0][1]
    except Exception:
        pass

    # Strategy 2: Try inline composer - look for "What's on your mind?" with better selector
    try:
        composer_div = page.locator(
            "div:has(span:has-text(\"What's on your mind\")):has(div[role='button'][aria-label*='Photo'])"
        ).first
        await composer_div.wait_for(state='visible', timeout=1000)
        text_content = await _locator_compact_text(composer_div, timeout=700)
        if comment_text_pattern.search(text_content):
            raise RuntimeError('Inline composer candidate is a comment surface')
        logger.debug("Found inline composer via 'What's on your mind' and Photo button")
        return composer_div
    except Exception:
        pass

    # Strategy 3: Look for the inline composer by checking for both placeholder and action buttons
    try:
        # Look for the container that has both "What's on your mind" AND Photo/video button
        composer_area = page.locator(
            "div:has(span:has-text(\"What's on your mind\")) "
            ":has(div[aria-label*='Photo'])"
        ).first
        await composer_area.wait_for(state='visible', timeout=1000)
        logger.debug("Found inline composer via placeholder + action buttons")
        return composer_area
    except Exception:
        pass

    # Strategy 4: Try finding by contenteditable textbox (appears after clicking composer)
    try:
        textboxes = await page.locator("[contenteditable='true'][role='textbox'], textarea, [role='textbox']").all()
        for textbox in textboxes:
            try:
                if not await textbox.is_visible(timeout=300):
                    continue
                aria_label = await textbox.get_attribute('aria-label') or ''
                if re.search(r'comment|search', aria_label, re.I):
                    continue
                context_text = await textbox.evaluate(
                    """
                    el => {
                        const dialog = el.closest('[role="dialog"]');
                        const container = dialog || el.closest('form') || el.parentElement;
                        return ((container && (container.innerText || container.textContent)) || '').replace(/\\s+/g, ' ').trim();
                    }
                    """
                )
                if context_text and composer_text_pattern.search(str(context_text)) and not comment_text_pattern.search(str(context_text)):
                    dialog = textbox.locator("xpath=ancestor-or-self::div[@role='dialog'][1]").first
                    if await dialog.count() > 0:
                        logger.debug("Found composer textbox inside create-post dialog.")
                        return dialog
            except Exception:
                continue
    except Exception:
        pass

    # Do not fall back to page-wide textboxes: comment boxes look like composers
    # and can cause the bot to post readiness text as comments.
    logger.debug("Could not locate a real create-post composer context")
    return page.locator("div[role='dialog']:has-text('Create post'), div[role='dialog']:has-text('Add to your post')").last


async def _wait_for_composer_action_controls(dialog: Any, timeout: int = 30000) -> None:
    """Wait until Facebook's composer/settings step exposes Next/Post controls."""
    deadline = asyncio.get_running_loop().time() + (timeout / 1000)
    action_pattern = re.compile(r'^(Next|Continue|Post|Publish|Share|Post now|Publish now|التالي|متابعة|نشر|مشاركة)$', re.I)
    while asyncio.get_running_loop().time() < deadline:
        try:
            text_content = await dialog.evaluate(
                "el => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim()"
            )
        except Exception:
            text_content = ''
        try:
            if await _has_enabled_text_match(dialog, action_pattern):
                return
        except Exception:
            pass
        if text_content and 'Post settings' not in text_content:
            return
        await asyncio.sleep(1.0)


async def _submit_desktop_composer(page: Page) -> bool:
    """Submit Facebook's desktop composer across common UI variants."""
    post_pattern = re.compile(
        r'^(Post|Publish|Share|Post now|Publish now|نشر|مشاركة)$',
        re.I,
    )
    next_pattern = re.compile(r'^(Next|Continue|التالي|متابعة)$', re.I)
    action_pattern = re.compile(
        r'^(Post|Publish|Share|Post now|Publish now|Next|Continue|نشر|مشاركة|التالي|متابعة)$',
        re.I,
    )
    reject_submit_pattern = re.compile(
        r'Add to your post|Photo/video|Feeling|Check in|More|Emoji|'
        r'Cancel|Close|Back|Previous|Audience|Public|إضافة|صورة|فيديو',
        re.I,
    )
    deadline = asyncio.get_running_loop().time() + POST_SUBMIT_ACTION_TIMEOUT_SECONDS

    def remaining_timeout(default_ms: int, minimum_ms: int = 250) -> int:
        remaining_ms = int((deadline - asyncio.get_running_loop().time()) * 1000)
        if remaining_ms <= 0:
            return 1
        return max(minimum_ms, min(default_ms, remaining_ms))

    for _ in range(4):
        if asyncio.get_running_loop().time() >= deadline:
            logger.warning(f'Composer submit search exceeded {POST_SUBMIT_ACTION_TIMEOUT_SECONDS}s.')
            return False
        if await _resume_unsaved_post_prompt(page):
            continue
        if await _profile_switch_dialog_visible(page):
            logger.warning('Profile switch dialog appeared during submit; aborting this composer attempt.')
            return False
        # Find the appropriate context (modal or inline composer)
        dialog = await _find_composer_context(page)
        wait_timeout = remaining_timeout(3500)
        if wait_timeout <= 0:
            return False
        await _wait_for_composer_action_controls(dialog, timeout=wait_timeout)

        # First composer step must advance with Next. Do this before any publish
        # finder so attachment controls such as "Add to your post" are never
        # treated as submit actions.
        clicked_text = await _click_enabled_text_match(
            dialog,
            next_pattern,
            timeout=remaining_timeout(1600),
            reject_pattern=reject_submit_pattern,
        )
        if not clicked_text:
            # Fast path: use aria-label selectors (confirmed from live DOM observation)
            try:
                for aria_label in ["التالي", "Next", "Continue", "متابعة"]:
                    btn = dialog.locator(f"div[role='button'][aria-label='{aria_label}']").first
                    visible_timeout = remaining_timeout(600)
                    if visible_timeout <= 0:
                        return False
                    if await _wait_for_element_state(btn, 'visible', visible_timeout):
                        await btn.click(timeout=remaining_timeout(1200))
                        clicked_text = aria_label
                        logger.info(f"✅ Clicked composer next via aria-label: '{aria_label}'")
                        break
            except Exception as e:
                logger.debug(f"aria-label next locator error: {e}")
        if not clicked_text:
            try:
                for pattern_text in ["Next", "Continue", "التالي", "متابعة"]:
                    candidate = dialog.get_by_text(re.compile(f"^{pattern_text}$", re.I)).first
                    visible_timeout = remaining_timeout(700)
                    if visible_timeout <= 0:
                        return False
                    if await candidate.is_visible(timeout=visible_timeout):
                        await candidate.scroll_into_view_if_needed(timeout=remaining_timeout(700))
                        await candidate.click(timeout=remaining_timeout(1200))
                        clicked_text = pattern_text
                        logger.info(f"✅ Clicked composer next control: '{pattern_text}'")
                        break
            except Exception as e:
                logger.debug(f"Direct next locator fallback error: {e}")
        if clicked_text:
            logger.info(f'Clicked Facebook composer intermediate control: {clicked_text}')
            # Dynamic wait: wait for publish button to appear instead of static 2.5s
            try:
                await page.wait_for_function(
                    """() => {
                        const buttons = document.querySelectorAll('div[role="button"][aria-label]');
                        return Array.from(buttons).some(b => /^(Post|Publish|نشر|مشاركة)$/i.test(b.getAttribute('aria-label') || b.innerText?.trim()));
                    }""",
                    timeout=3500,
                )
            except Exception:
                await asyncio.sleep(0.4)
            if await _resume_unsaved_post_prompt(page):
                continue
            continue

        # On the final settings step, prefer exact publish controls before broad
        # style/position scans. This avoids slow page-wide scans and attachment
        # controls.

        # Fast path: aria-label targeting (confirmed from live DOM: aria-label='نشر')
        try:
            for aria_label in ["نشر", "Post", "Publish", "Share", "مشاركة"]:
                btn = dialog.locator(f"div[role='button'][aria-label='{aria_label}']").first
                visible_timeout = remaining_timeout(600)
                if visible_timeout <= 0:
                    return False
                if await _wait_for_element_state(btn, 'visible', visible_timeout):
                    await btn.click(timeout=remaining_timeout(1200))
                    logger.info(f"✅ Clicked composer publish via aria-label: '{aria_label}'")
                    return True
        except Exception as e:
            logger.debug(f"aria-label publish locator error: {e}")

        clicked_text = await _click_enabled_text_match(
            dialog,
            post_pattern,
            timeout=remaining_timeout(1800),
            reject_pattern=reject_submit_pattern,
        )
        if clicked_text:
            logger.info(f'Clicked Facebook composer publish control: {clicked_text}')
            return True

        # Try the robust publish button finder only when no Next step is visible
        # and exact publish matching did not find a control.
        posted = await _find_and_click_publish_button_improved(
            page,
            dialog,
            timeout_ms=remaining_timeout(2500),
            logger=logger,
        )
        if posted:
            logger.info('Clicked Facebook composer publish control via improved button finder')
            return True

        # Last resort remains constrained to the composer dialog to avoid
        # clicking background feed controls.
        clicked_text = await _click_enabled_text_match(
            dialog,
            action_pattern,
            timeout=remaining_timeout(1200),
            reject_pattern=reject_submit_pattern,
        )

        if not clicked_text:
            if await _resume_unsaved_post_prompt(page):
                continue
            return False

        logger.info(f'Clicked Facebook composer intermediate control: {clicked_text}')
        if post_pattern.search(clicked_text):
            return True
        await asyncio.sleep(0.6)

    return False


_POST_PUBLISH_BLOCKING_POPUP_RE = re.compile(
    r'Make it easier to contact you|Add a WhatsApp button|Add WhatsApp button|WhatsApp|'
    r'Speak With People Directly|people to speak with you|Call Now|Add Button|'
    r'Hosting an event|Publish Original Post|'
    r'أضف زر واتساب|واتساب|ليس الآن',
    re.I,
)
_POST_PUBLISH_POPUP_ACTION_RE = re.compile(
    r'^\s*(Publish Original Post|نشر المنشور الأصلي)\s*$',
    re.I,
)
_POST_PUBLISH_POPUP_DISMISS_RE = re.compile(
    r'^\s*(Not now|No thanks|Maybe later|Later|Skip|Done|Close|'
    r'ليس الآن|لاحق(?:اً)?|لا شكرًا|تخطي|تم|إغلاق)\s*$',
    re.I,
)


async def _dismiss_post_publish_blocking_popups(page: Page, timeout_ms: int = 1200) -> bool:
    """Dismiss Facebook business CTAs that can block publish confirmation."""
    try:
        dialogs = await page.locator("div[role='dialog']").all()
    except Exception:
        dialogs = []

    for dialog in reversed(dialogs):
        text_content = await _locator_compact_text(dialog, timeout=min(timeout_ms, 700))
        if not text_content or not _POST_PUBLISH_BLOCKING_POPUP_RE.search(text_content):
            continue
        clicked_text = await _click_enabled_text_match(
            dialog,
            _POST_PUBLISH_POPUP_ACTION_RE,
            timeout=timeout_ms,
        )
        if not clicked_text:
            clicked_text = await _click_enabled_text_match(
                dialog,
                _POST_PUBLISH_POPUP_DISMISS_RE,
                timeout=timeout_ms,
            )
        if not clicked_text:
            try:
                close_button = dialog.locator("[aria-label='Close'], [aria-label='إغلاق']").first
                if await close_button.is_visible(timeout=min(timeout_ms, 700)):
                    await close_button.click(timeout=timeout_ms)
                    clicked_text = 'Close'
            except Exception:
                pass
        if clicked_text:
            logger.info(f'Facebook post-publish popup dismissed: {clicked_text}')
            await asyncio.sleep(0.4)
            return True
        logger.warning(
            f'Facebook post-publish popup detected but no safe dismiss/action button was clickable: '
            f'{text_content[:160]}'
        )
        return False

    return False


async def _dismiss_common_facebook_popups(page: Page) -> None:
    if await _dismiss_post_publish_blocking_popups(page):
        return
    await _click_first_available(
        page,
        [
            lambda: page.get_by_role('button', name=re.compile(r'Not now|Later|Close|Skip|Done|ليس الآن|لاحق|إغلاق|تخطي', re.I)),
            "[aria-label='Close']",
            "[aria-label='إغلاق']",
        ],
        timeout=1500,
    )


async def _visible_text(locator: Any, timeout: int = 700) -> str:
    try:
        if not await locator.is_visible(timeout=timeout):
            return ''
        return (await locator.inner_text(timeout=timeout)).strip()
    except Exception:
        return ''


async def _click_pages_portal_create_post(page: Page, page_name: str) -> bool:
    """
    Click the Pages Portal Create post button that belongs to page_name.

    Facebook's Pages Portal cards are deeply nested and class names are unstable, so
    this uses visible text plus DOM ancestor context instead of CSS class selectors.
    """
    create_pattern = re.compile(r'^\s*(Create post|إنشاء منشور)\s*$', re.I)
    target_name = ' '.join(page_name.lower().split())

    async def candidate_score(button: Any) -> int:
        try:
            return int(await button.evaluate(
                """
                (el, targetName) => {
                    const normalizedTarget = targetName.toLowerCase();
                    let score = 0;
                    let node = el;
                    for (let depth = 0; node && depth < 9; depth += 1, node = node.parentElement) {
                        const rect = node.getBoundingClientRect();
                        const text = (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
                        const lower = text.toLowerCase();
                        if (!text || rect.width <= 0 || rect.height <= 0) continue;
                        if (lower.includes(normalizedTarget)) score = Math.max(score, 100 - depth * 8);
                        if (/Professional dashboard|Insights|Planner|Monetization|Ad Center/i.test(text)) score -= 18;
                    }
                    return score;
                }
                """,
                target_name,
            ))
        except Exception:
            return -100

    for selector in (
        "div[role='button'], button, a[role='button']",
        "a, span, div",
    ):
        try:
            handles = await page.locator(selector).all()
        except Exception:
            continue

        scored: List[Tuple[int, Any, str]] = []
        for handle in handles:
            try:
                text = await handle.evaluate(
                    "el => (el.innerText || el.getAttribute('aria-label') || el.textContent || '').trim()"
                )
                if not text or not create_pattern.search(text):
                    continue
                if not await handle.is_visible(timeout=400):
                    continue
                score = await candidate_score(handle)
                scored.append((score, handle, text))
            except Exception:
                continue

        scored.sort(key=lambda item: item[0], reverse=True)
        for score, handle, text in scored:
            if score < 40:
                logger.debug(
                    f'Pages Portal: rejecting low-confidence Create post candidate '
                    f'for "{page_name}" (score={score}, text="{text}")'
                )
                continue
            try:
                logger.info(f'📄 Pages Portal: clicking "{text}" for "{page_name}" (score={score})')
                await handle.scroll_into_view_if_needed(timeout=1500)
                await handle.click(timeout=3000)
                return True
            except Exception as exc:
                logger.debug(f'Pages Portal: Create post candidate click failed: {exc}')

    return False


async def _open_visible_page_composer(page: Page) -> bool:
    """Open a page-profile composer when the portal lands on the Page instead of a modal."""
    open_pattern = re.compile(
        r"^(What's on your mind\?|Share a thought\.\.\.|Create post|Write something|"
        r"بم تفكر|إنشاء منشور|اكتب)",
        re.I,
    )
    blocked_pattern = re.compile(r'Boost|Promote|Advertise|Ad Center|Create ad|إعلان|ترويج', re.I)

    for attempt in range(1, 4):
        logger.info(f"Attempting to open page composer (attempt {attempt}/3)...")
        clicked = await _click_safe_composer_button(page, open_pattern, blocked_pattern, timeout=5000)
        if not clicked:
            clicked = bool(await _click_enabled_text_match(page, open_pattern, timeout=3000))

        if clicked:
            dialog_opened = await _smart_wait(
                lambda: _composer_dialog_or_inline_textbox_visible(page),
                timeout_ms=5000,
                check_interval_ms=200,
            )
            if dialog_opened:
                logger.info("✅ Page composer opened successfully!")
                return True
            logger.warning(f"Composer click registered but composer did not open on attempt {attempt}.")

        # Wait a bit before next attempt to allow JS hydration
        await asyncio.sleep(2.0)

    return False


async def _wait_for_pages_portal_composer(page: Page, timeout_ms: int = 9000) -> bool:
    return await _smart_wait(
        lambda: _composer_dialog_or_inline_textbox_visible(page),
        timeout_ms=timeout_ms,
        check_interval_ms=200,
    )


async def _handle_pages_portal_profile_switch(page: Page, page_name: str) -> bool:
    """Complete Facebook's page-actor switch prompt without forcing dialogs closed."""
    switched = False
    try:
        if await _wait_for_pages_portal_composer(page, timeout_ms=1200):
            return False
        if await _profile_switch_dialog_visible(page):
            selected = await _click_profile_switch_option(page, page_name, timeout=3500)
            if selected:
                switched = await _wait_for_profile_switch_to_settle(
                    page,
                    timeout_ms=8000,
                    initial_grace_seconds=POST_PROFILE_SWITCH_SETTLE_GRACE_SECONDS,
                )
        if not switched:
            switch_button = page.get_by_role(
                "button",
                name=re.compile(r'^(Switch|Switch now|تبديل|بدل|بدّل)$', re.I),
            ).first
            if await switch_button.is_visible(timeout=2500):
                logger.info('📄 Pages Portal: "Switch profiles" modal detected, clicking Switch')
                await switch_button.click(timeout=3000)
                switched = await _wait_for_profile_switch_to_settle(
                    page,
                    timeout_ms=8000,
                    initial_grace_seconds=POST_PROFILE_SWITCH_SETTLE_GRACE_SECONDS,
                )
    except Exception as exc:
        logger.debug(f'📄 Pages Portal: profile switch prompt not completed: {exc}')
        return False
    if switched:
        logger.info('📄 Pages Portal: profile switch completed')
        await _wait_for_facebook_ui_ready(page, timeout=3500)
        await _dismiss_common_facebook_popups(page)
    return switched


async def _click_profile_switch_option(page: Page, page_name: str, timeout: int = 4500) -> bool:
    if not page_name:
        return False
    page_name_pattern = re.compile(re.escape(page_name), re.I)
    deadline = asyncio.get_running_loop().time() + (timeout / 1000)
    while asyncio.get_running_loop().time() < deadline:
        try:
            dialogs = await page.locator("div[role='dialog']").all()
        except Exception:
            dialogs = []
        for dialog in reversed(dialogs):
            try:
                if not await dialog.is_visible(timeout=400):
                    continue
            except Exception:
                continue
            clicked = await _click_enabled_text_match(
                dialog,
                page_name_pattern,
                timeout=900,
            )
            if clicked:
                logger.info(f"Page Switch: selected target page from switch dialog: '{clicked}'")
                return True
            try:
                text_target = dialog.get_by_text(page_name_pattern).last
                if await text_target.is_visible(timeout=400):
                    await text_target.scroll_into_view_if_needed(timeout=800)
                    await text_target.click(timeout=1200)
                    logger.info(f"Page Switch: selected target page by text fallback: '{page_name}'")
                    return True
            except Exception:
                pass
        await asyncio.sleep(0.2)
    return False


async def _click_onscreen_switch_button(page: Page, page_name: str = '') -> bool:
    """Detect and click any prominent 'Switch Now' or 'Switch' button in the page content itself."""
    try:
        if page_name and await _profile_switch_dialog_visible(page):
            selected = await _click_profile_switch_option(page, page_name)
            if selected:
                return await _wait_for_profile_switch_to_settle(
                    page,
                    timeout_ms=6000,
                    initial_grace_seconds=POST_PROFILE_SWITCH_SETTLE_GRACE_SECONDS,
                )
            return False
        direct_switch_patterns = [
            "Switch Now", "Switch profile", "تبديل الآن", "بدّل الآن", "بدل الآن"
        ]
        generic_switch_patterns = ["Switch", "تبديل", "بدّل", "بدل"]
        for pattern in direct_switch_patterns + generic_switch_patterns:
            loc = page.locator(f"div[role='button']:has-text('{pattern}'), button:has-text('{pattern}')").first
            if await loc.is_visible(timeout=1000):
                try:
                    in_dialog = await loc.evaluate("el => Boolean(el.closest('[role=\"dialog\"]'))")
                    if in_dialog:
                        logger.debug(f"Skipping switch button inside dialog: '{pattern}'")
                        continue
                except Exception:
                    pass
                logger.info(f"Page Switch: found on-screen switch button with text '{pattern}'. Clicking it...")
                await loc.click(timeout=3000)
                await _wait_for_facebook_ui_ready(page, timeout=4000)
                if pattern in generic_switch_patterns and page_name:
                    selected = await _click_profile_switch_option(page, page_name)
                    if not selected:
                        logger.info(
                            f"Page Switch: generic switch button did not expose target page '{page_name}'."
                        )
                        await _wait_for_profile_switch_to_settle(
                            page,
                            timeout_ms=2000,
                            initial_grace_seconds=POST_PROFILE_SWITCH_SETTLE_GRACE_SECONDS,
                        )
                        return False
                return await _wait_for_profile_switch_to_settle(
                    page,
                    timeout_ms=6000,
                    initial_grace_seconds=POST_PROFILE_SWITCH_SETTLE_GRACE_SECONDS,
                )
    except Exception as e:
        logger.debug(f"Error checking on-screen switch button: {e}")
    return False


async def _switch_to_page_profile(page: Page, page_name: str = '') -> bool:
    """Switch the active Facebook actor to the target page when the account menu exposes it."""
    if page_name == 'profile.php':
        page_name = ''
    page_name_pattern = re.compile(re.escape(page_name), re.I) if page_name else re.compile(r'Premium Service', re.I)

    logger.info(f"Attempting switch to page profile: '{page_name or 'Premium Service'}'")

    menu_selectors = [
        "div[role='button'][aria-label*='profile' i]",
        "div[role='button'][aria-label*='Profile' i]",
        "div[role='button'][aria-label*='Account' i]",
        "div[role='button'][aria-label*='ملف' i]",
        "div[role='button'][aria-label*='شخصي' i]",
        "div[role='button'][aria-label*='الحساب' i]",
        "div[aria-label*='Your profile' i]",
        "[aria-label='Your profile']",
        "[aria-label='Account Controls and Settings']"
    ]

    menu_clicked = False
    for sel in menu_selectors:
        try:
            target = page.locator(sel).first
            if await target.is_visible(timeout=500):
                logger.info(f"Clicking account menu selector: '{sel}'")
                await target.click(timeout=1500)
                menu_clicked = True
                break
        except Exception as e:
            logger.debug(f"Account menu selector '{sel}' failed: {e}")
            continue

    if not menu_clicked:
        logger.warning("Could not find or click the top-right Your Profile account menu button.")
        return False

    await _smart_wait(
        lambda: page.locator(
            f"span:has-text('{page_name}'), div[role='button']:has-text('{page_name}')"
            if page_name
            else "span:has-text('Premium Service'), div[role='button']:has-text('Premium Service')"
        ).count(),
        timeout_ms=1500,
        check_interval_ms=150,
    )

    clicked = ''
    for selector in (
        f"span:has-text('{page_name}')" if page_name else "span:has-text('Premium Service')",
        f"div[role='button']:has-text('{page_name}')" if page_name else "div[role='button']:has-text('Premium Service')",
    ):
        try:
            target = page.locator(selector).last
            if await target.is_visible(timeout=1500):
                await target.click(timeout=1500)
                clicked = page_name or 'Premium Service'
                logger.info(f"Clicked profile switcher element via selector: '{selector}'")
                break
        except Exception as e:
            logger.debug(f"Selector '{selector}' failed to switch actor: {e}")
            continue

    if not clicked:
        logger.info("Using text match fallback for page profile switch...")
        clicked = await _click_enabled_text_match(page, page_name_pattern, timeout=5000) or ''

    if not clicked:
        logger.warning(f"Could not find switcher option for '{page_name or 'Premium Service'}' in account menu.")
        return False

    logger.info("Waiting for profile switch reload...")
    if not await _wait_for_facebook_ui_ready(page, timeout=4000):
        await _smart_wait(
            lambda: page.locator("[role='progressbar'], [aria-busy='true']").count() == 0,
            timeout_ms=1000,
            check_interval_ms=150,
        )
    logger.info(f'Successfully switched Facebook actor to page profile: {clicked}')
    if not await _wait_for_switch_to_settle(
        page,
        timeout_seconds=8,
        initial_grace_seconds=POST_PROFILE_SWITCH_SETTLE_GRACE_SECONDS,
    ):
        await _click_onscreen_switch_button(page, page_name)
    return True


async def _click_safe_composer_button(page: Page, create_pattern: re.Pattern, reject_pattern: re.Pattern, timeout: int = 6000) -> bool:
    deadline = asyncio.get_running_loop().time() + (timeout / 1000)
    selector = "div[role='button'], button, a[role='button'], a[href], span[role='button']"

    while asyncio.get_running_loop().time() < deadline:
        try:
            button_data = await page.evaluate(f'''
                () => {{
                    const els = Array.from(document.querySelectorAll("{selector}"));
                    return els.map((el, i) => {{
                        return {{
                            index: i,
                            text: (el.innerText || '').trim(),
                            aria: (el.getAttribute('aria-label') || '').trim()
                        }};
                    }}).filter(b => b.text || b.aria);
                }}
            ''')

            locators = page.locator(selector)

            for b in button_data:
                full_text = f"{b['text']} | {b['aria']}"
                if create_pattern.search(full_text):
                    if reject_pattern.search(full_text):
                        logger.debug(f"Composer rejected candidate: '{full_text}'")
                        continue

                    target = locators.nth(b['index'])
                    try:
                        if not await target.is_visible(timeout=500):
                            continue
                    except Exception:
                        continue

                    logger.info(f"Composer CLICKING candidate: '{full_text}'")
                    try:
                        await target.click(timeout=1500, force=True)
                        return True
                    except Exception as exc:
                        logger.debug(f"Failed to click candidate {b['index']}: {exc}")
                        continue
        except Exception as exc:
            logger.debug(f"Safe composer button search exception: {exc}")
        await asyncio.sleep(0.5)
    return False

async def _open_desktop_composer(page: Page, target_url: str, page_name: str = '') -> bool:
    page_label = _derive_page_name(target_url, page_name) or page_name or target_url
    create_pattern = re.compile(
        r"Create post|Write something|What's on your mind|Share a thought|"
        r"Post something|Start a post|"
        r"إنشاء منشور|اكتب شيئًا|اكتب شيئا|بم تفكر",
        re.I,
    )
    blocked_pattern = re.compile(
        r'Boost|Promote|Ad center|Create ad|Advertise|Ad\b|\bAd\b|Actions for this post|'
        r'Manage posts|post it on your profile|Hide post|Boost post|إعلان|ترويج|'
        r'الإجراءات لهذا المنشور|إجراءات هذا المنشور|تعليق|Comment',
        re.I,
    )
    routes = _facebook_posts_routes(target_url)[:POST_COMPOSER_ROUTE_LIMIT]

    for route_idx, route in enumerate(routes, start=1):
        try:
            _post_step(page_label, 'Desktop route open', f'route={route_idx}/{len(routes)} url={_safe_log_url(route)}')
            await page.goto(route, wait_until='domcontentloaded', timeout=30000)
            fatal_detail = await _facebook_navigation_fatal_block_detail(page)
            if fatal_detail:
                _post_step(page_label, 'Desktop route fatal block', f'route={route_idx} reason={fatal_detail[:120]}')
                raise RuntimeError(fatal_detail)
            await _smart_wait(
                lambda: page.locator('div[role="main"], h1').count(),
                timeout_ms=3000,
                check_interval_ms=200,
            )
            await _dismiss_common_facebook_popups(page)
            if await _page_looks_logged_out(page):
                logger.warning(f'Facebook target page is logged out or blocked for target={route}')
                _post_step(page_label, 'Desktop route blocked', f'route={route_idx} reason=logged_out')
                return False
            if route == routes[0]:
                switched = await _switch_to_page_profile(page, page_name)
                _post_step(page_label, 'Actor switch result', f'route={route_idx} switched={switched}')
                if switched:
                    logger.info(f'Desktop composer: page profile switched, reloading page')
                    await _wait_for_profile_switch_to_settle(page, timeout_ms=6000)
                    await page.goto(route, wait_until='domcontentloaded', timeout=30000)
                    fatal_detail = await _facebook_navigation_fatal_block_detail(page)
                    if fatal_detail:
                        _post_step(page_label, 'Desktop route fatal block', f'route={route_idx} reason={fatal_detail[:120]}')
                        raise RuntimeError(fatal_detail)
                    await _wait_for_facebook_ui_ready(page, timeout=3500)
                    await _dismiss_common_facebook_popups(page)
                    await _wait_for_profile_switch_to_settle(page, timeout_ms=3000)
                    await _wait_for_facebook_ui_ready(page, timeout=2000)

            # Click on-screen "Switch Now" if it appears
            clicked_onscreen = await _click_onscreen_switch_button(page, page_name)
            if clicked_onscreen:
                _post_step(page_label, 'Clicked visible switch button', f'route={route_idx}')
            if clicked_onscreen:
                await _wait_for_profile_switch_to_settle(page, timeout_ms=6000)

            # Check if composer modal is already open (e.g. from ?modal=composer parameter)
            try:
                if await _visible_composer_dialog(page):
                    _post_step(page_label, 'Desktop composer visible', f'route={route_idx} source=auto_dialog')
                    return True
            except Exception:
                pass

            max_attempts = 2
            for attempt in range(max_attempts):
                # Click on-screen "Switch Now" if it appears during the attempts
                if await _click_onscreen_switch_button(page, page_name):
                    await _wait_for_profile_switch_to_settle(page, timeout_ms=6000)
                    try:
                        if await _visible_composer_dialog(page):
                            logger.info("Desktop composer: dialog visible after onscreen switch click in loop.")
                            return True
                    except Exception:
                        pass

                _post_step(page_label, 'Find create button', f'route={route_idx} attempt={attempt + 1}/{max_attempts}')

                # Try scrolling to top to reveal composer if needed
                if attempt > 0:
                    await page.evaluate('window.scrollTo(0, 0)')
                    await asyncio.sleep(0.4)

                create_clicked = await _click_safe_composer_button(
                    page,
                    create_pattern,
                    reject_pattern=blocked_pattern,
                    timeout=POST_COMPOSER_BUTTON_TIMEOUT_MS,
                )
                if create_clicked:
                    _post_step(page_label, 'Create button clicked', f'route={route_idx} attempt={attempt + 1}')
                    await _wait_for_facebook_ui_ready(page, timeout=2000)
                    if _is_ad_flow_url(page.url):
                        logger.warning(f'Facebook composer candidate opened ad flow instead of composer: {page.url}')
                        try:
                            await page.go_back(wait_until='domcontentloaded', timeout=8000)
                        except Exception:
                            pass
                        break  # Break out of the attempt loop, try next route
                    dialog_opened = await _smart_wait(
                        lambda: _composer_dialog_or_inline_textbox_visible(page),
                        timeout_ms=POST_COMPOSER_DIALOG_TIMEOUT_MS,
                        check_interval_ms=200,
                    )
                    if dialog_opened:
                        if await _visible_composer_dialog(page):
                            _post_step(page_label, 'Desktop composer visible', f'route={route_idx} source=create_button_dialog')
                            return True
                        textbox = page.locator("[contenteditable='true'][role='textbox']:not([aria-label*='Search' i]):not([placeholder*='Search' i]), div[aria-label*=\"What's on your mind\"] [contenteditable='true']").first
                        if await textbox.is_visible(timeout=2000):
                            _post_step(page_label, 'Desktop composer visible', f'route={route_idx} source=inline_textbox')
                            return True
                    if await _profile_switch_dialog_visible(page):
                        logger.warning('Profile switch dialog appeared instead of composer; leaving it open and failing desktop route.')
                        return False
                    _post_step(page_label, 'Dialog rejected', f'route={route_idx} reason=not_composer')

                # Wait briefly before retrying; Facebook often renders controls after scroll.
                await _wait_for_facebook_ui_ready(page, timeout=1200)
                await page.evaluate('window.scrollBy(0, 550)')
                await asyncio.sleep(0.4)
        except Exception as exc:
            if _is_facebook_security_failure(str(exc)) or 'Cookies expired or invalid' in str(exc):
                raise
            logger.warning(f'Facebook composer route failed for {route}: {exc}')
            _post_step(page_label, 'Desktop route failed', f'route={route_idx} error={type(exc).__name__}')
            continue
    _post_step(page_label, 'Desktop composer unavailable', f'routes_tried={len(routes)}')
    return False


async def _create_post_mobile(
    page: Page,
    target_url: str,
    caption: str,
    post_type: str = 'post',
    media_url: Optional[str] = None,
) -> Tuple[bool, str]:
    """Create a post using Facebook's mobile interface (m.facebook.com)."""
    if post_type in {'image', 'video'} and not media_url:
        return False, f'Missing {post_type} media URL; refusing to publish a media post without media.'

    parsed = urlparse(target_url)

    # For profile.php URLs, extract the ID and use the direct format
    # m.facebook.com redirects profile.php URLs, so we need special handling
    path_parts = parsed.path.split('/')

    if 'profile.php' in parsed.path:
        # For profile.php?id=XXXXX, keep the path as-is
        mobile_url = urlunparse(('https', 'm.facebook.com', parsed.path or '/', '', parsed.query, ''))
        mbasic_url = urlunparse(('https', 'mbasic.facebook.com', parsed.path or '/', '', parsed.query, ''))
    else:
        # For regular pages like /PageName, keep as-is
        mobile_url = urlunparse(('https', 'm.facebook.com', parsed.path or '/', '', parsed.query, ''))
        mbasic_url = urlunparse(('https', 'mbasic.facebook.com', parsed.path or '/', '', parsed.query, ''))

    tmp_path = ''
    try:
        try:
            await page.set_viewport_size({'width': 390, 'height': 844})
            await page.set_extra_http_headers({'User-Agent': _MOBILE_FACEBOOK_USER_AGENT})
        except Exception as exc:
            logger.debug(f'Mobile composer: could not apply mobile page hints: {exc}')

        logger.info(f'Mobile composer: navigating to {mobile_url}')
        await page.goto(mobile_url, wait_until='domcontentloaded', timeout=45000)
        fatal_detail = await _facebook_navigation_fatal_block_detail(page)
        if fatal_detail:
            raise RuntimeError(fatal_detail)

        if 'web.facebook.com' in page.url:
            logger.warning('Mobile composer: m.facebook.com redirected to web.facebook.com; trying mbasic route')
            await page.goto(mbasic_url, wait_until='domcontentloaded', timeout=45000)
            fatal_detail = await _facebook_navigation_fatal_block_detail(page)
            if fatal_detail:
                raise RuntimeError(fatal_detail)
            if 'web.facebook.com' in page.url:
                logger.warning('Mobile composer: mbasic also redirected to web.facebook.com; refusing unsafe desktop-web mobile flow')
                diagnostic_path = await _save_diagnostics(page, 'mobile_redirected_to_desktop_web')
                return False, _with_diagnostic(
                    'Facebook redirected the mobile composer to desktop web. '
                    'Skipping mobile composer to avoid typing into comments; use desktop fallback.',
                    diagnostic_path,
                )

        if not await _wait_for_facebook_ui_ready(page, timeout=2500):
            await asyncio.sleep(0.3)
        await _dismiss_common_facebook_popups(page)

        create_pattern_mobile = re.compile(
            r'Create post|Create a post|Write something|Share a thought|What.s on your mind|إنشاء منشور|اكتب',
            re.I
        )
        blocked_pattern_mobile = re.compile(r'Boost|Promote|Ad center|Create ad|Advertise|Ad\b|\bAd\b|Actions for this post|Manage posts|post it on your profile|Hide post|Boost post|Comment|Leave a comment|Post comment|إعلان|ترويج|تعليق', re.I)

        logger.info(f'Mobile composer: attempting to open post button')
        open_clicked = await _click_safe_composer_button(
            page,
            create_pattern_mobile,
            reject_pattern=blocked_pattern_mobile,
            timeout=8000
        )
        if not open_clicked:
            logger.info(f'Mobile composer: safe button click failed, trying text match')
            open_clicked = await _click_text_match(
                page,
                re.compile(r'Create post|Create a post|Write something|Share a thought|What.s on your mind|إنشاء منشور|اكتب', re.I),
                timeout=4000,
            )

        if not open_clicked:
            logger.warning(f'Mobile composer: could not find or click post button')
            diagnostic_path = await _save_diagnostics(page, 'mobile_composer_button_missing')
            return False, _with_diagnostic("Could not find or click the Create Post button on mobile Facebook.", diagnostic_path)

        logger.info(f'Mobile composer: post button clicked, waiting for composer to open')
        await _smart_wait(
            lambda: _mobile_composer_textbox_visible(page) or _is_ad_flow_url(page.url),
            timeout_ms=3000,
            check_interval_ms=150,
        )

        if _is_ad_flow_url(page.url):
            logger.warning(f'Mobile composer fallback hit ad flow: {page.url}')
            diagnostic_path = await _save_diagnostics(page, 'mobile_ad_flow_blocked')
            return False, _with_diagnostic("Facebook forced an Ad Center page instead of the composer.", diagnostic_path)

        # Find and click the text input (with robust candidates and dynamic waits)
        text_found = False
        text_target = None
        candidates = [
            "textarea",
            "[contenteditable='true'][role='textbox']",
            "[contenteditable='true']",
            "div[role='textbox']",
            "div[aria-label*='What\\'s on your mind' i]",
            "div[aria-label*='Write' i]",
        ]
        for selector in candidates:
            try:
                candidate_targets = await page.locator(selector).all()
                for candidate_target in candidate_targets:
                    try:
                        await candidate_target.wait_for(state='visible', timeout=800)
                        if await _mobile_textbox_is_non_post_input(candidate_target):
                            continue
                        text_target = candidate_target
                        text_found = True
                        break
                    except Exception:
                        continue
                if text_found:
                    break
            except Exception:
                continue

        if not text_found or text_target is None:
            diagnostic_path = await _save_diagnostics(page, 'mobile_composer_text_input_missing')
            return False, _with_diagnostic("Could not find text input in mobile composer. Page may not be loaded properly.", diagnostic_path)

        if caption:
            text_filled = False
            try:
                await text_target.fill(caption, timeout=5000)
                text_filled = True
            except Exception as fill_exc:
                logger.debug(f'Mobile composer: locator.fill failed, falling back to keyboard typing: {fill_exc}')

            if not text_filled:
                text_focused = False
                try:
                    await text_target.click(timeout=5000)
                    text_focused = True
                except Exception as click_exc:
                    logger.warning(
                        f'Mobile composer: textbox click failed; trying forced DOM focus: {click_exc}'
                    )
                    try:
                        await page.set_viewport_size({'width': 390, 'height': 1200})
                    except Exception:
                        pass
                    try:
                        await text_target.evaluate(
                            """el => {
                                el.scrollIntoView({block: 'center', inline: 'nearest'});
                                if (typeof el.focus === 'function') {
                                    el.focus();
                                }
                            }"""
                        )
                        await text_target.click(timeout=3000, force=True)
                        text_focused = True
                    except Exception as focus_exc:
                        logger.warning(f'Mobile composer: forced textbox focus failed: {focus_exc}')

                if not text_focused:
                    diagnostic_path = await _save_diagnostics(page, 'mobile_composer_text_focus_failed')
                    return False, _with_diagnostic(
                        'Could not focus text input in mobile composer.',
                        diagnostic_path,
                    )
                await page.keyboard.type(caption, delay=25)

        if post_type in {'image', 'video'} and media_url:
            import tempfile

            upload_path = _local_media_source_path(media_url)
            if not upload_path:
                with tempfile.NamedTemporaryFile(delete=False, suffix=_media_file_suffix(post_type, media_url)) as tmp:
                    tmp_path = tmp.name
                _media_source_to_path(media_url, tmp_path)
                upload_path = tmp_path

            attached = False
            page_label = _derive_page_name(target_url, "") or "Unknown page"
            tracker_state, detach_tracker = _attach_upload_response_tracker(page, f'{page_label}:mobile')
            try:
                logger.info(
                    f'POST_STEP page="{page_label}" '
                    f'stage="Mobile attach media" | type={post_type}'
                )
                try:
                    file_input = page.locator("input[type='file']").last
                    await file_input.set_input_files(upload_path, timeout=7000)
                    attached = True
                except Exception:
                    pass
                if not attached:
                    await _click_first_available(
                        page,
                        [
                            lambda: page.get_by_role('button', name=re.compile(r'Photo|Video|Photo/video|صورة|فيديو', re.I)),
                            lambda: page.get_by_role('link', name=re.compile(r'Photo|Video|Photo/video|صورة|فيديو', re.I)),
                            "a:has-text('Photo')",
                            "button:has-text('Photo')",
                        ],
                        timeout=5000,
                    )
                    file_input = page.locator("input[type='file']").last
                    await file_input.set_input_files(upload_path, timeout=10000)
                await _wait_for_composer_upload_ready(
                    page,
                    timeout=MEDIA_UPLOAD_READY_IMAGE_TIMEOUT if post_type == 'image' else MEDIA_UPLOAD_READY_VIDEO_TIMEOUT,
                    upload_tracker=tracker_state,
                )
            except Exception as exc:
                diagnostic_path = await _save_diagnostics(page, 'mobile_media_upload_failed')
                return False, _with_diagnostic(str(exc), diagnostic_path)
            finally:
                detach_tracker()
            logger.info(
                f'POST_STEP page="{page_label}" '
                f'stage="Mobile media attached" | type={post_type}'
            )

        publish_monitor = _start_post_network_monitor(page)
        submitted = await _click_first_available(
            page,
            [
                "button[type='submit']",
                "input[type='submit'][value*='Post']",
                "input[type='submit'][value*='Publish']",
                "input[type='submit'][value*='Share']",
                "input[type='submit'][value*='نشر']",
                "input[type='submit'][value*='مشاركة']",
                lambda: page.get_by_role('button', name=re.compile(r'^(Post|Publish|Share|Send|نشر|انشر|مشاركة|Publier|Publicar|Postar)$', re.I)),
                "button:has-text('Post')",
                "button:has-text('Publish')",
                "button:has-text('Share')",
                "button:has-text('نشر')",
                "button:has-text('مشاركة')",
                "div[role='button']:has-text('Post')",
                "div[role='button']:has-text('Publish')",
                "div[role='button']:has-text('نشر')",
                "div[role='button']:has-text('مشاركة')",
                "div[role='button'][aria-label*='نشر' i]",
                "div[role='button'][aria-label*='مشاركة' i]",
            ],
            timeout=10000,
        )
        if not submitted:
            submitted = bool(await _click_enabled_text_match(page, re.compile(r'^(Post|Publish|Share|Send|Post now|Publish now|نشر|انشر|مشاركة)$', re.I), timeout=10000))
        if not submitted:
            submitted = await _click_text_match(page, re.compile(r'^(Post|Publish|Share|Send|نشر|انشر|مشاركة)$', re.I), timeout=5000)
        if not submitted:
            if publish_monitor is not None:
                publish_monitor.stop()
            diagnostic_path = await _save_diagnostics(page, 'mobile_post_publish_button_missing')
            return False, _with_diagnostic("Could not find the mobile Post/Publish button.", diagnostic_path)

        verified, verify_reason = await _confirm_post_published(
            page,
            caption=caption,
            post_type=post_type,
            timeout_ms=8000,
            accept_publish_click=post_type == 'video' and POST_ACCEPT_VIDEO_PUBLISH_CLICK_AS_SUCCESS,
            network_monitor=publish_monitor,
            target_url=target_url,
            page_name=_derive_page_name(target_url, ''),
        )
        if not verified:
            diagnostic_path = await _save_diagnostics(page, 'mobile_post_publish_unverified')
            return False, _publish_sent_unconfirmed(
                f'Clicked publish, but Facebook did not confirm. {verify_reason}',
                diagnostic_path,
            )
        return True, f"Post accepted by Facebook mobile. {verify_reason}"
    except Exception as exc:
        diagnostic_path = await _save_diagnostics(page, 'mobile_post_exception')
        return False, _with_diagnostic(str(exc), diagnostic_path)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def _create_text_post_mobile(page: Page, target_url: str, caption: str) -> Tuple[bool, str]:
    return await _create_post_mobile(page, target_url, caption, 'post', None)


async def _fill_composer_caption(page: Page, caption: str) -> None:
    if not caption:
        return

    logger.info("✍️ Filling composer caption...")

    # Try multiple times to find and focus the textbox
    for attempt in range(3):
        dialog = await _find_composer_context(page)

        candidates = [
            # Fast path: exact role='textbox' from live DOM observation
            lambda: dialog.locator("div[role='textbox']").first,
            lambda: dialog.locator("[contenteditable='true'][role='textbox']:not([aria-label*='Search' i]):not([placeholder*='Search' i])").first,
            # Arabic placeholder from live DOM: بم تفكر؟
            lambda: dialog.locator("[aria-placeholder*='بم تفكر'], [placeholder*='بم تفكر']").first,
            lambda: dialog.get_by_role('textbox').first,
            lambda: dialog.locator("[contenteditable='true']").first,
            lambda: dialog.locator("div[aria-label*=\"What's on your mind\"]").first,
            lambda: dialog.locator("textarea").first,
        ]

        for candidate in candidates:
            try:
                target = candidate() if callable(candidate) else dialog.locator(candidate).first
                if await target.is_visible(timeout=1500):
                    logger.info(f"Target textbox found on attempt {attempt+1}. Clicking to focus...")
                    await target.click(force=True)

                    # Clear any existing text if possible
                    try:
                        await target.fill("")
                    except Exception:
                        pass

                    # Type the caption (fast: 15ms delay)
                    await page.keyboard.type(caption, delay=15)

                    # Verify text was entered
                    current_text = ''
                    await _smart_wait(
                        lambda: target.evaluate("el => el.innerText || el.textContent || ''"),
                        timeout_ms=600,
                        check_interval_ms=100,
                    )
                    current_text = await target.evaluate("el => el.innerText || el.textContent || ''")
                    if current_text.strip():
                        logger.info("✅ Caption successfully written and verified in textbox!")
                        return
                    else:
                        logger.warning("⚠️ Textbox verified empty after typing. Retrying...")
            except Exception as e:
                logger.debug(f"Textbox candidate failed: {e}")
                continue

        # Wait a bit before next attempt
        await _smart_wait(
            lambda: page.locator(
                "div[role='textbox'], [contenteditable='true'][role='textbox'], textarea"
            ).count(),
            timeout_ms=800,
            check_interval_ms=150,
        )

    # Fallback: Type directly on page if all else fails
    logger.warning("⚠️ Textbox targeting failed; falling back to direct keyboard entry...")
    await page.keyboard.type(caption, delay=35)


async def _set_input_files_via_cdp(page: Page, file_input: Any, file_path: str) -> bool:
    if not FACEBOOK_UPLOAD_CDP_FALLBACK_ENABLED:
        return False
    try:
        resolved_path = str(Path(file_path).resolve())
        if not os.path.exists(resolved_path):
            return False
        marker = 'fb-upload-cdp-' + hashlib.sha1(
            f'{resolved_path}:{time.time()}'.encode('utf-8', errors='ignore')
        ).hexdigest()[:16]
        await file_input.evaluate(
            """(el, marker) => {
                el.setAttribute('data-fb-upload-cdp-id', marker);
            }""",
            marker,
        )
        client = await page.context.new_cdp_session(page)
        try:
            document = await client.send('DOM.getDocument', {'depth': -1, 'pierce': True})
            root_id = (document.get('root') or {}).get('nodeId')
            if not root_id:
                return False
            selector = f'input[type="file"][data-fb-upload-cdp-id="{marker}"]'
            query_result = await client.send('DOM.querySelector', {'nodeId': root_id, 'selector': selector})
            node_id = int(query_result.get('nodeId') or 0)
            if not node_id:
                search = await client.send(
                    'DOM.performSearch',
                    {'query': selector, 'includeUserAgentShadowDOM': True},
                )
                search_id = search.get('searchId')
                result_count = int(search.get('resultCount') or 0)
                if search_id and result_count > 0:
                    results = await client.send(
                        'DOM.getSearchResults',
                        {'searchId': search_id, 'fromIndex': 0, 'toIndex': 1},
                    )
                    node_ids = results.get('nodeIds') or []
                    node_id = int(node_ids[0]) if node_ids else 0
                if search_id:
                    try:
                        await client.send('DOM.discardSearchResults', {'searchId': search_id})
                    except Exception:
                        pass
            if not node_id:
                return False
            await client.send('DOM.setFileInputFiles', {'nodeId': node_id, 'files': [resolved_path]})
            return True
        finally:
            try:
                await client.detach()
            except Exception:
                pass
            try:
                await file_input.evaluate("""el => el.removeAttribute('data-fb-upload-cdp-id')""")
            except Exception:
                pass
    except Exception as exc:
        logger.debug(f'CDP file input upload failed: {exc}')
        return False


async def _refresh_facebook_upload_tokens(page: Page, label: str) -> Dict[str, Any]:
    """Touch the live page modules before upload and log only token presence."""
    try:
        tokens = await page.evaluate(
            """() => {
                const get = name => {
                    try {
                        if (typeof require === 'function') {
                            return require(name) || {};
                        }
                    } catch (_) {}
                    return {};
                };
                const cookie = document.cookie || '';
                return {
                    fb_dtsg: get('DTSGInitialData').token ||
                        get('DTSGInitData').token ||
                        document.querySelector('input[name="fb_dtsg"]')?.value || '',
                    lsd: get('LSD').token ||
                        document.querySelector('input[name="lsd"]')?.value || '',
                    user_id: get('CurrentUserInitialData').USER_ID || '',
                    revision: String(get('SiteData').client_revision || ''),
                    xs_present: /(?:^|;\\s*)xs=/.test(cookie),
                };
            }"""
        )
        if not isinstance(tokens, dict):
            tokens = {}
        logger.info(
            f'POST_STEP page="{label}" stage="Upload token refresh" | '
            f'fb_dtsg={"present" if tokens.get("fb_dtsg") else "missing"} '
            f'lsd={"present" if tokens.get("lsd") else "missing"} '
            f'user_id={"present" if tokens.get("user_id") else "missing"} '
            f'xs={"present" if tokens.get("xs_present") else "missing"} '
            f'revision={"present" if tokens.get("revision") else "missing"}'
        )
        return {
            'fb_dtsg_present': bool(tokens.get('fb_dtsg')),
            'lsd_present': bool(tokens.get('lsd')),
            'user_id_present': bool(tokens.get('user_id')),
            'xs_present': bool(tokens.get('xs_present')),
            'revision_present': bool(tokens.get('revision')),
        }
    except Exception as exc:
        logger.debug(f'Upload token refresh failed for {label}: {exc}')
        return {}


async def _prime_file_input_for_upload(page: Page, file_input: Any) -> None:
    """Dispatch the user-activation events Facebook commonly listens for."""
    try:
        await file_input.evaluate(
            """el => {
                try { el.scrollIntoView({block: 'center', inline: 'center'}); } catch (_) {}
                try { el.focus({preventScroll: true}); } catch (_) {}
                const eventInit = {bubbles: true, cancelable: true, composed: true, view: window};
                for (const type of ['mouseover', 'mouseenter', 'pointerover', 'pointerenter',
                    'pointerdown', 'mousedown', 'focus', 'pointerup', 'mouseup', 'click']) {
                    try {
                        const event = type.startsWith('pointer')
                            ? new PointerEvent(type, {...eventInit, pointerType: 'mouse', isPrimary: true})
                            : type.startsWith('mouse') || type === 'click'
                                ? new MouseEvent(type, eventInit)
                                : new FocusEvent(type, eventInit);
                        el.dispatchEvent(event);
                    } catch (_) {}
                }
            }"""
        )
    except Exception:
        pass
    try:
        await page.wait_for_timeout(120)
    except Exception:
        pass


async def _upload_media_file(
    page: Page,
    tmp_path: str,
    *,
    skip_if_already_selected: bool = False,
    upload_transport: str = 'payload',
) -> bool:
    """Upload a media file (image or video) to the Facebook composer dialog."""
    # Find the appropriate composer context (modal or inline)
    dialog = await _find_composer_context(page)
    is_video = tmp_path.lower().endswith(('.mp4', '.mov', '.m4v', '.webm', '.avi', '.mkv'))
    post_type = 'video' if is_video else 'image'
    upload_file: Any
    if upload_transport in {'path', 'cdp'}:
        upload_file = tmp_path
    else:
        upload_transport = 'payload'
        upload_file = _playwright_upload_file_payload(tmp_path, post_type)
    before_file_input_count = 0
    try:
        before_file_input_count = await page.locator("input[type='file']").count()
    except Exception:
        before_file_input_count = 0

    def input_accepts_media(accept_attr: str) -> bool:
        accept = (accept_attr or '').lower()
        if not accept:
            return False
        if is_video:
            return 'video' in accept
        return 'image' in accept

    def input_priority(accept_attr: str, multiple_attr: Optional[str], data_auto: Optional[str]) -> int:
        accept = (accept_attr or '').lower()
        if not input_accepts_media(accept):
            return -1000
        priority = 0
        if 'image/*' in accept:
            priority += 20
        elif 'image' in accept:
            priority += 10
        if 'video/*' in accept:
            priority += 20 if is_video else 5
        elif 'video' in accept:
            priority += 10 if is_video else 3
        if multiple_attr is not None:
            priority += 8
        if data_auto:
            priority -= 2
        if is_video and 'video' not in accept:
            priority -= 100
        if not is_video and 'image' not in accept:
            priority -= 100
        return priority

    async def try_set_file_inputs(scope: Any, scope_label: str) -> bool:
        try:
            file_inputs = await scope.locator("input[type='file']").all()
        except Exception:
            return False
        candidates: List[Tuple[int, int, Any, str]] = []
        for index, file_input in enumerate(file_inputs):
            try:
                accept_attr = await file_input.get_attribute('accept') or ''
                multiple_attr = await file_input.get_attribute('multiple')
                data_auto = await file_input.get_attribute('data-auto-logging-id')
                if not input_accepts_media(accept_attr):
                    continue
                candidates.append((input_priority(accept_attr, multiple_attr, data_auto), -index, file_input, accept_attr))
            except Exception:
                continue
        for priority, _negative_index, file_input, accept_attr in sorted(candidates, reverse=True):
            if priority < 0:
                continue
            try:
                if upload_transport == 'cdp':
                    await _prime_file_input_for_upload(page, file_input)
                    uploaded = await _set_input_files_via_cdp(page, file_input, tmp_path)
                    if not uploaded:
                        continue
                else:
                    await _prime_file_input_for_upload(page, file_input)
                    await file_input.set_input_files(cast(Any, upload_file), timeout=5000)
                logger.info(
                    f"Media uploaded via {scope_label} file input "
                    f"transport={upload_transport} priority={priority} accept={accept_attr[:120]!r}"
                )
                return True
            except Exception:
                continue
        return False

    async def try_set_new_page_file_inputs() -> bool:
        try:
            current_count = await page.locator("input[type='file']").count()
        except Exception:
            return False
        if current_count <= before_file_input_count:
            return False
        candidates: List[Tuple[int, int, Any, str]] = []
        for index in range(before_file_input_count, current_count):
            try:
                file_input = page.locator("input[type='file']").nth(index)
                accept_attr = await file_input.get_attribute('accept') or ''
                multiple_attr = await file_input.get_attribute('multiple')
                data_auto = await file_input.get_attribute('data-auto-logging-id')
                if not input_accepts_media(accept_attr):
                    continue
                candidates.append((input_priority(accept_attr, multiple_attr, data_auto), -index, file_input, accept_attr))
            except Exception:
                continue
        for priority, _negative_index, file_input, accept_attr in sorted(candidates, reverse=True):
            if priority < 0:
                continue
            try:
                if upload_transport == 'cdp':
                    await _prime_file_input_for_upload(page, file_input)
                    uploaded = await _set_input_files_via_cdp(page, file_input, tmp_path)
                    if not uploaded:
                        continue
                else:
                    await _prime_file_input_for_upload(page, file_input)
                    await file_input.set_input_files(cast(Any, upload_file), timeout=5000)
                logger.info(
                    f"Media uploaded via new page file input "
                    f"transport={upload_transport} priority={priority} accept={accept_attr[:120]!r}"
                )
                return True
            except Exception:
                continue
        return False

    if skip_if_already_selected and await _count_selected_media_file_inputs(page, post_type) > 0:
        logger.info('Skipping media file selection because composer already has selected media.')
        return True

    # Strategy 1: Direct file input injection (fastest — no click needed)
    if POST_DIRECT_FILE_INPUT_UPLOAD_ENABLED or upload_transport == 'cdp':
        try:
            if await try_set_file_inputs(dialog, 'direct composer'):
                return True
        except Exception:
            pass

    # Strategy 2: Click Photo/Video button using aria-label (from live DOM: صورة/فيديو)
    photo_video_clicked = False
    try:
        for aria_label in ["صورة/فيديو", "Photo/video", "Photo/Video", "Add photos/videos", "إضافة صور/فيديوهات"]:
            btn = dialog.locator(f"div[role='button'][aria-label='{aria_label}'], [aria-label='{aria_label}']").first
            if await btn.is_visible(timeout=800):
                clicked_this_button = False
                try:
                    async with page.expect_file_chooser(timeout=3500) as chooser_info:
                        await btn.click(timeout=2000)
                        clicked_this_button = True
                    file_chooser = await chooser_info.value
                    await file_chooser.set_files(cast(Any, upload_file))
                    logger.info(
                        f"Media uploaded via Photo/Video file chooser: '{aria_label}' "
                        f"transport={upload_transport}"
                    )
                    return True
                except Exception as chooser_exc:
                    if not clicked_this_button:
                        try:
                            await btn.click(timeout=2000)
                            clicked_this_button = True
                        except Exception:
                            logger.debug(f"Photo/Video button click failed for '{aria_label}': {chooser_exc}")
                            continue
                    photo_video_clicked = True
                    logger.info(f"Clicked Photo/Video button via aria-label: '{aria_label}'")
                break
    except Exception as e:
        logger.debug(f"aria-label Photo/Video click error: {e}")

    if not photo_video_clicked:
        await _click_first_available(
            dialog,
            [
                lambda: dialog.get_by_role('button', name=re.compile(r'Photo/video|Photo|Video|صورة|فيديو', re.I)),
                "div[role='button']:has-text('Photo/video')",
                "div[role='button']:has-text('Photo')",
                "div[role='button']:has-text('Video')",
                "div[role='button']:has-text('صورة/فيديو')",
            ],
            timeout=4000,
        )

    # After clicking Photo/Video, try file input again
    await _smart_wait(
        lambda: page.locator("input[type='file']").count(),
        timeout_ms=1200,
        check_interval_ms=150,
    )
    if await try_set_file_inputs(dialog, 'composer dialog after Photo/Video click'):
        return True
    if await try_set_new_page_file_inputs():
        return True

    # Strategy 3: File chooser dialog fallback
    try:
        async with page.expect_file_chooser(timeout=8000) as chooser_info:
            clicked = await _click_first_available(
                dialog,
                [
                    lambda: dialog.get_by_role('button', name=re.compile(r'Add photos/videos|Add photos|Upload|إضافة|تحميل', re.I)),
                    "div[role='button']:has-text('Add photos/videos')",
                    "div[role='button']:has-text('Add photos')",
                    "div[role='button']:has-text('Upload')",
                    "div[role='button']:has-text('إضافة صور')",
                ],
                timeout=5000,
            )
            if not clicked:
                return False
        file_chooser = await chooser_info.value
        await file_chooser.set_files(cast(Any, upload_file))
        logger.info(f"Media uploaded via file chooser dialog transport={upload_transport}")
        return True
    except Exception:
        return False


def _media_file_suffix(post_type: str, media_url: str = '') -> str:
    lowered = (media_url or '').split('?', 1)[0].lower()
    if post_type == 'video':
        if lowered.endswith(('.mov', '.m4v', '.webm')):
            return Path(lowered).suffix or '.mp4'
        return '.mp4'
    if lowered.endswith(('.png', '.webp', '.gif', '.jpeg', '.jpg')):
        return Path(lowered).suffix or '.jpg'
    return '.jpg'


def _batch_media_stage_key(post: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    post_type = str(post.get('post_type') or 'post').strip().lower()
    media_url = str(post.get('media_url') or '').strip()
    if post_type not in {'image', 'video'} or not media_url:
        return None
    return post_type, media_url


def _stage_batch_media_sources_sync(posts: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    if not POST_BATCH_MEDIA_PRESTAGE_ENABLED or len(posts) <= 1:
        return posts, []

    media_keys = {
        key
        for post in posts
        for key in [_batch_media_stage_key(post)]
        if key is not None
    }
    if not media_keys:
        return posts, []

    import concurrent.futures

    staged: Dict[Tuple[str, str], Tuple[str, bool]] = {}

    def stage_one(key: Tuple[str, str]) -> Tuple[Tuple[str, str], str, bool]:
        post_type, source = key
        staged_path, created = _stage_media_for_browser_upload(post_type, source)
        return key, staged_path, created

    started_at = time.time()
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(POST_BATCH_MEDIA_PRESTAGE_CONCURRENCY, len(media_keys))
    ) as executor:
        future_map = {executor.submit(stage_one, key): key for key in media_keys}
        for future in concurrent.futures.as_completed(future_map):
            key = future_map[future]
            try:
                staged_key, staged_path, created = future.result()
                staged[staged_key] = (staged_path, created)
            except Exception as exc:
                logger.warning(f'BATCH_MEDIA_PRESTAGE failed source="{key[1][:120]}": {exc}')

    if not staged:
        return posts, []

    staged_posts: List[Dict[str, Any]] = []
    for post in posts:
        key = _batch_media_stage_key(post)
        if key is not None and key in staged:
            staged_path, _created = staged[key]
            staged_post = dict(post)
            staged_post['media_url'] = staged_path
            staged_post['_prestaged_media_source'] = key[1]
            staged_posts.append(staged_post)
        else:
            staged_posts.append(post)

    logger.info(
        f'BATCH_MEDIA_PRESTAGE staged={len(staged)}/{len(media_keys)} '
        f'posts={len(posts)} elapsed={time.time() - started_at:.1f}s'
    )
    return staged_posts, [path for path, created in staged.values() if created]


async def _stage_batch_media_sources(posts: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    return await asyncio.to_thread(_stage_batch_media_sources_sync, posts)


def _cleanup_staged_batch_media(paths: List[str]) -> None:
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.unlink(path)
        except Exception as exc:
            logger.debug(f'Could not remove staged batch media file {path}: {exc}')


async def _attach_composer_media(page: Page, post_type: str, media_url: Optional[str], label: str) -> Tuple[bool, str]:
    if post_type not in {'image', 'video'}:
        return True, ''
    if not media_url:
        return False, f'Missing {post_type} media URL; refusing to publish a media post without media.'

    tmp_path = ''
    cleanup_paths: List[str] = []
    upload_attempt_events: List[Dict[str, Any]] = []
    try:
        upload_path = _local_media_source_path(media_url)
        if upload_path:
            try:
                staging_root = Path(_media_upload_staging_dir()).resolve()
                upload_resolved = Path(upload_path).resolve()
                if staging_root not in upload_resolved.parents and upload_resolved != staging_root:
                    tmp_path, created = _stage_media_for_browser_upload(post_type, upload_path)
                    if created:
                        cleanup_paths.append(tmp_path)
                    upload_path = tmp_path
            except Exception:
                tmp_path, created = _stage_media_for_browser_upload(post_type, upload_path)
                if created:
                    cleanup_paths.append(tmp_path)
                upload_path = tmp_path
        else:
            tmp_path, created = _stage_media_for_browser_upload(post_type, media_url)
            if created:
                cleanup_paths.append(tmp_path)
            upload_path = tmp_path
        if post_type == 'image':
            safe_upload_path, safe_created = _ensure_facebook_safe_image(upload_path)
            if safe_upload_path != upload_path:
                if safe_created:
                    cleanup_paths.append(safe_upload_path)
                upload_path = safe_upload_path
            jpeg_retry_path = _facebook_jpeg_upload_variant(upload_path)
            if jpeg_retry_path:
                cleanup_paths.append(jpeg_retry_path)
            png_retry_path = _facebook_png_upload_variant(upload_path)
            if png_retry_path:
                cleanup_paths.append(png_retry_path)
        else:
            jpeg_retry_path = None
            png_retry_path = None
        if not os.path.exists(upload_path):
            return False, f'Media file not found before upload: {Path(upload_path).name}'
        file_size = os.path.getsize(upload_path)
        if file_size <= 0:
            return False, f'Media file is empty before upload: {Path(upload_path).name}'
        logger.info(
            f'POST_STEP page="{label}" stage="Media file validated" | '
            f'type={post_type} size_bytes={file_size}'
        )
        await _refresh_facebook_upload_tokens(page, label)

        def switch_to_jpeg_retry(reason: str) -> bool:
            nonlocal upload_path, jpeg_retry_path, path_upload_path, cdp_upload_path, upload_transport, force_next_file_selection
            if post_type != 'image' or not jpeg_retry_path or jpeg_retry_path == upload_path:
                return False
            if not os.path.exists(jpeg_retry_path) or os.path.getsize(jpeg_retry_path) <= 0:
                return False
            logger.info(
                f'POST_STEP page="{label}" stage="Switch image upload variant" | '
                f'from={Path(str(upload_path)).suffix or "none"} to=.jpg reason="{reason[:160]}"'
            )
            upload_path = jpeg_retry_path
            jpeg_retry_path = None
            path_upload_path = ''
            cdp_upload_path = ''
            upload_transport = 'payload'
            force_next_file_selection = True
            return True

        upload_transport = 'payload'
        path_upload_path = ''
        cdp_upload_path = ''
        force_next_file_selection = False
        if post_type == 'image' and FACEBOOK_UPLOAD_PATH_FIRST_ENABLED:
            try:
                path_upload_path, created = _stable_browser_upload_path(str(upload_path), post_type)
                if created:
                    cleanup_paths.append(path_upload_path)
                upload_transport = 'path'
                logger.info(
                    f'POST_STEP page="{label}" stage="Use path upload first" | '
                    f'file={Path(path_upload_path).name}'
                )
            except Exception as exc:
                logger.warning(f'Could not prepare initial path-based image upload: {exc}')
                path_upload_path = ''

        def switch_to_png_retry(reason: str) -> bool:
            nonlocal upload_path, png_retry_path, path_upload_path, cdp_upload_path, upload_transport, force_next_file_selection
            if post_type != 'image' or not png_retry_path or png_retry_path == upload_path:
                return False
            if not os.path.exists(png_retry_path) or os.path.getsize(png_retry_path) <= 0:
                return False
            try:
                stable_png_path, created = _stable_browser_upload_path(png_retry_path, post_type)
                if created:
                    cleanup_paths.append(stable_png_path)
                path_upload_path = stable_png_path
            except Exception:
                path_upload_path = ''
            logger.info(
                f'POST_STEP page="{label}" stage="Switch image upload variant" | '
                f'from={Path(str(upload_path)).suffix or "none"} to=.png '
                f'transport=path reason="{reason[:160]}"'
            )
            upload_path = png_retry_path
            png_retry_path = None
            cdp_upload_path = ''
            upload_transport = 'path' if path_upload_path else 'payload'
            force_next_file_selection = True
            return True

        def switch_to_path_upload(reason: str) -> bool:
            nonlocal upload_transport, path_upload_path, force_next_file_selection
            if (
                post_type != 'image'
                or not FACEBOOK_UPLOAD_PATH_FALLBACK_ON_REJECTION
                or upload_transport in {'path', 'cdp'}
            ):
                return False
            try:
                path_upload_path, created = _stable_browser_upload_path(str(upload_path), post_type)
                if created:
                    cleanup_paths.append(path_upload_path)
                upload_transport = 'path'
                force_next_file_selection = True
                logger.info(
                    f'POST_STEP page="{label}" stage="Switch image upload transport" | '
                    f'to=path file={Path(path_upload_path).name} reason="{reason[:160]}"'
                )
                return True
            except Exception as exc:
                logger.warning(f'Could not prepare path-based Facebook image upload fallback: {exc}')
                return False

        def switch_to_cdp_upload(reason: str) -> bool:
            nonlocal upload_transport, cdp_upload_path, force_next_file_selection
            if (
                post_type != 'image'
                or not FACEBOOK_UPLOAD_CDP_FALLBACK_ENABLED
                or upload_transport == 'cdp'
            ):
                return False
            try:
                cdp_upload_path, created = _stable_browser_upload_path(str(upload_path), post_type)
                if created:
                    cleanup_paths.append(cdp_upload_path)
                upload_transport = 'cdp'
                force_next_file_selection = True
                logger.info(
                    f'POST_STEP page="{label}" stage="Switch image upload transport" | '
                    f'to=cdp file={Path(cdp_upload_path).name} reason="{reason[:160]}"'
                )
                return True
            except Exception as exc:
                logger.warning(f'Could not prepare CDP image upload fallback: {exc}')
                return False

        last_error = ''
        max_upload_attempts = FACEBOOK_MEDIA_UPLOAD_MAX_ATTEMPTS
        for attempt in range(1, max_upload_attempts + 1):
            await _clear_failed_media_upload_state(page)
            before_media_count = await _count_composer_media(page, post_type)
            tracker_state, detach_tracker = _attach_upload_response_tracker(page, f'{label}:attempt{attempt}')
            retry_after_failure = True
            try:
                logger.info(
                    f'POST_STEP page="{label}" stage="Upload attempt" | '
                    f'type={post_type} attempt={attempt} transport={upload_transport}'
                )
                selected_upload_path = (
                    cdp_upload_path
                    if upload_transport == 'cdp' and cdp_upload_path
                    else path_upload_path
                    if upload_transport == 'path' and path_upload_path
                    else upload_path
                )
                skip_selected_media = attempt > 1 and not force_next_file_selection
                force_next_file_selection = False
                uploaded = await _upload_media_file(
                    page,
                    selected_upload_path,
                    skip_if_already_selected=skip_selected_media,
                    upload_transport=upload_transport,
                )
                if not uploaded:
                    last_error = 'Could not attach media in the Facebook composer.'
                    continue
                logger.info(f'POST_STEP page="{label}" stage="Media attached" | type={post_type} attempt={attempt}')
                await _wait_for_composer_upload_ready(
                    page,
                    timeout=MEDIA_UPLOAD_READY_IMAGE_TIMEOUT if post_type == 'image' else MEDIA_UPLOAD_READY_VIDEO_TIMEOUT,
                    upload_tracker=tracker_state,
                )
                visible_upload_error = await _visible_media_upload_error_detail(page)
                if visible_upload_error:
                    last_error = f'Facebook rejected the selected {post_type}: {visible_upload_error}'
                    deterministic_rejection = _is_media_upload_rejection(visible_upload_error)
                    logger.warning(
                        f'POST_STEP page="{label}" stage="Upload visible error" | '
                        f'type={post_type} attempt={attempt} error="{visible_upload_error}"'
                    )
                    await _clear_failed_media_upload_state(page)
                    retry_after_failure = (
                        switch_to_cdp_upload(visible_upload_error)
                        or switch_to_png_retry(visible_upload_error)
                        or switch_to_jpeg_retry(visible_upload_error)
                        or switch_to_path_upload(visible_upload_error)
                        or (attempt < max_upload_attempts and not deterministic_rejection)
                    )
                    if retry_after_failure:
                        continue
                    break
                preview_visible = await _verify_composer_media_visible(
                    page,
                    post_type,
                    before_count=before_media_count,
                    timeout=12000 if post_type == 'image' else 30000,
                    allow_existing=attempt > 1,
                )
                visible_upload_error = await _visible_media_upload_error_detail(page)
                if visible_upload_error:
                    last_error = f'Facebook rejected the selected {post_type}: {visible_upload_error}'
                    deterministic_rejection = _is_media_upload_rejection(visible_upload_error)
                    logger.warning(
                        f'POST_STEP page="{label}" stage="Upload visible error after preview wait" | '
                        f'type={post_type} attempt={attempt} error="{visible_upload_error}"'
                    )
                    await _clear_failed_media_upload_state(page)
                    retry_after_failure = (
                        switch_to_cdp_upload(visible_upload_error)
                        or switch_to_png_retry(visible_upload_error)
                        or switch_to_jpeg_retry(visible_upload_error)
                        or switch_to_path_upload(visible_upload_error)
                        or (attempt < max_upload_attempts and not deterministic_rejection)
                    )
                    if retry_after_failure:
                        continue
                    break
                selected_file_count = await _count_selected_media_file_inputs(page, post_type)
                media_count_after = await _count_composer_media(page, post_type)
                upload_success_seen = int(tracker_state.get('completed_success') or 0) > 0
                media_count_increased = media_count_after > before_media_count
                if int(tracker_state.get('failed') or 0) > 0:
                    last_error = f'Facebook returned an upload error: {_upload_tracker_failure_detail(tracker_state) or "unknown"}'
                    last_status = int(tracker_state.get('last_status') or 0)
                    deterministic_rejection = _is_media_upload_rejection(last_error)
                    logical_upload_error = bool(tracker_state.get('logical_error'))
                    retry_after_failure = (
                        switch_to_cdp_upload(last_error)
                        or switch_to_jpeg_retry(last_error)
                        or switch_to_png_retry(last_error)
                        or switch_to_path_upload(last_error)
                        or (
                            not logical_upload_error
                            and bool(last_status >= 500)
                            and not deterministic_rejection
                        )
                    )
                    logger.warning(
                        f'POST_STEP page="{label}" stage="Upload network failed" | '
                        f'attempt={attempt} status={last_status} retry={retry_after_failure} '
                        f'logical_error={logical_upload_error} '
                        f'error="{_upload_tracker_failure_detail(tracker_state)}" '
                        f'trace="{_upload_tracker_summary(tracker_state)}"'
                    )
                    if retry_after_failure:
                        continue
                    break
                if not preview_visible:
                    if selected_file_count > 0 or upload_success_seen or media_count_increased or (attempt > 1 and media_count_after > 0):
                        logger.warning(
                            f'POST_STEP page="{label}" stage="Media preview inconclusive" | '
                            f'type={post_type} attempt={attempt} selected_files={selected_file_count} '
                            f'network_success={tracker_state.get("success")} '
                            f'completed_success={tracker_state.get("completed_success")} '
                            f'before={before_media_count} after={media_count_after}; '
                            'not re-selecting media to avoid duplicate attachments'
                        )
                        return True, ''
                    last_error = f'{post_type.title()} was selected but Facebook did not show a composer preview.'
                    logger.warning(
                        f'POST_STEP page="{label}" stage="Media preview missing" | '
                        f'type={post_type} attempt={attempt} before={before_media_count} after={media_count_after}'
                    )
                    continue
                logger.info(
                    f'POST_STEP page="{label}" stage="Upload verified" | '
                    f'type={post_type} attempt={attempt} '
                    f'network_seen={tracker_state.get("seen")} '
                    f'network_success={tracker_state.get("success")}'
                )
                return True, ''
            except Exception as exc:
                last_error = str(exc)
                if int(tracker_state.get('failed') or 0) > 0:
                    last_error = f'Facebook returned an upload error: {_upload_tracker_failure_detail(tracker_state) or last_error}'
                    last_status = int(tracker_state.get('last_status') or 0)
                    deterministic_rejection = _is_media_upload_rejection(last_error)
                    logical_upload_error = bool(tracker_state.get('logical_error'))
                    retry_after_failure = (
                        switch_to_cdp_upload(last_error)
                        or switch_to_jpeg_retry(last_error)
                        or switch_to_png_retry(last_error)
                        or switch_to_path_upload(last_error)
                        or (
                            not logical_upload_error
                            and bool(last_status >= 500)
                            and not deterministic_rejection
                        )
                    )
                elif _is_media_upload_rejection(last_error):
                    retry_after_failure = False
                elif _is_media_upload_readiness_timeout(last_error):
                    retry_after_failure = (
                        switch_to_cdp_upload(last_error)
                        or switch_to_png_retry(last_error)
                        or switch_to_jpeg_retry(last_error)
                        or switch_to_path_upload(last_error)
                        or (attempt < max_upload_attempts)
                    )
                logger.warning(
                    f'POST_STEP page="{label}" stage="Upload attempt failed" | '
                    f'type={post_type} attempt={attempt} retry={retry_after_failure} '
                    f'error="{last_error}" trace="{_upload_tracker_summary(tracker_state)}"'
                )
            finally:
                attempt_events = tracker_state.get('events') or []
                if isinstance(attempt_events, list):
                    for event in attempt_events:
                        if isinstance(event, dict):
                            upload_attempt_events.append({'attempt': attempt, **event})
                trace_summary = _upload_tracker_summary(tracker_state)
                if trace_summary:
                    logger.info(
                        f'POST_STEP page="{label}" stage="Upload attempt trace" | '
                        f'type={post_type} attempt={attempt} trace="{trace_summary}"'
                    )
                detach_tracker()
            if not retry_after_failure:
                break
            if attempt < max_upload_attempts:
                async def _upload_busy_state_clear() -> bool:
                    return await page.locator("[role='progressbar'], [aria-busy='true']").count() == 0

                await _smart_wait(
                    _upload_busy_state_clear,
                    timeout_ms=1000,
                    check_interval_ms=150,
                )

        diagnostic_path = await _save_diagnostics(page, f'{label}_media_upload_unverified')
        trace_detail = _upload_attempt_trace_summary(upload_attempt_events)
        final_error = last_error or f'{post_type.title()} upload could not be verified.'
        if trace_detail:
            final_error = f'{final_error}; upload_trace={trace_detail}'
        return False, _with_diagnostic(
            final_error,
            diagnostic_path,
        )
    except Exception as exc:
        diagnostic_path = await _save_diagnostics(page, f'{label}_media_attach_exception')
        trace_detail = _upload_attempt_trace_summary(upload_attempt_events)
        final_error = str(exc)
        if trace_detail:
            final_error = f'{final_error}; upload_trace={trace_detail}'
        return False, _with_diagnostic(final_error, diagnostic_path)
    finally:
        for path in reversed(cleanup_paths):
            try:
                if path and os.path.exists(path):
                    os.unlink(path)
            except Exception as cleanup_exc:
                logger.debug(f'Could not remove staged upload file {Path(path).name}: {cleanup_exc}')


async def _post_via_pages_portal(
    page: Page,
    page_name: str,
    caption: str,
    post_type: str = 'post',
    media_url: Optional[str] = None,
    target_url: str = '',
) -> Tuple[bool, str]:
    """
    Post to a Facebook Page using the Pages Portal strategy.

    Flow:
    1. Navigate to /pages/?category=your_pages
    2. Find the target page's "Create post" button and click it
    3. Type caption in the composer dialog
    4. Click "Next" → Click "Post"
    5. Handle post-publish popups (WhatsApp, Event hosting, etc.)
    """
    portal_urls = [
        'https://www.facebook.com/pages/?category=your_pages',
        'https://www.facebook.com/bookmarks/pages',
    ]

    # Step 1: Find the target page card and its "Create post" button
    create_post_clicked = False
    for portal_url in portal_urls:
        logger.info(f'📄 Pages Portal: navigating to {portal_url}')
        await page.goto(portal_url, wait_until='domcontentloaded', timeout=45000)
        try:
            await page.wait_for_selector('div[role="main"], h1', timeout=4000)
        except Exception:
            await asyncio.sleep(1.0)
        await _dismiss_common_facebook_popups(page)

        logger.info(f'📄 Pages Portal: searching for page "{page_name}"')
        create_post_clicked = await _click_pages_portal_create_post(page, page_name)
        if create_post_clicked:
            break

    if not create_post_clicked:
        diagnostic_path = await _save_diagnostics(page, 'pages_portal_create_post_not_found')
        return False, _with_diagnostic(
            f'Could not find "Create post" button for page "{page_name}" on the Pages Portal.',
            diagnostic_path,
        )

    # Step 2: Complete actor switching if Facebook asks, then require an actual
    # composer. A switch prompt is also a role=dialog, so waiting for any dialog
    # is not enough and causes false composer-open detection.
    logger.info(f'📄 Pages Portal: waiting for composer or Switch modal to open')
    switched = await _handle_pages_portal_profile_switch(page, page_name)
    if switched and not await _wait_for_pages_portal_composer(page, timeout_ms=2500):
        logger.info(f'📄 Pages Portal: retrying Create post for "{page_name}" after profile switch')
        create_post_clicked = await _click_pages_portal_create_post(page, page_name)
        if create_post_clicked:
            await _handle_pages_portal_profile_switch(page, page_name)

    composer_open = await _wait_for_pages_portal_composer(page, timeout_ms=10000)
    if not composer_open:
        # Some Facebook variants navigate to the Page profile and expose the regular composer.
        if await _open_visible_page_composer(page):
            logger.info(f'📄 Pages Portal: opened page-profile composer fallback')
            composer_open = True
    if not composer_open and target_url:
        logger.info(f'📄 Pages Portal: opening target page fallback after portal composer miss: {_safe_log_url(target_url)}')
        try:
            await page.goto(target_url, wait_until='domcontentloaded', timeout=30000)
            fatal_detail = await _facebook_navigation_fatal_block_detail(page)
            if fatal_detail:
                logger.warning(f'📄 Pages Portal target fallback blocked: {fatal_detail[:160]}')
            else:
                await _wait_for_facebook_ui_ready(page, timeout=3500)
                await _dismiss_common_facebook_popups(page)
                if await _click_onscreen_switch_button(page, page_name):
                    await _wait_for_profile_switch_to_settle(page, timeout_ms=6000)
                if await _open_visible_page_composer(page):
                    logger.info(f'📄 Pages Portal: opened target-page composer fallback')
                    composer_open = True
        except Exception as exc:
            logger.warning(f'📄 Pages Portal target-page composer fallback failed: {exc}')

    if not composer_open:
        diagnostic_path = await _save_diagnostics(page, 'pages_portal_composer_not_opened')
        return False, _with_diagnostic(
            'Composer dialog did not open after clicking "Create post".',
            diagnostic_path,
        )

    logger.info(f'📄 Pages Portal: composer opened successfully')

    if post_type in {'image', 'video'} and not media_url:
        return False, f'Missing {post_type} media URL; refusing to publish a media post without media.'

    # Step 3: Fill optional caption
    logger.info(f'📄 Pages Portal: typing caption')
    await _fill_composer_caption(page, caption)

    # Step 3b: Handle media if needed
    media_ok, media_error = await _attach_composer_media(page, post_type, media_url, 'pages_portal')
    if not media_ok:
        return False, media_error
    security_detail = await _facebook_navigation_fatal_block_detail(page)
    if security_detail:
        diagnostic_path = await _save_diagnostics(page, 'pages_portal_security_checkpoint')
        return False, _with_diagnostic(security_detail, diagnostic_path)

    # Step 4 & 5: Submit the composer (clicks Next and Post/Publish automatically using robust logic)
    logger.info(f'📄 Pages Portal: submitting the composer dialog')
    publish_monitor = _start_post_network_monitor(page)
    posted = await _submit_desktop_composer(page)
    if not posted and await _resume_unsaved_post_prompt(page):
        logger.info('📄 Pages Portal: unsaved-post prompt interrupted submit; retrying composer submit')
        posted = await _submit_desktop_composer(page)

    if not posted:
        if publish_monitor is not None:
            publish_monitor.stop()
        security_detail = await _facebook_navigation_fatal_block_detail(page)
        if security_detail:
            diagnostic_path = await _save_diagnostics(page, 'pages_portal_security_checkpoint')
            return False, _with_diagnostic(security_detail, diagnostic_path)
        diagnostic_path = await _save_diagnostics(page, 'pages_portal_post_button_not_found')
        return False, _with_diagnostic(
            'Could not publish the post through the Pages Portal composer dialog.',
            diagnostic_path,
        )

    network_result, network_reason = await _await_initial_publish_confirmation(
        page,
        publish_monitor,
        caption=caption,
        post_type=post_type,
    )
    if network_result is True:
        logger.info(f'📄 Pages Portal: post published successfully for page "{page_name}" ({network_reason})')
        return True, f'Post accepted by Facebook on page "{page_name}" via Pages Portal. {network_reason}'
    if network_result is False:
        security_detail = await _facebook_navigation_fatal_block_detail(page)
        if security_detail:
            diagnostic_path = await _save_diagnostics(page, 'pages_portal_security_checkpoint')
            return False, _with_diagnostic(security_detail, diagnostic_path)
        diagnostic_path = await _save_diagnostics(page, 'pages_portal_publish_graphql_failed')
        return False, _publish_sent_unconfirmed(
            f'Clicked publish in Pages Portal, but Facebook did not confirm the post. {network_reason}',
            diagnostic_path,
        )

    posting_settled, posting_reason = await _wait_for_facebook_posting_to_settle(page)
    if not posting_settled:
        diagnostic_path = await _save_diagnostics(page, 'pages_portal_publish_still_posting')
        if _is_facebook_security_failure(posting_reason):
            return False, _with_diagnostic(posting_reason, diagnostic_path)
        return False, _publish_sent_unconfirmed(
            f'Clicked publish in Pages Portal, but Facebook did not finish processing. {posting_reason}',
            diagnostic_path,
        )
    logger.info(f'📄 Pages Portal: publish processing check: {posting_reason}')

    # Step 6: Handle post-publish popups dynamically
    logger.info(f'📄 Pages Portal: handling post-publish popups')
    # Dynamic wait: check if the composer dialog disappears (post published)
    try:
        await page.wait_for_function(
            """() => {
                const dialogs = document.querySelectorAll('div[role="dialog"]');
                return dialogs.length === 0 || Array.from(dialogs).every(d => d.offsetParent === null);
            }""",
            timeout=8000,
        )
        logger.info('📄 Pages Portal: composer dialog closed (post published)')
    except Exception:
        await asyncio.sleep(1.0)

    # Handle multiple popup types with retries
    for popup_attempt in range(3):
        handled = False

        # Check for "Hosting an event?" / "Publish Original Post" popup
        try:
            publish_original = page.get_by_text(re.compile(r'^Publish Original Post$|^نشر المنشور الأصلي$', re.I)).first
            if await publish_original.is_visible(timeout=1500):
                await publish_original.click(timeout=3000)
                logger.info(f'📄 Pages Portal: clicked "Publish Original Post"')
                handled = True
                await asyncio.sleep(2.0)
                continue
        except Exception:
            pass

        # Check for WhatsApp "Not now" popup
        try:
            not_now = page.get_by_text(re.compile(r'^Not now$|^ليس الآن$|^لاحقاً$', re.I)).first
            if await not_now.is_visible(timeout=1500):
                await not_now.click(timeout=3000)
                logger.info(f'📄 Pages Portal: clicked "Not now" (WhatsApp popup dismissed)')
                handled = True
                await asyncio.sleep(1.5)
                continue
        except Exception:
            pass

        # Generic popup dismissal
        await _dismiss_common_facebook_popups(page)

        # Check if all dialogs are closed
        try:
            dialog_visible = await page.locator("div[role='dialog']").last.is_visible(timeout=1000)
            if not dialog_visible:
                logger.info(f'📄 Pages Portal: all dialogs closed')
                break
        except Exception:
            break

        if not handled:
            # Try closing via X button
            try:
                close_btn = page.locator("[aria-label='Close'], [aria-label='إغلاق']").first
                if await close_btn.is_visible(timeout=1000):
                    await close_btn.click(timeout=2000)
                    logger.info(f'📄 Pages Portal: closed dialog via X button')
            except Exception:
                pass
            break

    verify_timeout_ms = _adaptive_verify_timeout_ms(post_type, bool(media_url))
    verified, verify_reason = await _verify_post_published_with_target_fallback(
        page,
        caption=caption,
        post_type=post_type,
        timeout_ms=verify_timeout_ms,
        accept_publish_click=post_type == 'video' and POST_ACCEPT_VIDEO_PUBLISH_CLICK_AS_SUCCESS,
        target_url=target_url,
        page_name=page_name,
    )
    if not verified:
        diagnostic_path = await _save_diagnostics(page, 'pages_portal_publish_unverified')
        return False, _publish_sent_unconfirmed(
            f'Clicked publish in Pages Portal, but Facebook did not confirm the post. {verify_reason}',
            diagnostic_path,
        )

    logger.info(f'📄 Pages Portal: post published successfully for page "{page_name}"')
    return True, f'Post accepted by Facebook on page "{page_name}" via Pages Portal. {verify_reason}'


def _derive_page_name(target_url: str, page_name: Optional[str] = None) -> str:
    if page_name:
        return page_name
    parsed_target = urlparse(target_url)
    path_parts = [p for p in parsed_target.path.split('/') if p]
    derived = path_parts[0] if path_parts else ''
    return '' if derived == 'profile.php' else derived


async def _create_facebook_post_in_existing_page(
    page: Page,
    page_id_or_url: str,
    post_type: str,
    caption: str,
    media_url: Optional[str] = None,
    page_name: Optional[str] = None,
    progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
) -> Tuple[bool, str]:
    post_started_at = asyncio.get_running_loop().time()
    target_url = page_id_or_url if page_id_or_url.startswith('http') else f"https://www.facebook.com/{page_id_or_url}"
    derived_page_name = _derive_page_name(target_url, page_name)
    page_label = derived_page_name or page_name or page_id_or_url
    mobile_attempted = False

    async def progress(stage: str, detail: str = '') -> None:
        _post_step(page_label, stage, detail)
        if progress_callback is not None:
            await progress_callback({'page': page_label, 'stage': stage, 'detail': detail})

    async def try_mobile_fallback(reason: str) -> Optional[Tuple[bool, str]]:
        nonlocal mobile_attempted
        if mobile_attempted or not _mobile_fallback_enabled(post_type, bool(media_url)):
            return None
        mobile_attempted = True
        await progress('Trying mobile composer', f'fallback after {reason[:90]}')
        mobile_success, mobile_result = await _create_post_mobile(
            page,
            target_url,
            caption,
            post_type,
            media_url,
        )
        if mobile_success:
            logger.info(
                f'POST_FLOW stage="finished" page="{page_label}" mode=mobile_fallback '
                f'elapsed={asyncio.get_running_loop().time() - post_started_at:.1f}s success=True'
            )
            return True, mobile_result
        if _is_publish_sent_unconfirmed(mobile_result):
            logger.warning(f'Mobile fallback publish was unconfirmed for {target_url}; suppressing fallback to avoid duplicates.')
            return False, mobile_result
        logger.info(f'Mobile fallback failed for {target_url}: {mobile_result}')
        return None

    await progress('Starting post', f'type={post_type} target={_safe_log_url(target_url)}')
    if POST_MOBILE_FIRST_ENABLED or POST_MOBILE_ONLY_ENABLED:
        mobile_attempted = True
        mobile_detail = 'primary_route=m.facebook.com'
        if POST_MOBILE_ONLY_ENABLED:
            mobile_detail += ' mobile_only=true'
        await progress('Trying mobile composer', mobile_detail)
        mobile_success, mobile_result = await _create_post_mobile(
            page,
            target_url,
            caption,
            post_type,
            media_url,
        )
        if mobile_success:
            logger.info(
                f'POST_FLOW stage="finished" page="{page_label}" mode=mobile '
                f'elapsed={asyncio.get_running_loop().time() - post_started_at:.1f}s success=True'
            )
            return True, mobile_result
        if _is_publish_sent_unconfirmed(mobile_result):
            logger.warning(f'Mobile composer publish was unconfirmed for {target_url}; suppressing fallback to avoid duplicates.')
            return False, mobile_result
        if POST_MOBILE_ONLY_ENABLED:
            return False, mobile_result
        logger.info(f'Mobile composer failed for {target_url}; falling back to desktop/portal flow: {mobile_result}')
    else:
        logger.info(f'Mobile composer skipped for {target_url}; POST_MOBILE_FIRST_ENABLED=false')

    portal_first_failure_detail = ''
    portal_first_timed_out = False
    portal_first_enabled = _pages_portal_first_enabled(post_type, bool(media_url))
    if derived_page_name and portal_first_enabled:
        portal_timeout = _pages_portal_timeout_seconds(post_type, bool(media_url))
        await progress('Trying Pages Portal', f'timeout={portal_timeout}s')
        logger.info(f'🚀 Trying Pages Portal strategy for page: "{derived_page_name}"')
        try:
            portal_success, portal_result = await asyncio.wait_for(
                _post_via_pages_portal(page, derived_page_name, caption, post_type, media_url, target_url),
                timeout=portal_timeout,
            )
            if portal_success:
                return True, portal_result
            if _is_publish_sent_unconfirmed(portal_result):
                logger.warning(f'Pages Portal publish was unconfirmed for "{derived_page_name}"; suppressing fallback to avoid duplicates.')
                return False, portal_result
            portal_first_failure_detail = portal_result
            logger.warning(f'Pages Portal strategy failed: {portal_result}')
            if _is_media_upload_rejection(portal_result):
                return False, portal_result
        except asyncio.TimeoutError:
            portal_first_failure_detail = f'Pages Portal timed out after {portal_timeout}s.'
            portal_first_timed_out = True
            logger.warning('Pages Portal strategy timed out')
        except Exception as e:
            portal_first_failure_detail = f'Pages Portal error: {e}'
            logger.warning(f'Pages Portal strategy error: {e}')

    if POST_PREFER_DIRECT_POSTING:
        if (
            portal_first_timed_out
            and bool(media_url)
            and POST_SKIP_DIRECT_AFTER_MEDIA_PORTAL_TIMEOUT
        ):
            return False, portal_first_failure_detail
        direct_timeout = min(FACEBOOK_POST_TIMEOUT_SECONDS, POST_DIRECT_COMPOSER_TIMEOUT_SECONDS)
        await progress('Opening composer (direct)', f'timeout={direct_timeout}s')
        direct_success, direct_result = await _create_facebook_post_direct(
            page,
            page_id_or_url,
            post_type,
            caption,
            media_url,
            derived_page_name,
            progress_callback,
            try_mobile=False,
            allow_portal_fallback=not portal_first_enabled,
        )
        if direct_success:
            logger.info(
                f'POST_FLOW stage="finished" page="{page_label}" mode=direct '
                f'elapsed={asyncio.get_running_loop().time() - post_started_at:.1f}s success=True'
            )
            return True, direct_result
        if _is_publish_sent_unconfirmed(direct_result):
            logger.warning(f'Direct composer publish was unconfirmed for {target_url}; suppressing fallback to avoid duplicates.')
            return False, direct_result
        if _is_media_upload_rejection(direct_result):
            if portal_first_failure_detail:
                return False, f'Pages Portal failed first: {portal_first_failure_detail}; Direct composer failed: {direct_result}'
            return False, direct_result
        logger.info(f'Direct composer path failed for {target_url}; evaluating remaining fallback routes.')
        direct_already_tried_portal = 'pages portal' in str(direct_result or '').lower()
        if (
            derived_page_name
            and not portal_first_enabled
            and _pages_portal_fallback_enabled(post_type, bool(media_url))
            and not direct_already_tried_portal
        ):
            portal_timeout = _pages_portal_timeout_seconds(post_type, bool(media_url))
            await progress('Trying Pages Portal', f'direct composer unavailable; timeout={portal_timeout}s')
            logger.info(f'🚀 Direct composer unavailable; trying Pages Portal fallback for page: "{derived_page_name}"')
            try:
                portal_success, portal_result = await asyncio.wait_for(
                    _post_via_pages_portal(page, derived_page_name, caption, post_type, media_url, target_url),
                    timeout=portal_timeout,
                )
                if portal_success:
                    return True, portal_result
                if _is_publish_sent_unconfirmed(portal_result):
                    logger.warning(f'Pages Portal publish was unconfirmed for "{derived_page_name}"; suppressing fallback to avoid duplicates.')
                    return False, portal_result
                logger.warning(f'Pages Portal fallback after direct failure failed: {portal_result}')
                return False, portal_result
            except asyncio.TimeoutError:
                logger.warning('Pages Portal fallback after direct failure timed out')
                return False, (
                    'Could not publish through direct composer or Pages Portal. '
                    f'Pages Portal timed out after {portal_timeout}s.'
                )
            except Exception as exc:
                logger.warning(f'Pages Portal fallback after direct failure error: {exc}')
                return False, f'Could not publish through direct composer or Pages Portal: {exc}'
        mobile_fallback_result = await try_mobile_fallback('direct composer failed')
        if mobile_fallback_result is not None:
            return mobile_fallback_result
        logger.info(
            'Skipping duplicate desktop composer fallback because direct mode already '
            'exhausted the configured composer route(s).'
        )
        if portal_first_failure_detail:
            return False, f'Pages Portal failed first: {portal_first_failure_detail}; Direct composer failed: {direct_result}'
        return False, direct_result

    logger.info(f'🔄 Trying desktop composer for target={target_url}')
    composer_timeout = min(FACEBOOK_POST_TIMEOUT_SECONDS, POST_DESKTOP_COMPOSER_TIMEOUT_SECONDS)
    await progress('Opening desktop composer', f'timeout={composer_timeout}s')
    try:
        create_clicked = await asyncio.wait_for(
            _open_desktop_composer(page, target_url, page_name=derived_page_name),
            timeout=composer_timeout,
        )
    except asyncio.TimeoutError:
        create_clicked = False

    if not create_clicked:
        if derived_page_name and not portal_first_enabled and _pages_portal_fallback_enabled(post_type, bool(media_url)):
            logger.info(f'🚀 Desktop composer unavailable; trying Pages Portal fallback for page: "{derived_page_name}"')
            portal_timeout = _pages_portal_timeout_seconds(post_type, bool(media_url))
            try:
                portal_success, portal_result = await asyncio.wait_for(
                    _post_via_pages_portal(page, derived_page_name, caption, post_type, media_url, target_url),
                    timeout=portal_timeout,
                )
                if portal_success:
                    return True, portal_result
                if _is_publish_sent_unconfirmed(portal_result):
                    logger.warning(f'Pages Portal publish was unconfirmed for "{derived_page_name}"; suppressing fallback to avoid duplicates.')
                    return False, portal_result
                logger.warning(f'Pages Portal fallback failed: {portal_result}')
            except asyncio.TimeoutError:
                logger.warning('Pages Portal fallback timed out')
            except Exception as e:
                logger.warning(f'Pages Portal fallback error: {e}')
        diagnostic_path = await _save_diagnostics(page, 'desktop_composer_not_opened')
        return False, _with_diagnostic(
            'Could not open the desktop composer or Pages Portal composer.',
            diagnostic_path,
        )

    logger.info(f'Facebook composer opened for target={target_url}')
    if _is_ad_flow_url(page.url):
        if derived_page_name and _pages_portal_fallback_enabled(post_type, bool(media_url)):
            await progress('Trying Pages Portal', 'desktop route opened ad flow')
            portal_success, portal_result = await _post_via_pages_portal(
                page,
                derived_page_name,
                caption,
                post_type,
                media_url,
                target_url,
            )
            if portal_success:
                return True, portal_result
            if _is_publish_sent_unconfirmed(portal_result):
                logger.warning(f'Pages Portal publish was unconfirmed for "{derived_page_name}"; suppressing fallback to avoid duplicates.')
                return False, portal_result
        diagnostic_path = await _save_diagnostics(page, 'desktop_composer_ad_flow')
        return False, _with_diagnostic(
            'Desktop composer opened an ad flow instead of the post composer.',
            diagnostic_path,
        )

    if post_type in {'image', 'video'} and not media_url:
        return False, f'Missing {post_type} media URL; refusing to publish a media post without media.'

    await progress('Typing caption', f'has_caption={bool(caption)}')
    await _fill_composer_caption(page, caption)

    if post_type in {'image', 'video'}:
        await progress(f'Attaching {post_type}', f'has_media={bool(media_url)}')
    media_ok, media_error = await _attach_composer_media(page, post_type, media_url, 'post')
    if not media_ok:
        return False, media_error
    security_detail = await _facebook_navigation_fatal_block_detail(page)
    if security_detail:
        diagnostic_path = await _save_diagnostics(page, 'post_security_checkpoint')
        return False, _with_diagnostic(security_detail, diagnostic_path)

    await progress('Submitting post')
    publish_monitor = _start_post_network_monitor(page)
    posted = await _submit_desktop_composer(page)
    if not posted:
        if publish_monitor is not None:
            publish_monitor.stop()
        security_detail = await _facebook_navigation_fatal_block_detail(page)
        if security_detail:
            diagnostic_path = await _save_diagnostics(page, 'post_security_checkpoint')
            return False, _with_diagnostic(security_detail, diagnostic_path)
        diagnostic_path = await _save_diagnostics(page, 'post_publish_button_missing')
        return False, _with_diagnostic(
            'Could not find a publish control in the desktop composer.',
            diagnostic_path,
        )

    network_result, network_reason = await _await_initial_publish_confirmation(
        page,
        publish_monitor,
        caption=caption,
        post_type=post_type,
    )
    if network_result is True:
        logger.info(f'Playwright posting completed for target={page_id_or_url} ({network_reason})')
        await progress('Published')
        logger.info(
            f'POST_FLOW stage="finished" page="{page_label}" mode=desktop '
            f'elapsed={asyncio.get_running_loop().time() - post_started_at:.1f}s success=True'
        )
        return True, f"Post accepted by Facebook via Playwright. {network_reason}"
    if network_result is False:
        security_detail = await _facebook_navigation_fatal_block_detail(page)
        if security_detail:
            diagnostic_path = await _save_diagnostics(page, 'post_security_checkpoint')
            return False, _with_diagnostic(security_detail, diagnostic_path)
        diagnostic_path = await _save_diagnostics(page, 'post_publish_graphql_failed')
        return False, _publish_sent_unconfirmed(
            f'Clicked publish, but Facebook did not confirm the post. {network_reason}',
            diagnostic_path,
        )

    await progress('Confirming publish')
    posting_settled, posting_reason = await _wait_for_facebook_posting_to_settle(page)
    if not posting_settled:
        diagnostic_path = await _save_diagnostics(page, 'post_publish_still_posting')
        if _is_facebook_security_failure(posting_reason):
            return False, _with_diagnostic(posting_reason, diagnostic_path)
        return False, _publish_sent_unconfirmed(
            f'Clicked publish, but Facebook did not finish processing. {posting_reason}',
            diagnostic_path,
        )
    logger.info(f'Desktop composer publish processing check: {posting_reason}')
    try:
        await _dismiss_common_facebook_popups(page)
        await page.locator("div[role='dialog']").last.wait_for(state='hidden', timeout=8000)
    except Exception:
        try:
            await _dismiss_common_facebook_popups(page)
        except Exception:
            pass
        await asyncio.sleep(2.0)

    verify_timeout_ms = _adaptive_verify_timeout_ms(post_type, bool(media_url))
    verified, verify_reason = await _verify_post_published_with_target_fallback(
        page,
        caption=caption,
        post_type=post_type,
        timeout_ms=verify_timeout_ms,
        accept_publish_click=post_type == 'video' and POST_ACCEPT_VIDEO_PUBLISH_CLICK_AS_SUCCESS,
        target_url=target_url,
        page_name=derived_page_name or page_label,
    )
    if not verified:
        diagnostic_path = await _save_diagnostics(page, 'post_publish_unverified')
        return False, _publish_sent_unconfirmed(
            f'Clicked publish, but Facebook did not confirm the post. {verify_reason}',
            diagnostic_path,
        )

    logger.info(f'Playwright posting completed for target={page_id_or_url}')
    await progress('Published')
    logger.info(
        f'POST_FLOW stage="finished" page="{page_label}" mode=desktop '
        f'elapsed={asyncio.get_running_loop().time() - post_started_at:.1f}s success=True'
    )
    return True, f"Post accepted by Facebook via Playwright. {verify_reason}"


async def _create_facebook_post_direct(
    page: Page,
    page_id_or_url: str,
    post_type: str,
    caption: str,
    media_url: Optional[str] = None,
    page_name: Optional[str] = None,
    progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    try_mobile: Optional[bool] = None,
    allow_portal_fallback: bool = True,
) -> Tuple[bool, str]:
    """
    FAST direct-URL posting with optional mobile-first and Pages Portal fallback.

    Flow: navigate to page URL → switch profile → open composer → type → submit.
    ~20s faster per page compared to Pages Portal.
    Used by parallel workers for maximum speed.
    Falls back to Pages Portal only if the direct approach completely fails.
    """
    post_started_at = asyncio.get_running_loop().time()
    target_url = page_id_or_url if page_id_or_url.startswith('http') else f"https://www.facebook.com/{page_id_or_url}"
    derived_page_name = _derive_page_name(target_url, page_name)
    page_label = derived_page_name or page_name or page_id_or_url

    async def progress(stage: str, detail: str = '') -> None:
        _post_step(page_label, stage, detail)
        if progress_callback is not None:
            await progress_callback({'page': page_label, 'stage': stage, 'detail': detail})

    await progress('Starting post (direct)', f'type={post_type} target={_safe_log_url(target_url)}')
    if try_mobile is None:
        try_mobile = POST_MOBILE_FIRST_ENABLED
    if try_mobile:
        await progress('Trying mobile composer', 'primary_route=m.facebook.com')
        mobile_success, mobile_result = await _create_post_mobile(
            page,
            target_url,
            caption,
            post_type,
            media_url,
        )
        if mobile_success:
            logger.info(
                f'POST_FLOW stage="finished" page="{page_label}" mode=mobile '
                f'elapsed={asyncio.get_running_loop().time() - post_started_at:.1f}s success=True'
            )
            return True, mobile_result
        if _is_publish_sent_unconfirmed(mobile_result):
            logger.warning(f'Mobile composer publish was unconfirmed for {target_url}; suppressing fallback to avoid duplicates.')
            return False, mobile_result
        logger.info(f'Mobile composer failed for {target_url}; falling back to direct/portal flow: {mobile_result}')
    else:
        logger.info(f'Mobile composer skipped for {target_url}; POST_MOBILE_FIRST_ENABLED=false')

    # ── Step 1: Go directly to the page URL and open the desktop composer ──
    logger.info(f'⚡ Direct posting: navigating to {_safe_log_url(target_url)}')
    composer_timeout = min(FACEBOOK_POST_TIMEOUT_SECONDS, POST_DIRECT_COMPOSER_TIMEOUT_SECONDS)
    await progress('Opening composer (direct)', f'timeout={composer_timeout}s')
    try:
        create_clicked = await asyncio.wait_for(
            _open_desktop_composer(page, target_url, page_name=derived_page_name),
            timeout=composer_timeout,
        )
    except asyncio.TimeoutError:
        create_clicked = False

    if not create_clicked:
        # ── Fallback: try Pages Portal only when explicitly enabled ──
        if derived_page_name and allow_portal_fallback and _pages_portal_fallback_enabled(post_type, bool(media_url)):
            logger.info(f'⚡ Direct posting failed; falling back to Pages Portal for "{derived_page_name}"')
            portal_timeout = _pages_portal_timeout_seconds(post_type, bool(media_url))
            await progress('Fallback to Pages Portal', f'timeout={portal_timeout}s')
            try:
                portal_success, portal_result = await asyncio.wait_for(
                    _post_via_pages_portal(page, derived_page_name, caption, post_type, media_url, target_url),
                    timeout=portal_timeout,
                )
                if portal_success:
                    return True, portal_result
                if _is_publish_sent_unconfirmed(portal_result):
                    logger.warning(f'Pages Portal publish was unconfirmed for "{derived_page_name}"; suppressing fallback to avoid duplicates.')
                    return False, portal_result
                logger.warning(f'Pages Portal fallback failed: {portal_result}')
            except asyncio.TimeoutError:
                logger.warning('Pages Portal fallback timed out')
            except Exception as e:
                logger.warning(f'Pages Portal fallback error: {e}')
        diagnostic_path = await _save_diagnostics(page, 'direct_post_all_routes_failed')
        return False, _with_diagnostic(
            'Could not publish through direct composer or Pages Portal.'
            if allow_portal_fallback and _pages_portal_fallback_enabled(post_type, bool(media_url))
            else 'Could not publish through direct composer. Pages Portal fallback is disabled.',
            diagnostic_path,
        )

    logger.info(f'⚡ Direct posting: composer opened for {_safe_log_url(target_url)}')
    if _is_ad_flow_url(page.url):
        portal_result = 'Pages Portal fallback is disabled.'
        if allow_portal_fallback and _pages_portal_fallback_enabled(post_type, bool(media_url)):
            await progress('Trying Pages Portal', 'ad flow detected')
            portal_success, portal_result = await _post_via_pages_portal(page, derived_page_name or page_label, caption, post_type, media_url, target_url)
            if portal_success:
                return True, portal_result
            if _is_publish_sent_unconfirmed(portal_result):
                logger.warning(f'Pages Portal publish was unconfirmed for "{derived_page_name or page_label}"; suppressing fallback to avoid duplicates.')
                return False, portal_result
        diagnostic_path = await _save_diagnostics(page, 'direct_post_ad_flow_failed')
        return False, _with_diagnostic(
            f'Could not publish through direct composer.\nPages Portal: {portal_result}',
            diagnostic_path,
        )

    if post_type in {'image', 'video'} and not media_url:
        return False, f'Missing {post_type} media URL; refusing to publish a media post without media.'

    # ── Step 2: Fill caption ──
    await progress('Typing caption', f'has_caption={bool(caption)}')
    await _fill_composer_caption(page, caption)

    # ── Step 3: Attach media if needed ──
    if post_type in {'image', 'video'}:
        await progress(f'Attaching {post_type}', f'has_media={bool(media_url)}')
    media_ok, media_error = await _attach_composer_media(page, post_type, media_url, 'direct_post')
    if not media_ok:
        return False, media_error
    security_detail = await _facebook_navigation_fatal_block_detail(page)
    if security_detail:
        diagnostic_path = await _save_diagnostics(page, 'direct_post_security_checkpoint')
        return False, _with_diagnostic(security_detail, diagnostic_path)

    # ── Step 4: Submit ──
    await progress('Submitting post')
    publish_monitor = _start_post_network_monitor(page)
    posted = await _submit_desktop_composer(page)
    if not posted:
        if publish_monitor is not None:
            publish_monitor.stop()
        security_detail = await _facebook_navigation_fatal_block_detail(page)
        if security_detail:
            diagnostic_path = await _save_diagnostics(page, 'direct_post_security_checkpoint')
            return False, _with_diagnostic(security_detail, diagnostic_path)
        portal_result = 'Pages Portal fallback is disabled.'
        if allow_portal_fallback and _pages_portal_fallback_enabled(post_type, bool(media_url)):
            await progress('Trying Pages Portal', 'submit did not find publish control')
            portal_success, portal_result = await _post_via_pages_portal(page, derived_page_name or page_label, caption, post_type, media_url, target_url)
            if portal_success:
                return True, portal_result
            if _is_publish_sent_unconfirmed(portal_result):
                logger.warning(f'Pages Portal publish was unconfirmed for "{derived_page_name or page_label}"; suppressing fallback to avoid duplicates.')
                return False, portal_result
        diagnostic_path = await _save_diagnostics(page, 'direct_post_publish_missing')
        return False, _with_diagnostic(
            f"Could not publish through direct or Pages Portal composer.\nPages Portal: {portal_result}",
            diagnostic_path,
        )

    network_result, network_reason = await _await_initial_publish_confirmation(
        page,
        publish_monitor,
        caption=caption,
        post_type=post_type,
    )
    if network_result is True:
        logger.info(f'⚡ Direct posting completed for {_safe_log_url(target_url)} ({network_reason})')
        await progress('Published')
        return True, f"Post accepted by Facebook via direct composer. {network_reason}"
    if network_result is False:
        security_detail = await _facebook_navigation_fatal_block_detail(page)
        if security_detail:
            diagnostic_path = await _save_diagnostics(page, 'direct_post_security_checkpoint')
            return False, _with_diagnostic(security_detail, diagnostic_path)
        diagnostic_path = await _save_diagnostics(page, 'direct_post_publish_graphql_failed')
        return False, _publish_sent_unconfirmed(
            f'Clicked publish, but Facebook did not confirm the post. {network_reason}',
            diagnostic_path,
        )

    # ── Step 5: Confirm publish ──
    await progress('Confirming publish')
    posting_settled, posting_reason = await _wait_for_facebook_posting_to_settle(page)
    if not posting_settled:
        diagnostic_path = await _save_diagnostics(page, 'direct_post_publish_still_posting')
        if _is_facebook_security_failure(posting_reason):
            return False, _with_diagnostic(posting_reason, diagnostic_path)
        return False, _publish_sent_unconfirmed(
            f'Clicked publish, but Facebook did not finish processing. {posting_reason}',
            diagnostic_path,
        )
    logger.info(f'Direct composer publish processing check: {posting_reason}')
    try:
        await _dismiss_common_facebook_popups(page)
        await page.locator("div[role='dialog']").last.wait_for(state='hidden', timeout=8000)
    except Exception:
        try:
            await _dismiss_common_facebook_popups(page)
        except Exception:
            pass
        await asyncio.sleep(1.5)

    verify_timeout_ms = _adaptive_verify_timeout_ms(post_type, bool(media_url))
    verified, verify_reason = await _verify_post_published_with_target_fallback(
        page,
        caption=caption,
        post_type=post_type,
        timeout_ms=verify_timeout_ms,
        accept_publish_click=post_type == 'video' and POST_ACCEPT_VIDEO_PUBLISH_CLICK_AS_SUCCESS,
        target_url=target_url,
        page_name=derived_page_name or page_label,
    )
    if not verified:
        diagnostic_path = await _save_diagnostics(page, 'direct_post_publish_unverified')
        return False, _publish_sent_unconfirmed(
            f'Clicked publish, but Facebook did not confirm the post. {verify_reason}',
            diagnostic_path,
        )

    logger.info(f'⚡ Direct posting completed for {_safe_log_url(target_url)}')
    await progress('Published')
    return True, f"Post accepted by Facebook via direct composer. {verify_reason}"


async def _create_facebook_post_browser(
    cookies_json: str,
    page_id_or_url: str,
    post_type: str,
    caption: str,
    media_url: Optional[str] = None,
    page_name: Optional[str] = None,
    progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
) -> Tuple[bool, str]:
    """
    Automate creating a post on Facebook using the configured browser route strategy.

    Strategy priority:
    1. Desktop deep-link/direct composer - fastest browser route.
    2. Pages Portal fallback when enabled/needed.
    3. Optional mobile composer only when POST_MOBILE_FIRST_ENABLED=true.
    """
    session_guard = None
    session_success = False
    session_detail = ''
    try:
        session_guard = await _acquire_cookie_session_guard(cookies_json, 'single post')
    except Exception as exc:
        logger.error(f'Could not acquire cookie session lock for posting: {exc}')
        return False, str(exc)

    try:
        playwright, browser, context, page = await launch_browser_session(cookies_json)
    except Exception as exc:
        logger.error(f'Could not launch browser for posting: {exc}')
        await asyncio.to_thread(_mark_cookie_session_used, cookies_json, False, str(exc))
        await _release_cookie_session_guard(session_guard)
        return False, str(exc)

    try:
        logger.info(f'Playwright posting started for target={page_id_or_url} type={post_type}')
        await _enable_fast_posting_mode(context, page)
        await page.goto("https://www.facebook.com/", wait_until='domcontentloaded', timeout=45000)
        await _resume_facebook_cookie_session(page)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            logger.debug('Facebook home did not reach networkidle; continuing after domcontentloaded.')

        security_detail = await _facebook_security_block_detail(page)
        if security_detail:
            diagnostic_path = await _save_diagnostics(page, 'post_security_checkpoint')
            session_detail = _with_diagnostic(security_detail, diagnostic_path)
            return False, session_detail

        # Verify login
        if await _page_looks_logged_out(page):
            diagnostic_path = await _save_diagnostics(page, 'post_login_or_checkpoint')
            session_detail = _with_diagnostic("Cookies expired or invalid. Login page detected.", diagnostic_path)
            return False, session_detail

        success, result = await _create_facebook_post_in_existing_page(
            page,
            page_id_or_url,
            post_type,
            caption,
            media_url,
            page_name,
            progress_callback,
        )
        session_success = success
        session_detail = '' if success else result
        return success, result

    except asyncio.TimeoutError:
        logger.error(f'Playwright posting timed out for target={page_id_or_url}')
        diagnostic_path = await _save_diagnostics(page, 'post_timeout')
        session_detail = _with_diagnostic(
            f'Facebook composer did not open within {min(FACEBOOK_POST_TIMEOUT_SECONDS, POST_DESKTOP_COMPOSER_TIMEOUT_SECONDS)}s. '
            'Facebook did not expose the expected composer controls.',
            diagnostic_path,
        )
        return False, session_detail
    except Exception as e:
        logger.error(f"Playwright automation failed: {e}")
        diagnostic_path = await _save_diagnostics(page, 'post_exception')
        session_detail = _with_diagnostic(str(e), diagnostic_path)
        return False, session_detail
    finally:
        await browser.close()
        await playwright.stop()
        await asyncio.to_thread(_mark_cookie_session_used, cookies_json, session_success, session_detail)
        await _release_cookie_session_guard(session_guard)


async def _try_create_facebook_posts_fast_http(
    cookies_json: str,
    posts: List[Dict[str, Any]],
    progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]],
    browser_fallback: Callable[[List[Dict[str, Any]]], Awaitable[List[Dict[str, Any]]]],
) -> Optional[List[Dict[str, Any]]]:
    try:
        from fast_graphql_poster import create_facebook_posts_fast, is_fast_graphql_enabled
    except Exception as exc:
        logger.debug(f'Fast HTTP GraphQL tier unavailable: {exc}')
        return None
    if not is_fast_graphql_enabled():
        return None
    try:
        return await create_facebook_posts_fast(
            cookies_json,
            posts,
            progress_callback,
            launch_browser_session=launch_browser_session,
            browser_fallback=browser_fallback,
        )
    except Exception as exc:
        logger.warning(f'Fast HTTP GraphQL tier failed before a safe result was produced; using browser path: {exc}')
        return None


async def create_facebook_post(
    cookies_json: str,
    page_id_or_url: str,
    post_type: str,
    caption: str,
    media_url: Optional[str] = None,
    page_name: Optional[str] = None,
    progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
) -> Tuple[bool, str]:
    post = {
        'page_id_or_url': page_id_or_url,
        'page_id': _page_id_from_url(page_id_or_url) or page_id_or_url,
        'page_name': page_name or page_id_or_url or 'Unknown page',
        'post_type': post_type,
        'caption': caption,
        'media_url': media_url or '',
    }

    async def browser_fallback(fallback_posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        fallback_results: List[Dict[str, Any]] = []
        for fallback_post in fallback_posts:
            fallback_page_label = str(
                fallback_post.get('page_name')
                or fallback_post.get('page_id_or_url')
                or 'Unknown page'
            )
            success, result = await _create_facebook_post_browser(
                cookies_json,
                str(fallback_post.get('page_id_or_url') or ''),
                str(fallback_post.get('post_type') or 'post'),
                str(fallback_post.get('caption') or ''),
                fallback_post.get('media_url') or None,
                fallback_page_label,
                progress_callback,
            )
            fallback_results.append({'page': fallback_page_label, 'success': success, 'result': result})
        return fallback_results

    fast_results = await _try_create_facebook_posts_fast_http(
        cookies_json,
        [post],
        progress_callback,
        browser_fallback,
    )
    if fast_results is not None and fast_results:
        first = fast_results[0]
        return bool(first.get('success')), str(first.get('result') or '')

    return await _create_facebook_post_browser(
        cookies_json,
        page_id_or_url,
        post_type,
        caption,
        media_url,
        page_name,
        progress_callback,
    )


def _post_result_is_safe_to_recover(detail: str) -> bool:
    text = str(detail or '').strip().lower()
    if not text:
        return False
    if _is_publish_sent_unconfirmed(text) or _is_facebook_security_failure(text):
        return False
    unsafe_markers = (
        'cookies expired',
        'login page detected',
        'redirected to login',
        'could not publish through direct composer',
        'could not open the desktop composer',
        'desktop_composer_not_opened',
        'missing image media',
        'missing video media',
        'refusing to publish a media post without media',
        'permission',
        'pages_manage_posts',
        'publish_video',
        '(#200)',
        '(#283)',
    )
    if any(marker in text for marker in unsafe_markers):
        return False
    retryable_markers = (
        'could not publish through direct composer',
        'could not open the desktop composer',
        'composer dialog did not open',
        'composer did not open',
        'composer not opened',
        'desktop composer unavailable',
        'direct_post_all_routes_failed',
        'desktop_composer_not_opened',
        'pages_portal_composer_not_opened',
        'pages portal fallback is disabled',
    )
    return any(marker in text for marker in retryable_markers)


async def _create_facebook_posts_unstaged(
    cookies_json: str,
    posts: List[Dict[str, Any]],
    progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
) -> List[Dict[str, Any]]:
    """Publish multiple page posts with one browser session and one login check."""
    session_guard = None
    session_success = False
    session_detail = ''
    if progress_callback is not None:
        await progress_callback({
            'page': 'Cookie session',
            'stage': 'Waiting for cookie session lock',
            'detail': f'Wait limit {POST_COOKIE_SESSION_LOCK_WAIT_SECONDS}s',
            'done': 0,
            'total': len(posts),
        })
    try:
        session_guard = await _acquire_cookie_session_guard(cookies_json, 'batch post')
    except Exception as exc:
        logger.error(f'Could not acquire cookie session lock for batch posting: {exc}')
        return [
            {
                'page': str(post.get('page_name') or post.get('page_id_or_url') or 'Unknown page'),
                'success': False,
                'result': str(exc),
            }
            for post in posts
        ]

    try:
        playwright, browser, context, page = await launch_browser_session(cookies_json)
    except Exception as exc:
        logger.error(f'Could not launch browser for batch posting: {exc}')
        await asyncio.to_thread(_mark_cookie_session_used, cookies_json, False, str(exc))
        await _release_cookie_session_guard(session_guard)
        return [
            {
                'page': str(post.get('page_name') or post.get('page_id_or_url') or 'Unknown page'),
                'success': False,
                'result': str(exc),
            }
            for post in posts
        ]

    try:
        logger.info(f'BATCH_STEP stage="started" total_pages={len(posts)}')
        await _enable_fast_posting_mode(context, page)
        await page.goto("https://www.facebook.com/", wait_until='domcontentloaded', timeout=45000)
        await _resume_facebook_cookie_session(page)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            logger.debug('Facebook home did not reach networkidle; continuing after domcontentloaded.')

        security_detail = await _facebook_security_block_detail(page)
        if security_detail:
            diagnostic_path = await _save_diagnostics(page, 'batch_post_security_checkpoint')
            detail = _with_diagnostic(security_detail, diagnostic_path)
            session_detail = detail
            return [
                {
                    'page': str(post.get('page_name') or post.get('page_id_or_url') or 'Unknown page'),
                    'success': False,
                    'result': detail,
                }
                for post in posts
            ]

        if await _page_looks_logged_out(page):
            diagnostic_path = await _save_diagnostics(page, 'batch_post_login_or_checkpoint')
            detail = _with_diagnostic("Cookies expired or invalid. Login page detected.", diagnostic_path)
            session_detail = detail
            return [
                {
                    'page': str(post.get('page_name') or post.get('page_id_or_url') or 'Unknown page'),
                    'success': False,
                    'result': detail,
                }
                for post in posts
            ]

        results: List[Dict[str, Any]] = []
        for idx, post in enumerate(posts):
            page_label = str(post.get('page_name') or post.get('page_id_or_url') or 'Unknown page')
            page_post_type = str(post.get('post_type') or 'post')
            page_has_media = bool(post.get('media_url'))
            page_timeout = _batch_page_timeout_seconds(page_post_type, page_has_media)
            batch_started_at = time.time()
            try:
                logger.info(
                    f'BATCH_STEP stage="page_start" index={idx + 1}/{len(posts)} '
                    f'page="{page_label}" type={page_post_type} timeout={page_timeout}s'
                )
                if progress_callback is not None:
                    await progress_callback({
                        'page': page_label,
                        'stage': 'Starting page',
                        'index': idx,
                        'total': len(posts),
                        'done': len(results),
                    })
                success, result = await asyncio.wait_for(
                    _create_facebook_post_in_existing_page(
                        page,
                        str(post.get('page_id_or_url') or ''),
                        page_post_type,
                        str(post.get('caption') or ''),
                        post.get('media_url') or None,
                        page_label,
                        progress_callback,
                    ),
                    timeout=page_timeout,
                )
            except asyncio.TimeoutError:
                diagnostic_path = await _save_diagnostics(page, 'batch_post_page_timeout')
                success = False
                result = _with_diagnostic(
                    f'Page posting timed out after {page_timeout}s. '
                    'The bot stopped this page to prevent the batch from freezing.',
                    diagnostic_path,
                )
                try:
                    timed_out_page: Page = page
                    page = await context.new_page()
                    if FACEBOOK_STEALTH_ASYNC_ENABLED:
                        await stealth_async(page)
                    await timed_out_page.close()
                except Exception as reset_exc:
                    logger.warning(f'Could not reset browser page after batch timeout: {reset_exc}')
            except Exception as exc:
                diagnostic_path = await _save_diagnostics(page, 'batch_post_exception')
                success = False
                result = _with_diagnostic(str(exc), diagnostic_path)
            retry_attempt = 0
            while (
                POST_PAGE_RECOVERY_RETRY_ENABLED
                and retry_attempt < POST_PAGE_RECOVERY_RETRY_MAX
                and not success
                and _post_result_is_safe_to_recover(str(result))
            ):
                retry_attempt += 1
                retry_page: Optional[Page] = None
                logger.info(
                    f'BATCH_STEP stage="page_recovery_retry" index={idx + 1}/{len(posts)} '
                    f'page="{page_label}" attempt={retry_attempt}/{POST_PAGE_RECOVERY_RETRY_MAX}'
                )
                if progress_callback is not None:
                    await progress_callback({
                        'page': page_label,
                        'stage': f'Recovery retry {retry_attempt}/{POST_PAGE_RECOVERY_RETRY_MAX}',
                        'index': idx,
                        'total': len(posts),
                        'done': len(results),
                    })
                try:
                    active_retry_page: Page = await context.new_page()
                    retry_page = active_retry_page
                    if FACEBOOK_STEALTH_ASYNC_ENABLED:
                        await stealth_async(active_retry_page)
                    await active_retry_page.goto("https://www.facebook.com/", wait_until='domcontentloaded', timeout=45000)
                    await _resume_facebook_cookie_session(active_retry_page)
                    try:
                        await active_retry_page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        logger.debug('Retry page did not reach networkidle; continuing after domcontentloaded.')

                    retry_security_detail = await _facebook_security_block_detail(active_retry_page)
                    if retry_security_detail:
                        diagnostic_path = await _save_diagnostics(active_retry_page, 'batch_post_recovery_security_checkpoint')
                        success = False
                        result = _with_diagnostic(retry_security_detail, diagnostic_path)
                    elif await _page_looks_logged_out(active_retry_page):
                        diagnostic_path = await _save_diagnostics(active_retry_page, 'batch_post_recovery_login_or_checkpoint')
                        success = False
                        result = _with_diagnostic("Cookies expired or invalid. Login page detected.", diagnostic_path)
                    else:
                        success, result = await asyncio.wait_for(
                            _create_facebook_post_in_existing_page(
                                active_retry_page,
                                str(post.get('page_id_or_url') or ''),
                                page_post_type,
                                str(post.get('caption') or ''),
                                post.get('media_url') or None,
                                page_label,
                                progress_callback,
                            ),
                            timeout=page_timeout,
                        )

                    old_page: Page = page
                    page = active_retry_page
                    retry_page = None
                    try:
                        await old_page.close()
                    except Exception:
                        pass
                except Exception as exc:
                    diagnostic_path = await _save_diagnostics(retry_page, 'batch_post_recovery_exception') if retry_page else ''
                    retry_error = _with_diagnostic(str(exc), diagnostic_path) if diagnostic_path else str(exc)
                    result = f'{result}\nRecovery retry failed: {retry_error}'
                    break
                finally:
                    if retry_page is not None:
                        try:
                            await retry_page.close()
                        except Exception:
                            pass
            item = {'page': page_label, 'success': success, 'result': result}
            results.append(item)
            logger.info(
                f'BATCH_STEP stage="page_done" index={idx + 1}/{len(posts)} '
                f'page="{page_label}" success={success} elapsed={time.time() - batch_started_at:.1f}s'
            )
            if progress_callback is not None:
                await progress_callback({
                    **item,
                    'stage': 'Done' if success else 'Failed',
                    'post_type': page_post_type,
                    'index': idx,
                    'total': len(posts),
                    'done': len(results),
                    'completed': True,
                })
            if not success and _is_facebook_security_failure(str(result)):
                skipped_detail = (
                    'Skipped because Facebook reported a security checkpoint/account lock on this cookie session. '
                    'Unlock the account manually from a trusted device and re-add fresh cookies.'
                )
                for remaining in posts[idx + 1:]:
                    skipped = {
                        'page': str(remaining.get('page_name') or remaining.get('page_id_or_url') or 'Unknown page'),
                        'success': False,
                        'result': skipped_detail,
                    }
                    results.append(skipped)
                    logger.warning(
                        f'BATCH_STEP stage="page_skipped" page="{skipped["page"]}" '
                        'reason="security_checkpoint"'
                    )
                    if progress_callback is not None:
                        await progress_callback({
                            **skipped,
                            'stage': 'Skipped',
                            'index': len(results) - 1,
                            'total': len(posts),
                            'done': len(results),
                            'completed': True,
                        })
                break
        logger.info(
            f'BATCH_STEP stage="finished" success_count={sum(1 for item in results if item.get("success"))}/{len(results)}'
        )
        session_success = all(bool(item.get('success')) for item in results)
        session_detail = '' if session_success else _session_result_detail(results)
        return results
    finally:
        await browser.close()
        await playwright.stop()
        await asyncio.to_thread(_mark_cookie_session_used, cookies_json, session_success, session_detail)
        await _release_cookie_session_guard(session_guard)


async def _create_facebook_posts_browser(
    cookies_json: str,
    posts: List[Dict[str, Any]],
    progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
) -> List[Dict[str, Any]]:
    staged_paths: List[str] = []
    try:
        staged_posts, staged_paths = await _stage_batch_media_sources(posts)
        return await _create_facebook_posts_unstaged(cookies_json, staged_posts, progress_callback)
    finally:
        await asyncio.to_thread(_cleanup_staged_batch_media, staged_paths)


async def _create_facebook_posts_legacy(
    cookies_json: str,
    posts: List[Dict[str, Any]],
    progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
) -> List[Dict[str, Any]]:
    fast_results = await _try_create_facebook_posts_fast_http(
        cookies_json,
        posts,
        progress_callback,
        lambda fallback_posts: _create_facebook_posts_browser(cookies_json, fallback_posts, progress_callback),
    )
    if fast_results is not None:
        return fast_results
    return await _create_facebook_posts_browser(cookies_json, posts, progress_callback)


async def create_facebook_posts(
    cookies_json: str,
    posts: List[Dict[str, Any]],
    progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
) -> List[Dict[str, Any]]:
    try:
        from integration_adapter import IntegrationAdapter

        return await IntegrationAdapter(legacy_create_posts=_create_facebook_posts_legacy).create_posts(
            cookies_json,
            posts,
            progress_callback,
        )
    except Exception as exc:
        logger.warning(f'Integration adapter failed; using legacy batch path: {exc}')
        return await _create_facebook_posts_legacy(cookies_json, posts, progress_callback)


async def _create_facebook_posts_parallel_unstaged(
    cookies_json: str,
    posts: List[Dict[str, Any]],
    progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    max_parallel: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Publish multiple page posts in PARALLEL with isolated browser contexts.

    Each page gets its own BrowserContext (isolated cookies, cache, DOM state).
    A semaphore limits concurrency to avoid overwhelming the system.

    Architecture:
    ┌─────────────────────────────────────────┐
    │  One Playwright Instance + One Browser   │
    │                                         │
    │  ┌─────────┐ ┌─────────┐ ┌─────────┐  │
    │  │Context 1│ │Context 2│ │Context 3│  │
    │  │ Page A  │ │ Page B  │ │ Page C  │  │
    │  │(isolated)│ │(isolated)│ │(isolated)│  │
    │  └─────────┘ └─────────┘ └─────────┘  │
    └─────────────────────────────────────────┘
    """
    requested_concurrency = max_parallel or MAX_PARALLEL_PAGES
    total = len(posts)

    if total <= 1:
        # Single post — use the sequential path (simpler, less overhead)
        return await _create_facebook_posts_unstaged(cookies_json, posts, progress_callback)
    if not POST_ALLOW_PARALLEL_SAME_COOKIE:
        logger.info(
            'PARALLEL_BATCH stage="disabled" reason="same_cookie_session_safety"; '
            'set POST_ALLOW_PARALLEL_SAME_COOKIE=true to opt in'
        )
        return await _create_facebook_posts_unstaged(cookies_json, posts, progress_callback)
    concurrency = max(
        1,
        min(
            total,
            requested_concurrency,
            POST_PARALLEL_SAME_COOKIE_MAX_CONTEXTS,
        ),
    )
    if concurrency <= 1:
        logger.info(
            'PARALLEL_BATCH stage="disabled" reason="same_cookie_parallel_cap_is_one"; '
            'using optimized sequential batch'
        )
        return await _create_facebook_posts_unstaged(cookies_json, posts, progress_callback)
    if concurrency < requested_concurrency:
        logger.info(
            f'PARALLEL_BATCH stage="capped" requested={requested_concurrency} '
            f'using={concurrency} same_cookie_max={POST_PARALLEL_SAME_COOKIE_MAX_CONTEXTS}'
        )

    logger.info(f'PARALLEL_BATCH stage="init" total_pages={total} max_parallel={concurrency}')

    session_guard = None
    session_success = False
    session_detail = ''
    try:
        session_guard = await _acquire_cookie_session_guard(cookies_json, 'parallel batch post')
    except Exception as exc:
        logger.error(f'Could not acquire cookie session lock for parallel posting: {exc}')
        return [
            {
                'page': str(post.get('page_name') or post.get('page_id_or_url') or 'Unknown page'),
                'success': False,
                'result': str(exc),
            }
            for post in posts
        ]

    # --- Step 1: Launch one browser, validate login in a shared check ---
    try:
        playwright, browser, check_context, check_page = await launch_browser_session(cookies_json)
    except Exception as exc:
        logger.error(f'Could not launch browser for parallel posting: {exc}')
        await asyncio.to_thread(_mark_cookie_session_used, cookies_json, False, str(exc))
        await _release_cookie_session_guard(session_guard)
        return [
            {
                'page': str(post.get('page_name') or post.get('page_id_or_url') or 'Unknown page'),
                'success': False,
                'result': str(exc),
            }
            for post in posts
        ]

    try:
        # Validate login once (avoid N login checks)
        await _enable_fast_posting_mode(check_context, check_page)
        await check_page.goto("https://www.facebook.com/", wait_until='domcontentloaded', timeout=45000)
        await _resume_facebook_cookie_session(check_page)
        try:
            await check_page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

        security_detail = await _facebook_security_block_detail(check_page)
        if security_detail:
            diagnostic_path = await _save_diagnostics(check_page, 'parallel_batch_security')
            detail = _with_diagnostic(security_detail, diagnostic_path)
            session_detail = detail
            return [
                {
                    'page': str(post.get('page_name') or post.get('page_id_or_url') or 'Unknown page'),
                    'success': False,
                    'result': detail,
                }
                for post in posts
            ]

        if await _page_looks_logged_out(check_page):
            diagnostic_path = await _save_diagnostics(check_page, 'parallel_batch_logged_out')
            detail = _with_diagnostic("Cookies expired or invalid. Login page detected.", diagnostic_path)
            session_detail = detail
            return [
                {
                    'page': str(post.get('page_name') or post.get('page_id_or_url') or 'Unknown page'),
                    'success': False,
                    'result': detail,
                }
                for post in posts
            ]

        logger.info('PARALLEL_BATCH stage="login_validated" — spawning parallel workers')

        # Close the check context (we'll create isolated ones for each worker)
        await check_context.close()

        # --- Step 2: Define the isolated worker for each page ---
        semaphore = asyncio.Semaphore(concurrency)
        security_abort = asyncio.Event()  # If any worker hits security, abort all
        results_lock = asyncio.Lock()
        progress_lock = asyncio.Lock()
        results: List[Optional[Dict[str, Any]]] = [None] * total

        async def _emit_progress(event: Dict[str, Any]) -> None:
            if progress_callback is None:
                return
            async with progress_lock:
                await progress_callback(event)

        async def _post_to_page_worker(idx: int, post: Dict[str, Any]) -> None:
            page_label = str(post.get('page_name') or post.get('page_id_or_url') or 'Unknown page')
            current_result: Dict[str, Any] = {'page': page_label, 'success': False, 'result': ''}

            # Check if we should abort
            if security_abort.is_set():
                async with results_lock:
                    current_result = {
                        'page': page_label,
                        'success': False,
                        'result': 'Skipped: security checkpoint detected on another page.',
                    }
                    results[idx] = current_result
                return

            async with semaphore:
                if security_abort.is_set():
                    async with results_lock:
                        current_result = {
                            'page': page_label,
                            'success': False,
                            'result': 'Skipped: security checkpoint detected on another page.',
                        }
                        results[idx] = current_result
                    return

                worker_started = time.time()
                logger.info(
                    f'PARALLEL_BATCH stage="worker_start" index={idx + 1}/{total} '
                    f'page="{page_label}" type={post.get("post_type", "post")}'
                )

                if progress_callback is not None:
                    await _emit_progress({
                        'page': page_label,
                        'stage': 'Starting page (parallel)',
                        'index': idx,
                        'total': total,
                        'done': sum(1 for r in results if r is not None),
                    })
                stagger_seconds = (
                    (idx % concurrency) * POST_PARALLEL_SAME_COOKIE_STAGGER_SECONDS
                    if concurrency > 1
                    else 0
                )
                if stagger_seconds > 0:
                    logger.info(
                        f'PARALLEL_BATCH stage="worker_stagger" index={idx + 1}/{total} '
                        f'page="{page_label}" delay={stagger_seconds:.1f}s'
                    )
                    if progress_callback is not None:
                        await _emit_progress({
                            'page': page_label,
                            'stage': f'Start staggering {stagger_seconds:.0f}s',
                            'index': idx,
                            'total': total,
                            'done': sum(1 for r in results if r is not None),
                        })
                    await asyncio.sleep(stagger_seconds)

                # Create an ISOLATED browser context for this page
                worker_context = None
                worker_page = None
                try:
                    worker_context = await _new_facebook_context(browser)

                    # Inject cookies into this isolated context
                    cookies = json.loads(cookies_json)
                    cookies = normalize_facebook_cookies(cookies)
                    await worker_context.add_cookies(cast(Any, cookies))

                    active_worker_page: Page = await worker_context.new_page()
                    worker_page = active_worker_page
                    if FACEBOOK_STEALTH_ASYNC_ENABLED:
                        await stealth_async(active_worker_page)
                    await _enable_fast_posting_mode(worker_context, active_worker_page)

                    # Post to this page using FAST direct-URL approach (skips Pages Portal)
                    success, result = await _create_facebook_post_direct(
                        active_worker_page,
                        str(post.get('page_id_or_url') or ''),
                        str(post.get('post_type') or 'post'),
                        str(post.get('caption') or ''),
                        post.get('media_url') or None,
                        page_label,
                        _emit_progress,
                    )

                    async with results_lock:
                        current_result = {'page': page_label, 'success': success, 'result': result}
                        results[idx] = current_result

                    if not success and _is_facebook_security_failure(str(result)):
                        logger.warning(f'PARALLEL_BATCH security_abort triggered by page="{page_label}"')
                        security_abort.set()

                except Exception as exc:
                    if worker_page:
                        diagnostic_path = await _save_diagnostics(worker_page, f'parallel_worker_{idx}_exception')
                    else:
                        diagnostic_path = ''
                    async with results_lock:
                        current_result = {
                            'page': page_label,
                            'success': False,
                            'result': _with_diagnostic(str(exc), diagnostic_path) if diagnostic_path else str(exc),
                        }
                        results[idx] = current_result
                finally:
                    if worker_context:
                        try:
                            await worker_context.close()
                        except Exception:
                            pass

                elapsed = time.time() - worker_started
                logger.info(
                    f'PARALLEL_BATCH stage="worker_done" index={idx + 1}/{total} '
                    f'page="{page_label}" success={current_result.get("success", False)} '
                    f'elapsed={elapsed:.1f}s'
                )

                if progress_callback is not None and current_result:
                    await _emit_progress({
                        **current_result,
                        'stage': 'Done' if current_result.get('success') else 'Failed',
                        'post_type': str(post.get('post_type') or 'post'),
                        'index': idx,
                        'total': total,
                        'done': sum(1 for r in results if r is not None),
                        'completed': True,
                    })

        # --- Step 3: Launch all workers in parallel ---
        tasks = [
            asyncio.create_task(_post_to_page_worker(idx, post))
            for idx, post in enumerate(posts)
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

        # Fill any None results (shouldn't happen, but safety net)
        final_results = []
        for idx, r in enumerate(results):
            if r is None:
                page_label = str(posts[idx].get('page_name') or posts[idx].get('page_id_or_url') or 'Unknown page')
                final_results.append({
                    'page': page_label,
                    'success': False,
                    'result': 'Worker did not complete.',
                })
            else:
                final_results.append(r)

        success_count = sum(1 for r in final_results if r.get('success'))
        logger.info(
            f'PARALLEL_BATCH stage="finished" success_count={success_count}/{total} '
            f'mode=parallel max_parallel={concurrency}'
        )
        session_success = all(bool(item.get('success')) for item in final_results)
        session_detail = '' if session_success else _session_result_detail(final_results)
        return final_results
    finally:
        await browser.close()
        await playwright.stop()
        await asyncio.to_thread(_mark_cookie_session_used, cookies_json, session_success, session_detail)
        await _release_cookie_session_guard(session_guard)


async def _create_facebook_posts_parallel_browser(
    cookies_json: str,
    posts: List[Dict[str, Any]],
    progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    max_parallel: Optional[int] = None,
) -> List[Dict[str, Any]]:
    staged_paths: List[str] = []
    try:
        staged_posts, staged_paths = await _stage_batch_media_sources(posts)
        return await _create_facebook_posts_parallel_unstaged(
            cookies_json,
            staged_posts,
            progress_callback,
            max_parallel,
        )
    finally:
        await asyncio.to_thread(_cleanup_staged_batch_media, staged_paths)


async def create_facebook_posts_parallel(
    cookies_json: str,
    posts: List[Dict[str, Any]],
    progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    max_parallel: Optional[int] = None,
) -> List[Dict[str, Any]]:
    fast_results = await _try_create_facebook_posts_fast_http(
        cookies_json,
        posts,
        progress_callback,
        lambda fallback_posts: _create_facebook_posts_parallel_browser(
            cookies_json,
            fallback_posts,
            progress_callback,
            max_parallel,
        ),
    )
    if fast_results is not None:
        return fast_results
    return await _create_facebook_posts_parallel_browser(cookies_json, posts, progress_callback, max_parallel)


def create_facebook_posts_sync(
    cookies_json: str,
    posts: List[Dict[str, Any]],
    progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
) -> List[Dict[str, Any]]:
    return asyncio.run(create_facebook_posts(cookies_json, posts, progress_callback))


def _create_facebook_posts_unstaged_sync(
    cookies_json: str,
    posts: List[Dict[str, Any]],
    progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
) -> List[Dict[str, Any]]:
    return asyncio.run(_create_facebook_posts_unstaged(cookies_json, posts, progress_callback))


def create_facebook_posts_parallel_sync(
    cookies_json: str,
    posts: List[Dict[str, Any]],
    progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    max_parallel: Optional[int] = None,
) -> List[Dict[str, Any]]:
    return asyncio.run(create_facebook_posts_parallel(cookies_json, posts, progress_callback, max_parallel))


def _create_facebook_posts_parallel_unstaged_sync(
    cookies_json: str,
    posts: List[Dict[str, Any]],
    progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    max_parallel: Optional[int] = None,
) -> List[Dict[str, Any]]:
    return asyncio.run(_create_facebook_posts_parallel_unstaged(cookies_json, posts, progress_callback, max_parallel))


def _batch_has_mixed_media_modes(posts: List[Dict[str, Any]]) -> bool:
    post_types = {str(post.get('post_type') or 'post').strip().lower() for post in posts}
    media_flags = {bool(str(post.get('media_url') or '').strip()) for post in posts}
    return len(post_types) > 1 or len(media_flags) > 1


def _create_batch_posts_sync_browser_adaptive(
    cookies_json: str,
    posts: List[Dict[str, Any]],
    progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
) -> List[Dict[str, Any]]:
    staged_paths: List[str] = []
    try:
        staged_posts, staged_paths = _stage_batch_media_sources_sync(posts)
        timeout_result = _batch_operation_slot_timeout_results(staged_posts)
        if (
            POST_PARALLEL_BATCH_ENABLED
            and len(staged_posts) > 1
            and not _batch_has_mixed_media_modes(staged_posts)
        ):
            logger.info(
                f'Batch sync adaptive mode: parallel_enabled=true pages={len(staged_posts)} '
                f'allow_same_cookie={POST_ALLOW_PARALLEL_SAME_COOKIE}'
            )
            return _run_with_post_operation_slot(
                lambda: _create_facebook_posts_parallel_unstaged_sync(
                    cookies_json,
                    staged_posts,
                    progress_callback,
                    max_parallel=MAX_PARALLEL_PAGES,
                ),
                timeout_result,
            )
        if len(staged_posts) > 1:
            logger.info(
                f'Batch sync adaptive mode: sequential pages={len(staged_posts)} '
                f'parallel_enabled={POST_PARALLEL_BATCH_ENABLED} mixed_media={_batch_has_mixed_media_modes(staged_posts)}'
            )
        return _run_with_post_operation_slot(
            lambda: _create_facebook_posts_unstaged_sync(cookies_json, staged_posts, progress_callback),
            timeout_result,
        )
    finally:
        _cleanup_staged_batch_media(staged_paths)


def _try_create_facebook_posts_fast_http_sync(
    cookies_json: str,
    posts: List[Dict[str, Any]],
    progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]],
    browser_fallback: Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]],
) -> Optional[List[Dict[str, Any]]]:
    try:
        from fast_graphql_poster import create_facebook_posts_fast_sync, is_fast_graphql_enabled
    except Exception as exc:
        logger.debug(f'Fast HTTP GraphQL sync tier unavailable: {exc}')
        return None
    if not is_fast_graphql_enabled():
        return None
    try:
        return create_facebook_posts_fast_sync(
            cookies_json,
            posts,
            progress_callback,
            launch_browser_session=launch_browser_session,
            browser_fallback=browser_fallback,
        )
    except Exception as exc:
        logger.warning(f'Fast HTTP GraphQL sync tier failed before a safe result was produced; using browser path: {exc}')
        return None


def _create_batch_posts_sync_adaptive(
    cookies_json: str,
    posts: List[Dict[str, Any]],
    progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
) -> List[Dict[str, Any]]:
    fast_results = _try_create_facebook_posts_fast_http_sync(
        cookies_json,
        posts,
        progress_callback,
        lambda fallback_posts: _create_batch_posts_sync_browser_adaptive(
            cookies_json,
            fallback_posts,
            progress_callback,
        ),
    )
    if fast_results is not None:
        return fast_results
    return _create_batch_posts_sync_browser_adaptive(cookies_json, posts, progress_callback)


def _batch_operation_slot_timeout_results(posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            'page': str(post.get('page_name') or post.get('page_id_or_url') or 'Unknown page'),
            'success': False,
            'result': (
                'Timed out waiting for an available posting operation slot. '
                'Try again after the current posting jobs complete.'
            ),
        }
        for post in posts
    ]


def _rq_progress_callback_factory() -> Optional[_ProgressCoroutineCallback]:
    if get_current_job is None:
        return None
    try:
        job = get_current_job()
    except Exception:
        job = None
    if job is None:
        return None
    last_saved = {'at': 0.0}

    async def callback(event: Dict[str, Any]) -> None:
        try:
            now = time.time()
            completed = bool(event.get('completed', False))
            has_result = 'success' in event or bool(event.get('result'))
            if (
                not completed
                and not has_result
                and POST_RQ_PROGRESS_MIN_INTERVAL_SECONDS > 0
                and now - float(last_saved.get('at') or 0) < POST_RQ_PROGRESS_MIN_INTERVAL_SECONDS
            ):
                return
            progress = dict(job.meta.get('progress') or {})
            page = str(event.get('page') or progress.get('page') or '')
            stage = str(event.get('stage') or progress.get('stage') or 'Working')
            progress.update({
                'page': page,
                'stage': stage,
                'detail': str(event.get('detail') or ''),
                'done': int(event.get('done', progress.get('done', 0)) or 0),
                'total': int(event.get('total', progress.get('total', 0)) or 0),
                'index': int(event.get('index', progress.get('index', 0)) or 0),
                'post_type': str(event.get('post_type') or progress.get('post_type') or ''),
                'completed': completed,
                'updated_at': now,
            })
            if 'success' in event:
                progress['success'] = bool(event.get('success'))
            if event.get('result'):
                progress['result'] = str(event.get('result'))
            if completed and page:
                completed_events = list(job.meta.get('completed_events') or [])
                event_index = int(event.get('index', progress.get('index', 0)) or 0)
                event_post_type = str(event.get('post_type') or progress.get('post_type') or '')
                if not any(
                    str(item.get('page') or '') == page
                    and int(item.get('index', -1) or -1) == event_index
                    and str(item.get('post_type') or '') == event_post_type
                    for item in completed_events
                    if isinstance(item, dict)
                ):
                    completed_events.append({
                        'page': page,
                        'post_type': event_post_type,
                        'index': event_index,
                        'success': bool(event.get('success')),
                        'result': str(event.get('result') or stage),
                        'updated_at': now,
                    })
                job.meta['completed_events'] = completed_events[-100:]
            job.meta['progress'] = progress
            job.save_meta()
            last_saved['at'] = now
        except Exception as exc:
            logger.debug(f'Could not update RQ posting progress metadata: {exc}')

    return callback


def _emit_rq_progress_sync(
    progress_callback: Optional[_ProgressCoroutineCallback],
    event: Dict[str, Any],
) -> None:
    if progress_callback is None:
        return
    try:
        asyncio.run(progress_callback(event))
    except Exception as exc:
        logger.debug(f'Could not emit RQ posting progress metadata: {exc}')


def create_facebook_posts_with_account_lock_sync(
    cookies_json: str,
    posts: List[Dict[str, Any]],
    account_lock_key: str,
) -> List[Dict[str, Any]]:
    """Serialize a multi-page batch for a single Facebook account."""
    logger.info(f"🔑 Initializing batch account lock check for key: {account_lock_key}")
    progress_callback = _rq_progress_callback_factory()
    total_posts = len(posts)

    def lock_wait_result(detail: str) -> List[Dict[str, Any]]:
        return [
            {
                'page': str(post.get('page_name') or post.get('page_id_or_url') or 'Unknown page'),
                'success': False,
                'result': detail,
            }
            for post in posts
        ]

    client = _redis_client()
    if client is None:
        logger.info("ℹ️ Redis not configured or unavailable for batch. Attempting PostgreSQL advisory lock...")
        _emit_rq_progress_sync(progress_callback, {
            'page': 'Account lock',
            'stage': 'Waiting for account lock',
            'detail': f'PostgreSQL lock wait limit {POST_BATCH_ACCOUNT_LOCK_WAIT_SECONDS}s',
            'done': 0,
            'total': total_posts,
        })
        pg_conn = _acquire_postgres_account_lock(account_lock_key, POST_BATCH_ACCOUNT_LOCK_WAIT_SECONDS)
        if pg_conn is not None:
            try:
                return _create_batch_posts_sync_adaptive(cookies_json, posts, progress_callback)
            finally:
                _release_postgres_account_lock(pg_conn, account_lock_key)
        return _create_batch_posts_sync_adaptive(cookies_json, posts, progress_callback)

    lock_name = _post_account_lock_name(account_lock_key)
    lock_ttl = _batch_account_lock_ttl_seconds(posts)
    lock = client.lock(
        lock_name,
        timeout=lock_ttl,
        blocking_timeout=max(1, POST_BATCH_ACCOUNT_LOCK_WAIT_SECONDS),
        sleep=2,
    )
    acquired = False
    lock_heartbeat: Optional[Tuple[threading.Event, str]] = None
    try:
        logger.info(f"ℹ️ Redis is configured. Attempting to acquire batch lock: '{lock_name}'")
        _emit_rq_progress_sync(progress_callback, {
            'page': 'Account lock',
            'stage': 'Waiting for account lock',
            'detail': f'Checking account lock; wait limit {POST_BATCH_ACCOUNT_LOCK_WAIT_SECONDS}s',
            'done': 0,
            'total': total_posts,
        })
        deadline = time.monotonic() + max(1, POST_BATCH_ACCOUNT_LOCK_WAIT_SECONDS)
        last_progress_at = 0.0
        while time.monotonic() < deadline:
            acquired = bool(lock.acquire(blocking=False))
            if acquired:
                break
            now = time.monotonic()
            if now - last_progress_at >= 5:
                ttl_seconds = _redis_lock_ttl_seconds(client, lock_name)
                ttl_detail = f' lock TTL remaining ~{ttl_seconds:.0f}s.' if ttl_seconds else ''
                waited_seconds = int(max(0, POST_BATCH_ACCOUNT_LOCK_WAIT_SECONDS - (deadline - now)))
                detail = (
                    f'Another posting job is using this Facebook account; '
                    f'waited {waited_seconds}/{POST_BATCH_ACCOUNT_LOCK_WAIT_SECONDS}s.{ttl_detail}'
                )
                logger.info(f'Waiting for Redis batch account lock: {detail}')
                _emit_rq_progress_sync(progress_callback, {
                    'page': 'Account lock',
                    'stage': 'Waiting for account lock',
                    'detail': detail,
                    'done': 0,
                    'total': total_posts,
                })
                last_progress_at = now
            time.sleep(min(2.0, max(0.1, deadline - time.monotonic())))
        if not acquired:
            if _break_stale_redis_lock_if_safe(client, lock_name, 'batch account lock wait timed out'):
                lock = client.lock(
                    lock_name,
                    timeout=lock_ttl,
                    blocking_timeout=0,
                    sleep=0.1,
                )
                acquired = bool(lock.acquire(blocking=False))
        if not acquired:
            ttl_seconds = _redis_lock_ttl_seconds(client, lock_name)
            ttl_detail = f' Redis reports ~{ttl_seconds:.0f}s remaining on the account lock.' if ttl_seconds else ''
            result = (
                f'Timed out after {POST_BATCH_ACCOUNT_LOCK_WAIT_SECONDS}s waiting for this Facebook account '
                f'to finish another posting job.{ttl_detail} Try again after the current/stale lock clears.'
            )
            _emit_rq_progress_sync(progress_callback, {
                'page': 'Account lock',
                'stage': 'Failed',
                'completed': True,
                'success': False,
                'result': result,
                'done': 0,
                'total': total_posts,
            })
            return lock_wait_result(result)
        logger.info(f'✅ Redis batch account publish lock acquired: {lock_name}. Proceeding with {len(posts)} post(s)...')
        lock_heartbeat = _start_redis_lock_heartbeat(
            client,
            lock_name,
            'batch_account',
            f'posts={len(posts)} account_lock_key={account_lock_key[:80]}',
            lock_ttl,
        )
        _emit_rq_progress_sync(progress_callback, {
            'page': 'Account lock',
            'stage': 'Account lock acquired',
            'detail': f'Proceeding with {total_posts} post(s)',
            'done': 0,
            'total': total_posts,
        })
        return _create_batch_posts_sync_adaptive(cookies_json, posts, progress_callback)
    finally:
        if acquired:
            try:
                _release_redis_lock_with_metadata_sync(lock, lock_heartbeat, client)
                logger.info(f'🔓 Redis batch account publish lock released: {lock_name}')
            except Exception as exc:
                logger.warning(f'Failed to release Redis batch account publish lock: {exc}')


def create_facebook_post_sync(
    cookies_json: str,
    page_id_or_url: str,
    post_type: str,
    caption: str,
    media_url: Optional[str] = None,
    page_name: Optional[str] = None,
    progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
) -> Tuple[bool, str]:
    return asyncio.run(
        create_facebook_post(
            cookies_json,
            page_id_or_url,
            post_type,
            caption,
            media_url,
            page_name,
            progress_callback,
        )
    )


def create_facebook_post_with_account_lock_sync(
    cookies_json: str,
    page_id_or_url: str,
    post_type: str,
    caption: str,
    media_url: Optional[str],
    account_lock_key: str,
    page_name: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Serialize browser publishing per Facebook account.

    Multiple workers may publish for different accounts at the same time, but a
    single cookie session must not be driven by two browsers concurrently.
    """
    logger.info(f"🔑 Initializing account lock check for key: {account_lock_key}")
    progress_callback = _rq_progress_callback_factory()
    progress_page = page_name or page_id_or_url or 'Unknown page'
    operation_slot_timeout_result = (
        False,
        'Timed out waiting for an available posting operation slot. '
        'Try again after the current posting jobs complete.',
    )

    def run_post_with_slot() -> Tuple[bool, str]:
        _emit_rq_progress_sync(progress_callback, {'page': progress_page, 'stage': 'Waiting for operation slot'})
        success, result = _run_with_post_operation_slot(
            lambda: create_facebook_post_sync(
                cookies_json,
                page_id_or_url,
                post_type,
                caption,
                media_url,
                page_name,
                progress_callback,
            ),
            operation_slot_timeout_result,
        )
        _emit_rq_progress_sync(progress_callback, {
            'page': progress_page,
            'stage': 'Done' if success else 'Failed',
            'completed': True,
            'success': success,
            'result': result,
        })
        return success, result

    client = _redis_client()
    if client is None:
        logger.info("ℹ️ Redis not configured or unavailable. Attempting PostgreSQL advisory lock...")
        pg_conn = _acquire_postgres_account_lock(account_lock_key)
        if pg_conn is not None:
            logger.info("✅ PostgreSQL advisory lock acquired successfully. Proceeding with post...")
            try:
                return run_post_with_slot()
            finally:
                logger.info("🔓 Releasing PostgreSQL advisory lock...")
                _release_postgres_account_lock(pg_conn, account_lock_key)

        logger.warning(
            '⚠️ No Redis or PostgreSQL lock available; posting directly without distributed account lock. '
            'This is safe for local single-worker and test environments.'
        )
        return run_post_with_slot()

    lock_name = _post_account_lock_name(account_lock_key)
    logger.info(f"ℹ️ Redis is configured. Attempting to acquire lock: '{lock_name}'")
    lock_ttl = max(
        60,
        POST_ACCOUNT_LOCK_TTL_SECONDS,
        POST_COOKIE_SESSION_LOCK_TTL_SECONDS,
        FACEBOOK_POST_TIMEOUT_SECONDS + 300,
    )
    lock = client.lock(
        lock_name,
        timeout=lock_ttl,
        blocking_timeout=max(1, POST_ACCOUNT_LOCK_WAIT_SECONDS),
        sleep=2,
    )
    acquired = False
    lock_heartbeat: Optional[Tuple[threading.Event, str]] = None
    try:
        acquired = lock.acquire(blocking=True)
        if not acquired:
            if _break_stale_redis_lock_if_safe(client, lock_name, 'single account lock wait timed out'):
                lock = client.lock(
                    lock_name,
                    timeout=lock_ttl,
                    blocking_timeout=0,
                    sleep=0.1,
                )
                acquired = bool(lock.acquire(blocking=False))
        if not acquired:
            logger.warning(f"❌ Redis lock acquisition timed out for '{lock_name}'")
            _emit_rq_progress_sync(progress_callback, {
                'page': progress_page,
                'stage': 'Failed',
                'completed': True,
                'success': False,
                'result': 'Timed out waiting for this Facebook account to finish another posting job.',
            })
            return (
                False,
                'Timed out waiting for this Facebook account to finish another posting job. '
                'Try again after the current job completes.',
            )
        logger.info(f'✅ Redis account publish lock acquired: {lock_name}. Proceeding with post...')
        lock_heartbeat = _start_redis_lock_heartbeat(
            client,
            lock_name,
            'single_account',
            f'page={progress_page} account_lock_key={account_lock_key[:80]}',
            lock_ttl,
        )
        return run_post_with_slot()
    except Exception as exc:
        logger.warning(
            f'⚠️ Redis account publish lock failed (connection refused/error: {exc}). '
            'Falling back to PostgreSQL advisory lock or direct execution...'
        )
        pg_conn = _acquire_postgres_account_lock(account_lock_key)
        if pg_conn is not None:
            logger.info("✅ PostgreSQL advisory lock acquired successfully during Redis fallback. Proceeding with post...")
            try:
                return run_post_with_slot()
            finally:
                logger.info("🔓 Releasing PostgreSQL advisory lock...")
                _release_postgres_account_lock(pg_conn, account_lock_key)

        logger.warning(
            '⚠️ No PostgreSQL lock available during Redis fallback; posting directly without distributed account lock. '
            'This is safe for local single-worker and test environments.'
        )
        return run_post_with_slot()
    finally:
        if acquired:
            try:
                _release_redis_lock_with_metadata_sync(lock, lock_heartbeat, client)
                logger.info(f'🔓 Redis account publish lock released: {lock_name}')
            except Exception as exc:
                logger.warning(f'Failed to release Redis account publish lock: {exc}')
