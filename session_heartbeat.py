"""
Session Heartbeat, Cookie Refresh & Behavioral Telemetry
========================================================
Keeps a Facebook cookie session alive for as long as possible by:

1. Sending a lightweight periodic keep-alive GET to facebook.com (heartbeat).
2. Periodically re-launching a stealth Playwright browser, loading the stored
   cookies, letting the page run so JS can refresh the xs/session tokens, then
   exporting the updated cookie jar back into TokenVault and Redis.
3. Sending synthetic /ajax/bz behavioral telemetry to avoid detection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, Optional

from .fb_client import FBClient
from .behavior_simulator import TelemetryFlusher, BehaviorSimulator

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


HEARTBEAT_INTERVAL_SECONDS = _env_int("FB_HEARTBEAT_INTERVAL_SECONDS", 900)
COOKIE_REFRESH_INTERVAL_SECONDS = _env_int("FB_COOKIE_REFRESH_INTERVAL_SECONDS", 1800)
REFRESH_NAV_TIMEOUT_MS = _env_int("FB_COOKIE_REFRESH_NAV_TIMEOUT_MS", 45_000)
_TELEMETRY_INTERVAL_SECONDS = _env_int("FB_TELEMETRY_INTERVAL_SECONDS", 60)

_COOKIE_HEADER_REDIS_PREFIX = "fb_live_cookie_header:"
_COOKIE_HEADER_REDIS_TTL = 7200


async def _send_heartbeat(cookie_header: str, user_agent: str) -> bool:
    """Lightweight GET to facebook.com to prevent idle-timeout."""
    try:
        import aiohttp
    except ImportError:
        logger.warning("aiohttp not available; skipping HTTP heartbeat.")
        return True

    headers = {
        "User-Agent": user_agent,
        "Cookie": cookie_header,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://www.facebook.com/",
                headers=headers,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                final_url = str(resp.url)
                text = await resp.text(errors="replace")
                if "/login" in final_url or "/checkpoint" in final_url:
                    logger.warning(
                        "Heartbeat: session expired/challenged. Final URL: %s",
                        final_url,
                    )
                    return False
                if "id_token" in text or "login_form" in text.lower():
                    logger.warning("Heartbeat: login form detected in response body.")
                    return False
                logger.debug("Heartbeat OK (HTTP %s).", resp.status)
                return True
    except Exception as exc:
        logger.warning("Heartbeat request failed: %s", exc)
        return False


async def _refresh_cookies_via_playwright(
    cookies_json: str,
    account_id: str,
    identity: Any,
    token_vault: Any,
    redis_client: Optional[Any] = None,
) -> Optional[str]:
    """Launch stealth Playwright to refresh cookies and tokens."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("Playwright not installed; cannot refresh cookies.")
        return None

    try:
        from browser_stealth import BrowserStealth, StealthConfig
    except ImportError:
        BrowserStealth = None
        StealthConfig = None

    logger.info("Cookie refresh: launching stealth browser for account %s.", account_id)

    async with async_playwright() as pw:
        launch_options: Dict[str, Any] = {
            "headless": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-extensions",
            ],
        }
        browser_executable = os.getenv("FACEBOOK_BROWSER_EXECUTABLE", "").strip()
        if browser_executable and os.path.exists(browser_executable):
            launch_options["executable_path"] = browser_executable

        browser = await pw.chromium.launch(**launch_options)

        user_agent = getattr(identity, "user_agent", None) or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        viewport_raw = getattr(identity, "viewport", None) or (1280, 720)
        if isinstance(viewport_raw, dict):
            viewport = viewport_raw
        elif isinstance(viewport_raw, (list, tuple)) and len(viewport_raw) >= 2:
            viewport = {"width": viewport_raw[0], "height": viewport_raw[1]}
        else:
            viewport = {"width": 1280, "height": 720}

        context = await browser.new_context(
            viewport=viewport,
            user_agent=user_agent,
            timezone_id=getattr(identity, "timezone", None) or "UTC",
            locale=getattr(identity, "locale", None) or "en-US",
        )

        if BrowserStealth and StealthConfig:
            stealth = BrowserStealth(StealthConfig(
                webgl_vendor=getattr(identity, "webgl_vendor", None),
                webgl_renderer=getattr(identity, "webgl_renderer", None),
            ))
            await stealth.apply_to_context(context)

        try:
            raw_cookies = json.loads(cookies_json)
            if isinstance(raw_cookies, dict) and "cookies" in raw_cookies:
                raw_cookies = raw_cookies["cookies"]
            if isinstance(raw_cookies, list):
                await context.add_cookies(raw_cookies)
        except Exception as exc:
            logger.warning("Cookie refresh: failed to load cookies: %s", exc)

        page = await context.new_page()
        refreshed_cookie_header: Optional[str] = None

        try:
            try:
                await page.goto(
                    "https://www.facebook.com/",
                    wait_until="domcontentloaded",
                    timeout=REFRESH_NAV_TIMEOUT_MS,
                )
            except Exception as nav_exc:
                logger.warning(
                    "Cookie refresh: navigation issue: %s. Proceeding.",
                    str(nav_exc)[:200],
                )
                try:
                    await page.evaluate("window.stop && window.stop()")
                except Exception:
                    pass

            await asyncio.sleep(3)

            current_url = page.url
            if "/login" in current_url or "/checkpoint" in current_url:
                logger.error(
                    "Cookie refresh: session expired (redirected to %s).", current_url
                )
                return None

            _TOKEN_SCRIPT = """() => {
                const get = (name) => { try { return require(name); } catch (_) { return {}; } };
                const cookie = document.cookie || '';
                const xs = (cookie.match(/(?:^|; )xs=([^;]+)/) || [])[1] || '';
                return {
                    fb_dtsg: get('DTSGInitialData').token || get('DTSGInitData').token
                              || document.querySelector('input[name="fb_dtsg"]')?.value || '',
                    lsd: get('LSD').token || document.querySelector('input[name="lsd"]')?.value || '',
                    jazoest: document.querySelector('input[name="jazoest"]')?.value || '',
                    user_id: get('CurrentUserInitialData').USER_ID || '',
                    xs,
                    revision: String(get('SiteData').client_revision || ''),
                    timestamp: Date.now() / 1000
                };
            }"""
            try:
                tokens = await page.evaluate(_TOKEN_SCRIPT)
            except Exception as eval_exc:
                logger.warning("Cookie refresh: token script failed: %s", eval_exc)
                tokens = {}

            browser_cookies = await context.cookies()
            if browser_cookies:
                parts = []
                for c in browser_cookies:
                    name = c.get("name", "")
                    value = c.get("value", "")
                    if name:
                        parts.append(f"{name}={value}")
                if parts:
                    refreshed_cookie_header = "; ".join(parts)
                    logger.info(
                        "Cookie refresh: exported %d cookies for account %s.",
                        len(parts), account_id,
                    )

            if isinstance(tokens, dict) and tokens.get("fb_dtsg"):
                merged = dict(tokens)
                if refreshed_cookie_header:
                    merged["cookie_header"] = refreshed_cookie_header
                merged["refreshed_at"] = time.time()
                merged.setdefault("timestamp", time.time())

                try:
                    from .utils import maybe_await
                    await maybe_await(token_vault.set(account_id, merged))
                    logger.info(
                        "Cookie refresh: token vault updated for account %s (fb_dtsg=%s…).",
                        account_id, str(merged.get("fb_dtsg", ""))[:8],
                    )
                except Exception as vault_exc:
                    logger.warning("Cookie refresh: token vault write failed: %s", vault_exc)
            else:
                logger.warning(
                    "Cookie refresh: no fb_dtsg found for account %s. Tokens NOT updated.",
                    account_id,
                )

            if refreshed_cookie_header and redis_client is not None:
                try:
                    from .utils import maybe_await
                    redis_key = f"{_COOKIE_HEADER_REDIS_PREFIX}{account_id}"
                    await maybe_await(
                        redis_client.setex(redis_key, _COOKIE_HEADER_REDIS_TTL, refreshed_cookie_header)
                    )
                except Exception as redis_exc:
                    logger.warning("Cookie refresh: Redis persist failed: %s", redis_exc)

        finally:
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass

    return refreshed_cookie_header


