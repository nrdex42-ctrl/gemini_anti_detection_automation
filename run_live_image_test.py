#!/usr/bin/env python3
"""
Live image posting test — posts to all discovered Facebook pages.

Usage:
    python3 run_live_image_test.py

Requires:
    - playwright (pip install playwright && playwright install chromium)
    - Pillow

Environment:
    - FB_RAW_COOKIE_STRING — set automatically by this script
    - LIVE_EMULATION_IMAGE_PATH — set automatically by this script
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import logging
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Ensure fb_automation is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("live_image_test")

# ─── Configuration ───────────────────────────────────────────────────────────

COOKIE_STRING = os.getenv("FB_RAW_COOKIE_STRING", "").strip() or os.getenv("FB_COOKIE_STRING", "").strip()

IMAGE_PATH = str(Path(__file__).resolve().parent / "Screenshot from 2026-05-31 13-30-37.png")

ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts" / "live_test"

UPLOAD_URL_MARKERS = (
    "upload.facebook.com",
    "rupload.facebook.com",
    "/ajax/react_composer/attachments/photo/upload",
    "/composer/attachments",
    "/media/upload",
)


def env_timeout_ms(name: str, default_seconds: float) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return int(default_seconds * 1000)
    try:
        return int(max(0.5, float(raw)) * 1000)
    except ValueError:
        logger.warning("Invalid %s=%r; using %.1fs", name, raw, default_seconds)
        return int(default_seconds * 1000)


def safe_log_url(url: str) -> str:
    """Drop query strings so upload traces do not leak request parameters."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed.scheme else url.split("?", 1)[0]


def upload_endpoint_kind(url: str) -> str:
    lowered = url.lower()
    if "rupload.facebook.com" in lowered:
        return "rupload"
    if "upload.facebook.com" in lowered:
        return "upload_facebook"
    if "/composer/attachments" in lowered:
        return "composer_attachments"
    if "/media/upload" in lowered:
        return "media_upload"
    return "upload"


def extract_upload_error(url: str, body: str) -> str:
    """Return a concise upload error without including auth material."""
    combined = f"{url}\n{body[:4000]}"
    parsed_query = parse_qs(urlparse(url).query)
    for key in ("error", "error_code", "errorSummary", "errorDescription"):
        value = parsed_query.get(key)
        if value:
            return f"{key}={str(value[0])[:180]}"

    if "1366046" in combined:
        return "error=1366046"

    patterns = (
        r'"error"\s*:\s*"?([^",}\n]{1,180})',
        r'"errorSummary"\s*:\s*"([^"]{1,180})"',
        r'"errorDescription"\s*:\s*"([^"]{1,180})"',
        r"Your photos couldn.?t be uploaded[^<\n]{0,180}",
        r"Can.?t read files[^<\n]{0,180}",
        r"NotAuthorizedError[^<\n]{0,180}",
    )
    for pattern in patterns:
        match = re.search(pattern, combined, re.I)
        if match:
            return (match.group(1) if match.lastindex else match.group(0)).strip()[:180]
    return ""


def attach_upload_response_tracker(page, label: str):
    state = {
        "label": label,
        "events": [],
        "last_error": "",
        "logical_error": False,
        "completed_success": 0,
    }

    async def handle_response(response):
        url = response.url
        lowered = url.lower()
        if not any(marker in lowered for marker in UPLOAD_URL_MARKERS):
            return

        event = {
            "endpoint": upload_endpoint_kind(url),
            "url": safe_log_url(url),
            "status": response.status,
        }
        body = ""
        try:
            body = await response.text()
        except Exception as exc:
            event["read_error"] = str(exc)[:120]

        error = extract_upload_error(url, body)
        if response.status >= 400 or error:
            event["error"] = error or f"HTTP {response.status}"
            state["last_error"] = event["error"]
            state["logical_error"] = response.status < 400
        else:
            state["completed_success"] = int(state.get("completed_success") or 0) + 1

        state["events"].append(event)
        del state["events"][:-12]
        logger.info(
            "Upload response %s: endpoint=%s status=%s error=%s",
            label,
            event["endpoint"],
            event["status"],
            event.get("error") or "",
        )

    def listener(response):
        asyncio.create_task(handle_response(response))

    page.on("response", listener)

    def detach():
        try:
            page.remove_listener("response", listener)
        except Exception:
            pass

    return state, detach


