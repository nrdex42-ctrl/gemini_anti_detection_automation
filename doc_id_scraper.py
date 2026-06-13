"""
doc_id Scraper
==============
Intercepts the Facebook GraphQL network requests made by a real Playwright
browser session to capture the live ``doc_id`` for ``ComposerStoryCreateMutation``
(and optionally other mutations).

The scraped value is stored in Redis under the key ``fb_graphql_doc_id`` so that
`HardenedGraphQLPoster._get_doc_id()` can pick it up automatically.

Usage::

    from doc_id_scraper import DocIdScraper

    scraper = DocIdScraper(redis_client=redis_client)
    doc_id = await scraper.scrape(cookies_json=cookies_json, identity=identity)
    # doc_id is also saved in Redis automatically

The scraper works by:
1. Launching a stealth Playwright browser with the stored cookies.
2. Registering a route/request interceptor on ``https://www.facebook.com/api/graphql/``.
3. Navigating to the Page-management composer URL (or the home feed).
4. Waiting until a POST to ``/api/graphql/`` with
   ``fb_api_req_friendly_name=ComposerStoryCreateMutation`` (or similar) is
   captured, then reading ``doc_id`` from its body.

If no real composer interaction is available (headless), we fall back to
parsing ``doc_id`` from the minified JS bundles that are loaded by the page
(slower, but works without user interaction).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import asyncio
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MUTATION_NAMES = (
    "ComposerStoryCreateMutation",
    "ComposerStoryCreate",
    "useCometComposerSubmit",
    "CometComposerStoryCreate",
)

# Redis key where we store the scraped doc_id
DOC_ID_REDIS_KEY = "fb_graphql_doc_id"
DOC_ID_REDIS_TTL = 86_400  # 24 hours — Facebook usually deploys less frequently

# Navigation timeout for the page load
NAV_TIMEOUT_MS = int(os.getenv("FB_DOC_ID_SCRAPE_NAV_TIMEOUT_MS", "45000"))

# How long to wait for a GraphQL request to appear during passive capture
CAPTURE_WAIT_SECONDS = int(os.getenv("FB_DOC_ID_CAPTURE_WAIT_SECONDS", "15"))


# ---------------------------------------------------------------------------
# Regex fallback — parse doc_id from inline JS
# ---------------------------------------------------------------------------

# Patterns seen in minified bundles, e.g.:
#   {"__bbox":{"require":[["ScheduledServerJS","handle",null,[{"__m":"ComposerStoryCreateMutation","id":"7711610262198779"}]]}}
# or simply: ComposerStoryCreateMutation","id":"<id>"
_DOC_ID_RE = re.compile(
    r'ComposerStoryCreate(?:Mutation)?"[^"]*",\s*"id"\s*:\s*"(\d{10,})"'
    r'|"doc_id"\s*:\s*"(\d{10,})"[^}]*ComposerStory'
    r'|ComposerStoryCreateMutation[^,]*,\s*"(\d{10,})"',
    re.IGNORECASE,
)
_PLAIN_DOC_ID_RE = re.compile(r'"doc_id"\s*:\s*"(\d{10,})"')


def _extract_doc_id_from_js(js: str) -> Optional[str]:
    """Try to find a ComposerStoryCreate doc_id from inline JS text."""
    for m in _DOC_ID_RE.finditer(js):
        for g in m.groups():
            if g and g.isdigit() and len(g) >= 10:
                return g
    # Broader fallback — grab any doc_id that appears near "Composer"
    context_matches = re.finditer(r'.{0,80}ComposerStory.{0,80}', js)
    for cm in context_matches:
        surrounding = cm.group(0)
        dm = _PLAIN_DOC_ID_RE.search(surrounding)
        if dm:
            return dm.group(1)
    return None


# ---------------------------------------------------------------------------
# DocIdScraper
# ---------------------------------------------------------------------------

class DocIdScraper:
    """
    Scrapes the live ``doc_id`` for ``ComposerStoryCreateMutation`` from Facebook.

    Parameters
    ----------
    redis_client:
        An async Redis client (e.g. ``redis.asyncio``).  Optional; if None,
        the doc_id is returned but not stored.
    """

    def __init__(self, redis_client: Optional[Any] = None) -> None:
        self.redis = redis_client

    async def get_cached(self) -> Optional[str]:
        """Return the most recently scraped doc_id from Redis, or None."""
        if self.redis is None:
            return None
        try:
            from .utils import maybe_await
            raw = await maybe_await(self.redis.get(DOC_ID_REDIS_KEY))
            if raw:
                value = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else str(raw)
                if value.isdigit():
                    return value
        except Exception as exc:
            logger.debug("DocIdScraper: Redis get failed: %s", exc)
        return None

    async def store(self, doc_id: str) -> None:
        """Persist the doc_id to Redis."""
        if self.redis is None or not doc_id:
            return
        try:
            from .utils import maybe_await
            await maybe_await(self.redis.setex(DOC_ID_REDIS_KEY, DOC_ID_REDIS_TTL, doc_id))
            logger.info("DocIdScraper: stored doc_id=%s in Redis (TTL %ds).", doc_id, DOC_ID_REDIS_TTL)
        except Exception as exc:
            logger.warning("DocIdScraper: Redis store failed: %s", exc)

    async def scrape(
        self,
        cookies_json: str,
        identity: Any,
        page_url: str = "https://www.facebook.com/",
    ) -> Optional[str]:
        """
        Launch a stealth browser, load the cookies, and attempt to capture
        the live doc_id via two strategies:

        1. **Network interception**: intercept POSTs to /api/graphql/ and read
           doc_id from the POST body.  Works if any GraphQL mutation fires while
           the page loads.

        2. **JS bundle scan**: parse inline <script> tags and loaded JS resources
           for the doc_id pattern.

        Returns the doc_id string on success, or None on failure.
        Automatically stores the result in Redis if a client was provided.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.error("DocIdScraper: playwright is not installed.")
            return None

        try:
            from browser_stealth import BrowserStealth, StealthConfig
        except ImportError:
            BrowserStealth = None  # type: ignore
            StealthConfig = None   # type: ignore

        captured_doc_id: Optional[str] = None
        capture_event = asyncio.Event()

        logger.info("DocIdScraper: starting browser for doc_id capture (%s).", page_url)

        async with async_playwright() as pw:
            launch_options: Dict[str, Any] = {
                "headless": True,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-gpu",
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

            # Load cookies
            try:
                raw_cookies = json.loads(cookies_json)
                if isinstance(raw_cookies, dict) and "cookies" in raw_cookies:
                    raw_cookies = raw_cookies["cookies"]
                if isinstance(raw_cookies, list):
                    await context.add_cookies(raw_cookies)
            except Exception as exc:
                logger.warning("DocIdScraper: failed to load cookies: %s", exc)

            page = await context.new_page()

            # ----------------------------------------------------------------
            # Strategy 1: intercept POST /api/graphql/ requests
            # ----------------------------------------------------------------
            js_sources: List[str] = []

            async def _on_request(request: Any) -> None:
                nonlocal captured_doc_id
                if captured_doc_id:
                    return
                if "api/graphql" not in request.url:
                    return
                if request.method != "POST":
                    return
                try:
                    body_bytes = request.post_data_buffer or b""
                    body = body_bytes.decode("utf-8", errors="ignore") if isinstance(body_bytes, bytes) else str(body_bytes)
                    friendly_name = ""
                    # Try parse as form-encoded
                    from urllib.parse import parse_qs
                    try:
                        params = parse_qs(body)
                        friendly_name = (params.get("fb_api_req_friendly_name") or [""])[0]
                        doc_id_candidate = (params.get("doc_id") or [""])[0]
                    except Exception:
                        doc_id_candidate = ""

                    if not doc_id_candidate:
                        # Try JSON body
                        try:
                            obj = json.loads(body)
                            friendly_name = obj.get("fb_api_req_friendly_name", "")
                            doc_id_candidate = str(obj.get("doc_id") or "")
                        except Exception:
                            pass

                    # Check doc_id via regex in raw body as last resort
                    if not doc_id_candidate:
                        m = re.search(r'"doc_id"\s*:\s*"?(\d{10,})', body)
                        if m:
                            doc_id_candidate = m.group(1)

                    if doc_id_candidate and doc_id_candidate.isdigit():
                        is_composer = any(name.lower() in friendly_name.lower() for name in MUTATION_NAMES)
                        if is_composer or not friendly_name:
                            logger.info(
                                "DocIdScraper: captured doc_id=%s from request "
                                "(friendly_name=%r).",
                                doc_id_candidate, friendly_name,
                            )
                            captured_doc_id = doc_id_candidate
                            capture_event.set()
                except Exception as exc:
                    logger.debug("DocIdScraper: request interception error: %s", exc)

            async def _on_response(response: Any) -> None:
                """Also check responses to capture doc_id from GraphQL payloads."""
                nonlocal captured_doc_id
                if captured_doc_id:
                    return
                if "api/graphql" not in response.url:
                    return
                try:
                    text = await response.text()
                    m = re.search(r'"doc_id"\s*:\s*"?(\d{10,})', text)
                    if m:
                        doc_id_candidate = m.group(1)
                        logger.info(
                            "DocIdScraper: captured doc_id=%s from response body.", doc_id_candidate
                        )
                        captured_doc_id = doc_id_candidate
                        capture_event.set()
                except Exception:
                    pass

            page.on("request", _on_request)
            page.on("response", _on_response)

            # ----------------------------------------------------------------
            # Strategy 2: collect inline JS for bundle scan
            # ----------------------------------------------------------------
            async def _on_js_response(response: Any) -> None:
                ct = response.headers.get("content-type", "")
                if "javascript" in ct or response.url.endswith(".js"):
                    try:
                        src = await response.text()
                        if "ComposerStory" in src:
                            js_sources.append(src[:500_000])  # limit to 500 KB per file
                    except Exception:
                        pass

            page.on("response", _on_js_response)

            try:
                try:
                    await page.goto(page_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                except Exception as nav_exc:
                    logger.warning(
                        "DocIdScraper: navigation incomplete: %s. Continuing with current page state.",
                        str(nav_exc)[:200],
                    )
                    try:
                        await page.evaluate("window.stop && window.stop()")
                    except Exception:
                        pass

                # Wait for a network capture or the timeout
                try:
                    await asyncio.wait_for(capture_event.wait(), timeout=CAPTURE_WAIT_SECONDS)
                except asyncio.TimeoutError:
                    logger.debug(
                        "DocIdScraper: no GraphQL request captured within %ds; "
                        "falling back to JS bundle scan.",
                        CAPTURE_WAIT_SECONDS,
                    )

                # ----------------------------------------------------------------
                # Fallback: scan inline scripts from the page
                # ----------------------------------------------------------------
                if not captured_doc_id:
                    try:
                        inline_scripts = await page.evaluate("""() => {
                            return Array.from(document.querySelectorAll('script'))
                                .map(s => s.textContent || '')
                                .filter(t => t.includes('ComposerStory') || t.includes('doc_id'))
                                .join('\\n');
                        }""")
                        if inline_scripts:
                            captured_doc_id = _extract_doc_id_from_js(inline_scripts)
                            if captured_doc_id:
                                logger.info(
                                    "DocIdScraper: found doc_id=%s in inline page scripts.",
                                    captured_doc_id,
                                )
                    except Exception as exc:
                        logger.debug("DocIdScraper: inline script scan failed: %s", exc)

                # Scan already-captured JS bundles
                if not captured_doc_id:
                    for src in js_sources:
                        captured_doc_id = _extract_doc_id_from_js(src)
                        if captured_doc_id:
                            logger.info(
                                "DocIdScraper: found doc_id=%s in JS bundle.", captured_doc_id
                            )
                            break

            finally:
                try:
                    await context.close()
                except Exception:
                    pass
                try:
                    await browser.close()
                except Exception:
                    pass

        if captured_doc_id:
            await self.store(captured_doc_id)
        else:
            logger.warning(
                "DocIdScraper: could not determine a live doc_id. "
                "The hardcoded fallback in graphql_poster.py will be used."
            )

        return captured_doc_id


# ---------------------------------------------------------------------------
# Convenience: background periodic re-scrape
# ---------------------------------------------------------------------------

async def run_doc_id_refresh_loop(
    cookies_json: str,
    identity: Any,
    redis_client: Optional[Any] = None,
    refresh_interval_seconds: int = 21_600,  # 6 hours
) -> None:
    """
    Background coroutine that re-scrapes the doc_id periodically.
    Cancel it with asyncio cancellation.

    Parameters
    ----------
    refresh_interval_seconds:
        How often to re-scrape. Defaults to 6 hours.  Facebook typically only
        updates doc_ids on major frontend deploys.
    """
    scraper = DocIdScraper(redis_client=redis_client)
    logger.info(
        "doc_id refresh loop started (interval=%ds).", refresh_interval_seconds
    )
    while True:
        try:
            doc_id = await scraper.scrape(cookies_json=cookies_json, identity=identity)
            if doc_id:
                logger.info("doc_id refresh loop: scraped doc_id=%s.", doc_id)
            else:
                logger.warning("doc_id refresh loop: scrape returned None.")
        except asyncio.CancelledError:
            logger.info("doc_id refresh loop cancelled.")
            return
        except Exception as exc:
            logger.error("doc_id refresh loop error: %s — will retry.", exc)
        try:
            await asyncio.sleep(refresh_interval_seconds)
        except asyncio.CancelledError:
            return