async def get_live_cookie_header(
    account_id: str,
    token_vault: Any,
    redis_client: Optional[Any] = None,
    fallback_cookie_header: str = "",
) -> str:
    """Return the most up-to-date cookie header for an account."""
    if redis_client is not None:
        try:
            from .utils import maybe_await
            redis_key = f"{_COOKIE_HEADER_REDIS_PREFIX}{account_id}"
            cached = await maybe_await(redis_client.get(redis_key))
            if cached:
                header = cached.decode("utf-8", errors="ignore") if isinstance(cached, bytes) else str(cached)
                if header.strip():
                    return header.strip()
        except Exception as exc:
            logger.debug("get_live_cookie_header: Redis lookup failed: %s", exc)

    try:
        from .utils import maybe_await
        tokens = await maybe_await(token_vault.get(account_id))
        if tokens and isinstance(tokens, dict):
            header = str(tokens.get("cookie_header") or "").strip()
            if header:
                return header
    except Exception as exc:
        logger.debug("get_live_cookie_header: token_vault lookup failed: %s", exc)

    return fallback_cookie_header


class SessionHeartbeatManager:
    """
    Long-running background coroutine that keeps a Facebook session alive and
    sends behavioral telemetry.

    Runs three independent loops:
      - Heartbeat loop  : lightweight GET every HEARTBEAT_INTERVAL_SECONDS.
      - Refresh loop    : full Playwright cookie refresh every COOKIE_REFRESH_INTERVAL_SECONDS.
      - Telemetry loop  : /ajax/bz behavioral events every TELEMETRY_INTERVAL_SECONDS.
    """

    def __init__(
        self,
        cookies_json: str,
        account_id: str,
        token_vault: Any,
        identity: Any,
        redis_client: Optional[Any] = None,
        on_session_expired: Optional[Any] = None,
        heartbeat_interval: int = HEARTBEAT_INTERVAL_SECONDS,
        refresh_interval: int = COOKIE_REFRESH_INTERVAL_SECONDS,
        telemetry_interval: int = _TELEMETRY_INTERVAL_SECONDS,
        fb_client: Optional[FBClient] = None,
    ) -> None:
        self.cookies_json = cookies_json
        self.account_id = account_id
        self.token_vault = token_vault
        self.identity = identity
        self.redis_client = redis_client
        self.on_session_expired = on_session_expired
        self.heartbeat_interval = heartbeat_interval
        self.refresh_interval = refresh_interval
        self.telemetry_interval = telemetry_interval
        self._session_alive = True
        self._last_cookie_header: str = ""
        self.fb_client = fb_client

        # Telemetry
        self._telemetry_flusher: Optional[TelemetryFlusher] = None

        try:
            raw = json.loads(cookies_json)
            if isinstance(raw, dict) and "cookies" in raw:
                raw = raw["cookies"]
            if isinstance(raw, list):
                parts = [f"{c['name']}={c['value']}" for c in raw if c.get("name")]
                self._last_cookie_header = "; ".join(parts)
        except Exception:
            self._last_cookie_header = ""

        # Initialize telemetry flusher if FBClient is available
        if fb_client is not None:
            sim = BehaviorSimulator(
                screen_w=getattr(identity, "screen_width", 1920),
                screen_h=getattr(identity, "screen_height", 1080),
            )
            self._telemetry_flusher = TelemetryFlusher(
                client=fb_client,
                account_id=account_id,
                simulator=sim,
                interval=telemetry_interval,
            )

    @property
    def session_alive(self) -> bool:
        return self._session_alive

    async def run_forever(self) -> None:
        """Start heartbeat, refresh, and telemetry loops concurrently."""
        logger.info(
            "SessionHeartbeatManager started for account %s "
            "(heartbeat=%ds, refresh=%ds, telemetry=%ds).",
            self.account_id, self.heartbeat_interval,
            self.refresh_interval, self.telemetry_interval,
        )

        tasks = [
            self._heartbeat_loop(),
            self._refresh_loop(),
        ]

        # Add telemetry loop if flusher is available
        if self._telemetry_flusher is not None:
            tasks.append(self._telemetry_flusher.run_forever())

        try:
            await asyncio.gather(*tasks, return_exceptions=False)
        except asyncio.CancelledError:
            logger.info("SessionHeartbeatManager cancelled for account %s.", self.account_id)
        except Exception as exc:
            logger.error("SessionHeartbeatManager unexpected error: %s", exc)

    async def flush_telemetry_pre_action(self):
        """Send pre-action telemetry burst before a post/comment/like."""
        if self._telemetry_flusher is not None:
            try:
                await self._telemetry_flusher.flush_pre_action()
            except Exception as exc:
                logger.debug("Pre-action telemetry burst failed: %s", exc)

    async def _heartbeat_loop(self) -> None:
        while self._session_alive:
            try:
                await asyncio.sleep(self.heartbeat_interval)
                if not self._session_alive:
                    break

                cookie_header = await get_live_cookie_header(
                    self.account_id,
                    self.token_vault,
                    self.redis_client,
                    fallback_cookie_header=self._last_cookie_header,
                )
                ua = getattr(self.identity, "user_agent", "") or ""
                alive = await _send_heartbeat(cookie_header, ua)
                if not alive:
                    self._session_alive = False
                    await self._handle_session_expired("Heartbeat detected session expiry")
                    return
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("Heartbeat loop error (will retry): %s", exc)

    async def _refresh_loop(self) -> None:
        while self._session_alive:
            try:
                logger.info("Cookie refresh cycle starting for account %s.", self.account_id)
                new_header = await _refresh_cookies_via_playwright(
                    self.cookies_json,
                    self.account_id,
                    self.identity,
                    self.token_vault,
                    self.redis_client,
                )
                if new_header:
                    self._last_cookie_header = new_header
                    logger.info("Cookie refresh cycle succeeded for account %s.", self.account_id)
                else:
                    logger.warning(
                        "Cookie refresh returned None for account %s. "
                        "Session may be expired.", self.account_id,
                    )
                await asyncio.sleep(self.refresh_interval)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error(
                    "Cookie refresh cycle error for account %s: %s — retrying in 60s.",
                    self.account_id, exc,
                )
                await asyncio.sleep(60)

    async def _handle_session_expired(self, reason: str) -> None:
        logger.error(
            "Session EXPIRED for account %s: %s. Manual re-login required.",
            self.account_id, reason,
        )
        if callable(self.on_session_expired):
            try:
                import inspect
                if inspect.iscoroutinefunction(self.on_session_expired):
                    await self.on_session_expired(self.account_id, reason)
                else:
                    self.on_session_expired(self.account_id, reason)
            except Exception as cb_exc:
                logger.warning("on_session_expired callback error: %s", cb_exc)