async def visible_dialog_error(page) -> str:
    selectors = [
        "div[role='dialog'] [role='alert']",
        "div[role='dialog']:has-text(\"couldn't\")",
        "div[role='dialog']:has-text(\"couldn’t\")",
        "div[role='dialog']:has-text(\"Can't read files\")",
        "div[role='dialog']:has-text('error')",
        "div[role='dialog']:has-text('failed')",
    ]
    for selector in selectors:
        try:
            element = page.locator(selector).first
            if await element.count() > 0 and await element.is_visible(timeout=400):
                text = (await element.text_content() or "").strip()
                if text:
                    return re.sub(r"\s+", " ", text)[:240]
        except Exception:
            continue
    return ""


async def click_exact_dialog_button(page, names, timeout: int = 5000) -> str:
    """Click an exact button name inside the composer dialog."""
    deadline = time.monotonic() + (timeout / 1000)
    dialog = page.locator("div[role='dialog']").first
    while time.monotonic() < deadline:
        for name in names:
            pattern = re.compile(rf"^\s*{re.escape(name)}\s*$", re.I)
            locators = [
                dialog.get_by_role("button", name=pattern).last,
                dialog.locator("div[role='button']").filter(has_text=pattern).last,
            ]
            for button in locators:
                try:
                    if await button.count() <= 0:
                        continue
                    if not await button.is_visible(timeout=500):
                        continue
                    enabled = await button.evaluate(
                        """el => {
                            const disabledAttr = node => {
                                if (!node || node.nodeType !== Node.ELEMENT_NODE) return false;
                                const style = window.getComputedStyle(node);
                                return node.getAttribute('aria-disabled') === 'true' ||
                                    node.getAttribute('disabled') !== null ||
                                    node.disabled === true ||
                                    node.inert === true ||
                                    style.pointerEvents === 'none';
                            };
                            let node = el;
                            for (let depth = 0; node && depth < 5; depth += 1, node = node.parentElement) {
                                if (disabledAttr(node)) return false;
                            }
                            return true;
                        }"""
                    )
                    if not enabled:
                        continue
                    await button.click(timeout=2000)
                    return name
                except Exception:
                    continue
        try:
            clicked_name = await page.evaluate(
                """(names) => {
                    const normalize = value => String(value || '')
                        .replace(/\\s+/g, ' ')
                        .trim()
                        .toLowerCase();
                    const visible = el => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.visibility !== 'hidden' &&
                            style.display !== 'none' &&
                            rect.width > 2 &&
                            rect.height > 2;
                    };
                    const enabled = el => {
                        let node = el;
                        for (let depth = 0; node && depth < 5; depth += 1, node = node.parentElement) {
                            const style = window.getComputedStyle(node);
                            if (node.getAttribute('aria-disabled') === 'true' ||
                                node.getAttribute('disabled') !== null ||
                                node.disabled === true ||
                                node.inert === true ||
                                style.pointerEvents === 'none') {
                                return false;
                            }
                        }
                        return true;
                    };
                    const dialogs = Array.from(document.querySelectorAll('[role="dialog"]'))
                        .filter(visible);
                    const root = dialogs.find(el => /create post/i.test(el.innerText || '')) ||
                        dialogs[dialogs.length - 1] ||
                        document.body;
                    const scopedCandidates = Array.from(root.querySelectorAll(
                        'button, [role="button"], [aria-label], [tabindex], div, span'
                    ));
                    const globalCandidates = Array.from(document.querySelectorAll(
                        'button, [role="button"], [aria-label], [tabindex], div, span'
                    ));
                    const candidates = [...scopedCandidates, ...globalCandidates].filter(visible);
                    for (const name of names) {
                        const wanted = normalize(name);
                        const matches = candidates.filter(el => {
                            const label = normalize(el.getAttribute('aria-label'));
                            const text = normalize(el.innerText || el.textContent);
                            return enabled(el) && (label === wanted || text === wanted);
                        });
                        const match = matches[matches.length - 1];
                        if (match) {
                            match.scrollIntoView({block: 'center', inline: 'center'});
                            match.click();
                            return name;
                        }
                    }
                    return '';
                }""",
                names,
            )
            if clicked_name:
                return str(clicked_name)
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return ""


