"""Browser fallback skeletons for token extraction and video posting."""

from __future__ import annotations

import inspect
import json
import time
import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from .models import IdentityContext, PostResult
from .tokens import TokenVault
from .browser_stealth import BrowserStealth, StealthConfig
from .utils import cookies_json_to_header, maybe_await, stable_hash

logger = logging.getLogger(__name__)

ProgressCallback = Optional[Callable[[Dict[str, Any]], Awaitable[None]]]
LegacyCreatePosts = Callable[[str, List[Dict[str, Any]], ProgressCallback], Awaitable[List[Dict[str, Any]]]]


class BrowserTokenExtractor:
    def __init__(self, token_vault: TokenVault, identity: IdentityContext):
        self.token_vault = token_vault
        self.identity = identity

    async def extract_tokens(self, cookies_json: str) -> Dict[str, Any]:
        owner = stable_hash(self.identity.account_id, time.time(), length=32)
        lock_acquired = await self.acquire_extraction_lock(owner)
        if not lock_acquired:
            raise RuntimeError('token extraction already running for this account')
        try:
            return await self._extract_tokens_unlocked(cookies_json)
        finally:
            await self.release_extraction_lock(owner)

    async def extract_with_retry(
        self,
        cookies_json: str,
        max_wait_seconds: int = 60,
        poll_interval_seconds: float = 1.0,
    ) -> Optional[Dict[str, Any]]:
        owner = stable_hash(self.identity.account_id, time.time(), length=32)
        lock_acquired = await self.acquire_extraction_lock(owner)
        if not lock_acquired:
            deadline = time.time() + max(0, max_wait_seconds)
            while time.time() < deadline:
                tokens = await self.token_vault.get(self.identity.account_id)
                if tokens:
                    return tokens
                await asyncio.sleep(max(0.05, poll_interval_seconds))
            return None
        try:
            return await self._extract_tokens_unlocked(cookies_json)
        finally:
            await self.release_extraction_lock(owner)

    async def _extract_tokens_unlocked(self, cookies_json: str) -> Dict[str, Any]:
        try:
            from playwright.async_api import Error as PlaywrightError
            from playwright.async_api import async_playwright
        except ImportError as exc:
            logger.error('Playwright not installed: %s', exc)
            raise RuntimeError('playwright is required for token extraction') from exc

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                    '--disable-gpu',
                ],
            )
            context_args = self.identity.to_browser_args()
            viewport = context_args.get('viewport') or (1280, 720)
            if isinstance(viewport, dict):
                viewport_arg = viewport
            elif isinstance(viewport, (list, tuple)) and len(viewport) >= 2:
                viewport_arg = {'width': viewport[0], 'height': viewport[1]}
            else:
                viewport_arg = {'width': 1280, 'height': 720}

            context = await browser.new_context(
                viewport=viewport_arg,
                user_agent=self.identity.user_agent,
                geolocation=self.identity.geolocation if isinstance(self.identity.geolocation, dict) else None,
                timezone_id=self.identity.timezone or 'UTC',
                locale=self.identity.locale or 'en-US',
            )

            # Apply anti-detection stealth patches
            stealth = BrowserStealth(StealthConfig(
                webgl_vendor=self.identity.webgl_vendor,
                webgl_renderer=self.identity.webgl_renderer,
            ))
            await stealth.apply_to_context(context)

            try:
                cookies = json.loads(cookies_json)
                if isinstance(cookies, list):
                    await context.add_cookies(cookies)
                elif isinstance(cookies, dict) and 'cookies' in cookies:
                    await context.add_cookies(cookies['cookies'])
            except Exception as exc:
                logger.warning('Failed to load cookies: %s', exc)

            page = await context.new_page()

            try:
                await page.goto('https://www.facebook.com/me', wait_until='domcontentloaded', timeout=15000)
                tokens = await page.evaluate(
                    """() => {
                        const get = (name) => { try { return require(name); } catch (_) { return {}; } };
                        const cookie = document.cookie || '';
                        const xs = (cookie.match(/(?:^|; )xs=([^;]+)/) || [])[1] || '';
                        return {
                            fb_dtsg: get('DTSGInitialData').token || get('DTSGInitData').token || document.querySelector('input[name="fb_dtsg"]')?.value || '',
                            lsd: get('LSD').token || document.querySelector('input[name="lsd"]')?.value || '',
                            user_id: get('CurrentUserInitialData').USER_ID || '',
                            xs,
                            revision: String(get('SiteData').client_revision || ''),
                            timestamp: Date.now() / 1000
                        };
                    }"""
                )
                if not tokens.get('fb_dtsg') or not tokens.get('lsd'):
                    raise RuntimeError('required Facebook tokens were not found')
                try:
                    tokens['cookie_header'] = cookies_json_to_header(cookies_json)
                except Exception as exc:
                    logger.warning('Failed to derive cookie header for cached tokens: %s', exc)
                await self.token_vault.set(self.identity.account_id, tokens)
                return tokens
            finally:
                await context.close()
                await browser.close()

    async def acquire_extraction_lock(self, owner: str, ttl_seconds: int = 300) -> bool:
        redis = self.token_vault.redis
        if redis is None:
            return True
        key = f'token_extract_lock:{self.identity.account_id}'
        try:
            return bool(await maybe_await(redis.set(key, owner, nx=True, ex=ttl_seconds)))
        except TypeError:
            if await maybe_await(redis.exists(key)):
                return False
            await maybe_await(redis.setex(key, ttl_seconds, owner))
            return True

    async def release_extraction_lock(self, owner: str) -> None:
        redis = self.token_vault.redis
        if redis is None:
            return
        key = f'token_extract_lock:{self.identity.account_id}'
        current = await maybe_await(redis.get(key))
        if isinstance(current, bytes):
            current = current.decode('utf-8', errors='ignore')
        if str(current or '') == str(owner):
            await maybe_await(redis.delete(key))