async def click_publish_flow(page) -> tuple[bool, str]:
    """Handle Facebook's page composer, which often requires Next then Post."""
    first = await click_exact_dialog_button(
        page,
        ["Post", "Next"],
        timeout=env_timeout_ms("LIVE_PUBLISH_FIRST_BUTTON_TIMEOUT_SECONDS", 20.0),
    )
    if not first:
        return False, "No exact Post/Next button found"
    logger.info("   ✅ Clicked %s button", first)
    if first.lower() == "post":
        return True, "Post"

    await asyncio.sleep(1.5)
    second = await click_exact_dialog_button(
        page,
        ["Post"],
        timeout=env_timeout_ms("LIVE_PUBLISH_SECOND_BUTTON_TIMEOUT_SECONDS", 90.0),
    )
    if not second:
        return False, "Clicked Next, but no exact Post button appeared"
    logger.info("   ✅ Clicked Post button")
    return True, "Next -> Post"


async def handle_post_submit_interstitial(page) -> str:
    """Handle Facebook dialogs that appear after clicking the final Post button."""
    return await click_exact_dialog_button(
        page,
        [
            "Publish Original Post",
            "Publish original post",
            "Publish post",
            "Not now",
            "No thanks",
            "Maybe later",
            "Later",
            "Skip",
            "Done",
            "Close",
            "نشر المنشور الأصلي",
            "ليس الآن",
            "لاحقاً",
            "لا شكرًا",
            "تخطي",
            "تم",
            "إغلاق",
        ],
        timeout=1200,
    )


async def set_existing_image_file_input(page, image_path: str) -> str:
    """Set an existing file input without opening the native file chooser."""
    selectors = [
        "div[role='dialog'] input[type='file'][accept*='image']",
        "div[role='dialog'] input[type='file']",
        "input[type='file'][accept*='image']",
        "input[type='file']",
    ]
    for selector in selectors:
        try:
            file_input = page.locator(selector).first
            if await file_input.count() <= 0:
                continue
            await file_input.set_input_files(image_path, timeout=7000)
            return selector
        except Exception:
            continue
    return ""


async def upload_image_without_stale_file_dialog(page, image_path: str) -> str:
    """Upload image while avoiding an unmanaged native file picker window."""
    direct_selector = await set_existing_image_file_input(page, image_path)
    if direct_selector:
        return f"direct input: {direct_selector}"

    photo_selectors = [
        "div[role='dialog'] div[aria-label='Photo/video']",
        "div[role='dialog'] div[aria-label='Photo/Video']",
        "div[role='dialog'] [aria-label='Photo/video']",
        "div[role='dialog'] div[role='button']:has-text('Photo')",
        "div[role='dialog'] [aria-label*='hoto']",
        "div[role='dialog'] [aria-label*='Photo']",
    ]

    for selector in photo_selectors:
        photo_btn = page.locator(selector).first
        try:
            if await photo_btn.count() <= 0:
                continue
            try:
                async with page.expect_file_chooser(timeout=3500) as chooser_info:
                    await photo_btn.click(timeout=3000)
                file_chooser = await chooser_info.value
                await file_chooser.set_files(image_path)
                return f"file chooser: {selector}"
            except Exception:
                direct_selector = await set_existing_image_file_input(page, image_path)
                if direct_selector:
                    return f"post-click input: {direct_selector}"
        except Exception:
            continue

    return ""


def parse_cookies(cookie_string: str):
    """Parse cookie string into Playwright-compatible format."""
    cookies = []
    for pair in cookie_string.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        cookies.append({
            "name": name.strip(),
            "value": value.strip(),
            "domain": ".facebook.com",
            "path": "/",
        })
    return cookies


async def run_test():
    """Main test runner."""
    from playwright.async_api import async_playwright

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    cookies = parse_cookies(COOKIE_STRING)
    timestamp = time.strftime("%Y%m%d_%H%M%S")

    logger.info("=" * 60)
    logger.info("  🚀 LIVE IMAGE POSTING TEST")
    logger.info("=" * 60)
    logger.info("Image: %s", IMAGE_PATH)
    logger.info("Cookies: %d entries", len(cookies))

    if not Path(IMAGE_PATH).exists():
        logger.error("Image file not found: %s", IMAGE_PATH)
        return

    async with async_playwright() as pw:
        # Launch browser (visible so user can watch)
        browser = await pw.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )

        context = await browser.new_context(
            viewport={"width": 1294, "height": 617},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        )

        # Inject cookies
        await context.add_cookies(cookies)
        logger.info("✅ Cookies injected")

        page = await context.new_page()

        # ─── Step 1: Navigate to Facebook and extract tokens ─────────
        logger.info("Navigating to Facebook...")
        await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
        
        # Wait for the splash screen to disappear and main content to load
        try:
            await page.wait_for_selector('div[role="navigation"], form[action*="login"]', timeout=15000)
        except Exception:
            logger.warning("Main content took too long to load, proceeding anyway...")
            
        await asyncio.sleep(3)

        # Take screenshot
        ss_path = ARTIFACT_DIR / f"01_facebook_home_{timestamp}.png"
        await page.screenshot(path=str(ss_path))
        logger.info("Screenshot saved: %s", ss_path)

        # Extract tokens
        tokens = await page.evaluate("""() => {
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
        }""")

        logger.info(
            "Tokens: fb_dtsg=%s lsd=%s user_id=%s",
            "✅" if tokens.get("fb_dtsg") else "❌",
            "✅" if tokens.get("lsd") else "❌",
            tokens.get("user_id") or "❌",
        )

        if not tokens.get("fb_dtsg"):
            logger.error("❌ Failed to extract fb_dtsg — cookies may be expired!")
            ss_path2 = ARTIFACT_DIR / f"error_no_token_{timestamp}.png"
            await page.screenshot(path=str(ss_path2))
            await browser.close()
            return

        # ─── Step 2: Discover pages ──────────────────────────────────
        logger.info("Discovering managed pages...")
        await page.goto(
            "https://www.facebook.com/pages/?category=your_pages",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await asyncio.sleep(3)

        ss_path3 = ARTIFACT_DIR / f"02_pages_directory_{timestamp}.png"
        await page.screenshot(path=str(ss_path3))
        logger.info("Screenshot saved: %s", ss_path3)

        # --- Strategy A: Extract page IDs from page HTML source ---
        # Only use specific page-related JSON keys (NOT generic "id" which matches everything)
        page_content = await page.content()
        page_ids = list(set(
            re.findall(r'"pageID":"(\d+)"', page_content)
            + re.findall(r'"page_id":"(\d+)"', page_content)
            + re.findall(r'"ownerID":"(\d+)"', page_content)
            + re.findall(r'"pageId":"(\d+)"', page_content)
        ))
        # Remove the logged-in user's own ID from page candidates
        user_id = tokens.get("user_id", "")
        page_ids = [pid for pid in page_ids if pid != user_id]
        logger.info("Found page IDs from source regex: %s", page_ids)

        # --- Strategy B: Extract pages from visible DOM cards ---
        dom_pages = await page.evaluate("""() => {
            const pages = [];
            // Find all links that contain profile.php?id= (page links on the cards)
            document.querySelectorAll('a[href*="profile.php?id="]').forEach(a => {
                const href = a.href || '';
                const text = (a.textContent || '').trim();
                // Skip navigation links, settings, category filters
                if (!text || text.length > 100) return;
                if (href.includes('category') || href.includes('?sk=')) return;
                const idMatch = href.match(/id=(\d+)/);
                if (idMatch) {
                    pages.push({ id: idMatch[1], name: text, url: href });
                }
            });
            // Also look for page name heading elements near "Create Post" buttons
            document.querySelectorAll('span').forEach(span => {
                const text = (span.textContent || '').trim();
                if (!text || text.length < 2 || text.length > 60) return;
                const parent = span.closest('div');
                if (!parent) return;
                // Check if a sibling/nearby element has "Create Post" text
                const parentHTML = parent.parentElement?.innerHTML || '';
                if (parentHTML.includes('Create Post') || parentHTML.includes('إنشاء منشور')) {
                    // Look for a nearby link with profile ID
                    const nearbyLink = parent.closest('div')?.querySelector('a[href*="profile.php?id="]');
                    if (nearbyLink) {
                        const idMatch = nearbyLink.href.match(/id=(\d+)/);
                        if (idMatch) {
                            pages.push({ id: idMatch[1], name: text, url: nearbyLink.href });
                        }
                    }
                }
            });
            return pages;
        }""")
        logger.info("Found pages from DOM cards: %s", [(p.get("name"), p.get("id")) for p in dom_pages])

        # --- Strategy C: GraphQL API fallback ---
        graphql_pages = []
        if tokens.get("fb_dtsg"):
            try:
                graphql_pages = await page.evaluate("""async (fb_dtsg) => {
                    try {
                        const resp = await fetch('https://www.facebook.com/api/graphql/', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                            body: new URLSearchParams({
                                fb_dtsg: fb_dtsg,
                                fb_api_req_friendly_name: 'ProfileCometManagedPagesQuery',
                                variables: JSON.stringify({count: 20}),
                                doc_id: '6939853642762493'
                            }).toString()
                        });
                        const text = await resp.text();
                        const ids = [...text.matchAll(/"id":"(\d{10,})"/g)].map(m => m[1]);
                        const names = [...text.matchAll(/"name":"([^"]+)"/g)].map(m => m[1]);
                        const pages = [];
                        for (let i = 0; i < ids.length; i++) {
                            pages.push({ id: ids[i], name: names[i] || ids[i], url: 'https://www.facebook.com/profile.php?id=' + ids[i] });
                        }
                        return pages;
                    } catch (e) { return []; }
                }""", tokens.get("fb_dtsg", ""))
                logger.info("Found pages from GraphQL API: %s", [(p.get("name"), p.get("id")) for p in graphql_pages])
            except Exception as e:
                logger.warning("GraphQL page discovery failed: %s", e)

        # --- Strategy D: Profile switcher in account menu ---
        await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(2)

        profile_pages = []
        try:
            account_btn = page.locator('[aria-label="Your profile"], [aria-label="Account"], [aria-label="Account controls and settings"]').first
            if await account_btn.count() > 0:
                await account_btn.click(timeout=3000)
                await asyncio.sleep(2)

                ss_path4 = ARTIFACT_DIR / f"03_account_menu_{timestamp}.png"
                await page.screenshot(path=str(ss_path4))

                see_all = page.locator('text="See all profiles"').first
                if await see_all.count() > 0:
                    await see_all.click(timeout=3000)
                    await asyncio.sleep(2)

                    ss_path5 = ARTIFACT_DIR / f"04_profile_switcher_{timestamp}.png"
                    await page.screenshot(path=str(ss_path5))

                    profile_pages = await page.evaluate("""() => {
                        const pages = [];
                        document.querySelectorAll('[role="dialog"] a, [role="menu"] a').forEach(a => {
                            const text = (a.textContent || '').trim();
                            const href = a.href || '';
                            if (text && href && href.includes('facebook.com')) {
                                pages.push({ name: text, url: href });
                            }
                        });
                        return pages;
                    }""")

                await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning("Profile switcher exploration failed: %s", e)

        # ── Combine all discovered pages ──
        all_pages = []
        seen_ids = set()

        def _add_page(pid, url, name):
            if pid and pid != user_id and pid not in seen_ids:
                seen_ids.add(pid)
                all_pages.append({"id": pid, "url": url or f"https://www.facebook.com/profile.php?id={pid}", "name": name or pid})

        # Source: regex from HTML
        for pid in page_ids:
            _add_page(pid, f"https://www.facebook.com/profile.php?id={pid}", pid)

        # Source: DOM cards (highest quality — has real page names)
        for item in dom_pages:
            _add_page(item.get("id", ""), item.get("url", ""), item.get("name", ""))

        # Source: GraphQL API
        for item in graphql_pages:
            _add_page(item.get("id", ""), item.get("url", ""), item.get("name", ""))

        # Source: profile switcher
        for item in profile_pages:
            name = item.get("name", "")
            url = item.get("url", "")
            pid_match = re.search(r'id=(\d+)', url)
            pid = pid_match.group(1) if pid_match else ""
            _add_page(pid, url, name)

        # Last resort: if STILL nothing found, do NOT fall back to user profile
        if not all_pages:
            logger.warning("⚠️ No managed pages discovered by any strategy. Skipping profile fallback.")

        logger.info("=" * 60)
        logger.info("  📄 DISCOVERED PAGES: %d", len(all_pages))
        for p in all_pages:
            logger.info("    • %s (%s)", p["name"], p["url"])
        logger.info("=" * 60)

        if not all_pages:
            logger.error("❌ No pages discovered. Cannot post.")
            await browser.close()
            return

        # ─── Step 3: Post image to each page ─────────────────────────
        results = []

        for page_info in all_pages:
            page_url = page_info["url"]
            page_name = page_info["name"]
            page_id = page_info.get("id", "")
            upload_tracker, detach_upload_tracker = attach_upload_response_tracker(page, page_name)

            logger.info("─" * 60)
            logger.info("📸 Posting image to: %s", page_name)
            logger.info("   URL: %s", page_url)

            try:
                # helper function to switch identity via account menu
                async def switch_profile_via_menu(target_name: str) -> bool:
                    logger.info("   Switching profile to '%s' via Account Menu...", target_name)
                    # 1. Click Account Button
                    account_btn = page.locator(
                        "div[role='banner'] div[aria-label*='profile' i], "
                        "div[role='banner'] div[aria-label*='Profile' i], "
                        "div[role='banner'] div[aria-label*='Account' i], "
                        "div[role='banner'] div[aria-label*='الملف الشخصي' i], "
                        "div[role='banner'] div[aria-label*='الحساب' i]"
                    ).first
                    
                    if await account_btn.count() == 0:
                        logger.warning("   Account button not found in banner.")
                        return False
                        
                    await account_btn.click(timeout=5000)
                    await asyncio.sleep(1.5)
                    
                    # 2. Click "See all profiles" if visible
                    see_all = page.locator(
                        "text='See all profiles', "
                        "text='عرض كل الملفات الشخصية', "
                        "text='See All Profiles'"
                    ).first
                    if await see_all.count() > 0 and await see_all.is_visible():
                        await see_all.click(timeout=5000)
                        await asyncio.sleep(1.5)
                        
                    # 3. Click the target page profile
                    target_btn = page.locator(
                        f"div[role='button']:has-text('{target_name}'), "
                        f"div[role='link']:has-text('{target_name}'), "
                        f"text='{target_name}'"
                    ).first
                    
                    if await target_btn.count() > 0:
                        await target_btn.click(timeout=5000)
                        logger.info("   Switch clicked. Waiting for profile switch reload...")
                        await asyncio.sleep(6) # Wait for page switch reload
                        return True
                    else:
                        logger.warning("   Target profile '%s' not found in switcher menu.", target_name)
                        await page.keyboard.press("Escape")
                        await asyncio.sleep(1)
                        return False

                # Reset identity to main user before switching (ensures we start from a clean slate)
                await context.clear_cookies()
                await context.add_cookies(cookies)
                
                # Navigate to facebook home to have a clean header menu
                await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(3)
                
                # Perform the profile switch
                switched = await switch_profile_via_menu(page_name)
                if not switched:
                    logger.warning("   UI profile switch failed, falling back to direct navigation...")
                    await page.goto(page_url, wait_until="domcontentloaded", timeout=20000)
                    await asyncio.sleep(5)
                
                # --- Validate page is accessible (skip "page isn't available" errors) ---
                page_body = await page.text_content('body') or ''
                if any(phrase in page_body for phrase in [
                    "This page isn't available",
                    "This content isn't available",
                    "هذه الصفحة غير متاحة",
                    "Reload page",
                ]):
                    logger.warning("   ⚠️ Page %s is not accessible — skipping", page_name)
                    results.append({
                        "page": page_name,
                        "success": False,
                        "error": "Page not accessible",
                        "upload_trace": upload_tracker["events"],
                    })
                    continue

                ss_before = ARTIFACT_DIR / f"05_page_{page_id}_{timestamp}.png"
                await page.screenshot(path=str(ss_before))

                # Check if composer is ALREADY open (clicking 'Create Post' on the card may open it directly)
                composer_found = False
                try:
                    if await page.locator("div[role='dialog']").count() > 0 and await page.locator("div[role='dialog']").first.is_visible():
                        composer_found = True
                        logger.info("   ✅ Composer popup is already open from directory card!")
                except Exception:
                    pass

                # Find and click the composer (with scrolling + Arabic selectors) if not already open
                if not composer_found:
                    composer_selectors = [
                        "div[role='button']:has-text(\"What's on your mind\")",
                        "div[role='button']:has-text('بم تفكر')",
                        "[aria-label='Create post']",
                        "[aria-label='إنشاء منشور']",
                        "div[role='button']:has-text('Create post')",
                        "div[role='button']:has-text('إنشاء منشور')",
                        "div[role='button']:has-text('Write something')",
                        "div[role='button']:has-text('اكتب شيئًا')",
                    ]

                    # Try multiple scroll positions to find the composer below the fold
                    # Try multiple scroll positions to find the composer below the fold
                    for scroll_y in [0, 400, 800, 1200]:
                        if scroll_y > 0:
                            await page.evaluate(f"window.scrollTo(0, {scroll_y})")
                            await asyncio.sleep(1.5)

                        for sel in composer_selectors:
                            composer = page.locator(sel).first
                            try:
                                if await composer.count() > 0 and await composer.is_visible():
                                    await composer.scroll_into_view_if_needed(timeout=3000)
                                    await asyncio.sleep(0.5)
                                    await composer.click(timeout=5000)
                                    
                                    # Wait to see if dialog actually opens
                                    try:
                                        await page.wait_for_selector("div[role='dialog']", state="visible", timeout=4000)
                                        composer_found = True
                                    except Exception:
                                        # Dialog didn't open. Likely a profile switch page refresh occurred.
                                        logger.warning("   ⚠️ Dialog didn't open. Possible identity switch refresh. Retrying...")
                                        await asyncio.sleep(4) # Wait for page reload
                                        
                                        composer_retry = page.locator(sel).first
                                        if await composer_retry.count() > 0 and await composer_retry.is_visible():
                                            await composer_retry.scroll_into_view_if_needed(timeout=3000)
                                            await asyncio.sleep(0.5)
                                            await composer_retry.click(timeout=5000)
                                            try:
                                                await page.wait_for_selector("div[role='dialog']", state="visible", timeout=4000)
                                                composer_found = True
                                            except Exception:
                                                pass
                                    
                                    if composer_found:
                                        logger.info("   ✅ Opened composer popup (scroll=%d): %s", scroll_y, sel)
                                        break
                            except Exception:
                                continue

                        if composer_found:
                            break

                    if not composer_found:
                        logger.warning("   ⚠️ No composer found on page %s after scrolling, trying direct URL", page_name)
                        try:
                            await page.goto(f"{page_url}&sk=composer", wait_until="domcontentloaded", timeout=15000)
                        except Exception:
                            pass
                        await asyncio.sleep(3)
                        for sel in composer_selectors:
                            composer = page.locator(sel).first
                            try:
                                if await composer.count() > 0:
                                    await composer.click(timeout=5000)
                                    try:
                                        await page.wait_for_selector("div[role='dialog']", state="visible", timeout=4000)
                                        composer_found = True
                                        logger.info("   ✅ Opened composer popup (direct URL): %s", sel)
                                        break
                                    except Exception:
                                        pass
                            except Exception:
                                continue

                if not composer_found:
                    results.append({
                        "page": page_name,
                        "success": False,
                        "error": "Composer not found",
                        "upload_trace": upload_tracker["events"],
                    })
                    logger.error("   ❌ Could not find composer for %s", page_name)
                    continue

                await asyncio.sleep(2)

                # Screenshot the open composer
                ss_composer = ARTIFACT_DIR / f"06_composer_{page_id}_{timestamp}.png"
                await page.screenshot(path=str(ss_composer))

                # Upload the image. Prefer existing inputs so the native
                # GTK file chooser does not remain open over the browser.
                upload_method = await upload_image_without_stale_file_dialog(page, IMAGE_PATH)
                if upload_method:
                    logger.info("   ✅ Image file set via %s", upload_method)
                else:
                    logger.warning("   ⚠️ No image file input or file chooser found")

                await asyncio.sleep(3)
                if upload_tracker.get("last_error"):
                    logger.warning("   ⚠️ Upload endpoint reported: %s", upload_tracker["last_error"])

                # Screenshot after image attached
                ss_attached = ARTIFACT_DIR / f"07_image_attached_{page_id}_{timestamp}.png"
                await page.screenshot(path=str(ss_attached))

                # Type the caption
                caption = f"Live automation test {timestamp} — posted via Smart Poster 🚀"
                textbox = page.locator(
                    "div[role='dialog'] [contenteditable='true'][role='textbox']"
                ).first
                if await textbox.count() > 0:
                    await textbox.click(timeout=3000)
                    await asyncio.sleep(0.3)
                    await textbox.fill(caption)
                    logger.info("   ✅ Caption typed")
                else:
                    logger.warning("   ⚠️ Textbox not found for caption")

                await asyncio.sleep(1)

                # Screenshot before posting
                ss_ready = ARTIFACT_DIR / f"08_ready_to_post_{page_id}_{timestamp}.png"
                await page.screenshot(path=str(ss_ready))

                # Click the exact publish flow. Managed pages commonly show
                # "Next" first, then the real "Post" button.
                posted, publish_flow = await click_publish_flow(page)
                if not posted:
                    logger.warning("   ⚠️ %s", publish_flow)

                # Wait for post to complete or for Facebook/upload to report a failure.
                error_text = ""
                dialog_still_open = True
                if posted:
                    deadline = time.monotonic() + 45
                    while time.monotonic() < deadline:
                        interstitial_action = await handle_post_submit_interstitial(page)
                        if interstitial_action:
                            publish_flow = f"{publish_flow} -> {interstitial_action}"
                            logger.info("   ✅ Clicked post-submit prompt: %s", interstitial_action)
                            await asyncio.sleep(2)
                            continue
                        error_text = await visible_dialog_error(page)
                        if not error_text and upload_tracker.get("last_error"):
                            error_text = f"Upload endpoint error: {upload_tracker['last_error']}"
                        dialog_still_open = await page.locator("div[role='dialog']:has-text('Create post')").count() > 0
                        if error_text or not dialog_still_open:
                            break
                        await asyncio.sleep(1)
                else:
                    error_text = publish_flow
                    dialog_still_open = await page.locator("div[role='dialog']:has-text('Create post')").count() > 0

                ss_after = ARTIFACT_DIR / f"09_after_post_{page_id}_{timestamp}.png"
                await page.screenshot(path=str(ss_after))

                if not dialog_still_open and not error_text:
                    results.append({
                        "page": page_name,
                        "success": True,
                        "error": "",
                        "publish_flow": publish_flow,
                        "upload_trace": upload_tracker["events"],
                    })
                    logger.info("   ✅ POST SUCCESSFUL for %s!", page_name)
                elif error_text:
                    results.append({
                        "page": page_name,
                        "success": False,
                        "error": error_text,
                        "publish_flow": publish_flow,
                        "upload_trace": upload_tracker["events"],
                    })
                    logger.error("   ❌ POST FAILED for %s: %s", page_name, error_text)
                else:
                    results.append({
                        "page": page_name,
                        "success": False,
                        "error": "Dialog still open after 45s",
                        "publish_flow": publish_flow,
                        "upload_trace": upload_tracker["events"],
                    })
                    logger.warning("   ⚠️ POST UNCERTAIN for %s (dialog still open)", page_name)

                # Close dialog if still open
                if dialog_still_open:
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(1)

            except Exception as exc:
                results.append({
                    "page": page_name,
                    "success": False,
                    "error": str(exc)[:200],
                    "upload_trace": upload_tracker["events"],
                })
                logger.error("   ❌ EXCEPTION for %s: %s", page_name, exc)
                # Try to recover
                try:
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(1)
                except Exception:
                    pass
            finally:
                detach_upload_tracker()

        # ─── Final Report ─────────────────────────────────────────────
        logger.info("=" * 60)
        logger.info("  📊 FINAL RESULTS")
        logger.info("=" * 60)
        success_count = sum(1 for r in results if r["success"])
        fail_count = len(results) - success_count
        for r in results:
            status = "✅" if r["success"] else "❌"
            logger.info("  %s %s — %s", status, r["page"], r.get("error") or "OK")
        logger.info("─" * 60)
        logger.info("  Total: %d | Success: %d | Failed: %d", len(results), success_count, fail_count)
        logger.info("=" * 60)

        # Save results
        result_path = ARTIFACT_DIR / f"results_{timestamp}.json"
        result_path.write_text(json.dumps({
            "timestamp": timestamp,
            "pages": all_pages,
            "results": results,
            "tokens_available": {
                "fb_dtsg": bool(tokens.get("fb_dtsg")),
                "lsd": bool(tokens.get("lsd")),
                "user_id": tokens.get("user_id", ""),
            },
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Results saved: %s", result_path)

        # Keep browser open for 10 seconds so user can see
        logger.info("Browser staying open for 10 seconds...")
        await asyncio.sleep(10)

        await browser.close()
        logger.info("Done!")


if __name__ == "__main__":
    asyncio.run(run_test())