async def _default_legacy_create_posts(
    cookies_json: str,
    posts: List[Dict[str, Any]],
    progress_callback: ProgressCallback = None,
) -> List[Dict[str, Any]]:
    from playwright_engine import create_facebook_posts

    return await create_facebook_posts(cookies_json, posts, progress_callback)


class BrowserVideoUploader:
    def __init__(self, legacy_create_posts: LegacyCreatePosts = _default_legacy_create_posts):
        self.legacy_create_posts = legacy_create_posts

    async def upload_video_post(
        self,
        video_path: str,
        cookies_json: str,
        identity: IdentityContext,
        page_id_or_url: str = '',
        caption: str = '',
        page_name: str = '',
        progress_callback: ProgressCallback = None,
    ) -> PostResult:
        started = time.monotonic()
        page_id = str(page_id_or_url or '').strip()
        video = Path(video_path)

        if not video.exists():
            return self._result(False, 'BROWSER_VIDEO_FILE_MISSING', page_id, 'video file not found', started)
        if not cookies_json:
            return self._result(
                False,
                'BROWSER_VIDEO_COOKIES_MISSING',
                page_id,
                'cookies_json is required for browser video fallback',
                started,
            )
        if not page_id:
            return self._result(
                False,
                'BROWSER_VIDEO_PAGE_MISSING',
                page_id,
                'page_id_or_url is required for browser video fallback',
                started,
            )

        post = {
            'page_id_or_url': page_id,
            'page_name': page_name or page_id,
            'post_type': 'video',
            'caption': caption,
            'media_url': str(video),
        }
        await self._emit(progress_callback, {
            'stage': 'browser_video_fallback_started',
            'account_id': identity.account_id,
            'page_id': page_id,
            'media_path': str(video),
        })
        try:
            result = self.legacy_create_posts(cookies_json, [post], progress_callback)
            rows = await result if inspect.isawaitable(result) else result
        except Exception as exc:
            error = f'legacy browser video fallback raised: {str(exc)[:200]}'
            await self._emit(progress_callback, {
                'stage': 'browser_video_fallback_failed',
                'account_id': identity.account_id,
                'page_id': page_id,
                'error': error,
            })
            return self._result(False, 'BROWSER_VIDEO_FALLBACK_ERROR', page_id, error, started)

        normalized = self._normalize_legacy_result(rows, page_id, started)
        await self._emit(progress_callback, {
            'stage': 'browser_video_fallback_completed',
            'account_id': identity.account_id,
            'page_id': page_id,
            'success': normalized.success,
            'status': normalized.status,
            'result': normalized.post_id or normalized.error_message or '',
        })
        return normalized

    async def upload_video(
        self,
        video_path: str,
        cookies_json: str,
        identity: IdentityContext,
        page_id_or_url: str = '',
        caption: str = '',
        page_name: str = '',
        progress_callback: ProgressCallback = None,
    ) -> Tuple[bool, Optional[str]]:
        result = await self.upload_video_post(
            video_path,
            cookies_json,
            identity,
            page_id_or_url=page_id_or_url,
            caption=caption,
            page_name=page_name,
            progress_callback=progress_callback,
        )
        return result.success, result.post_id or result.error_message or result.status

    @staticmethod
    def _normalize_legacy_result(rows: Any, page_id: str, started: float) -> PostResult:
        if not rows:
            return BrowserVideoUploader._result(
                False,
                'BROWSER_VIDEO_FALLBACK_EMPTY',
                page_id,
                'legacy browser video fallback returned no result',
                started,
            )
        first = rows[0] if isinstance(rows, list) else rows
        if not isinstance(first, dict):
            return BrowserVideoUploader._result(
                False,
                'BROWSER_VIDEO_FALLBACK_INVALID',
                page_id,
                'legacy browser video fallback returned an invalid result',
                started,
            )
        success = bool(first.get('success'))
        detail = str(first.get('result') or first.get('status') or '').strip()
        post_id = str(first.get('post_id') or first.get('id') or '').strip()
        if success:
            return BrowserVideoUploader._result(
                True,
                'BROWSER_VIDEO_FALLBACK_SUCCESS',
                page_id,
                None,
                started,
                post_id=post_id or detail or None,
            )
        return BrowserVideoUploader._result(
            False,
            'BROWSER_VIDEO_FALLBACK_FAILED',
            page_id,
            detail or 'legacy browser video fallback failed',
            started,
        )

    @staticmethod
    def _result(
        success: bool,
        status: str,
        page_id: str,
        error_message: Optional[str],
        started: float,
        post_id: Optional[str] = None,
    ) -> PostResult:
        return PostResult(
            success=success,
            status=status,
            page_id=str(page_id or ''),
            post_id=post_id,
            error_message=error_message,
            execution_time_ms=int((time.monotonic() - started) * 1000),
        )

    @staticmethod
    async def _emit(progress_callback: ProgressCallback, event: Dict[str, Any]) -> None:
        if progress_callback is None:
            return
        await maybe_await(progress_callback(event))
