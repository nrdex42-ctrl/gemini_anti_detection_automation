"""Live emulation script to post to all pages using the provided cookies."""

import asyncio
import io
import json
import logging
import sys
import os
import subprocess
import struct
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Ensure the renamed project directory wins over any unrelated fb_automation
# folders that may exist under /home/shabana/Public.
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(CURRENT_DIR)
sys.path.insert(0, CURRENT_DIR)
sys.path.insert(1, PARENT_DIR)

from fb_automation.browser_stealth import BrowserStealth, StealthConfig
from fb_automation.identity import IdentityContext
from fb_automation.smart_poster import (
    smart_image_post,
    looks_like_upload_block,
    mark_upload_blocked,
    is_upload_blocked,
    get_image_upload_strategy,
    STRATEGY_TEXT_ONLY,
)
from fb_automation.utils import cookies_json_to_header, extract_page_id

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

COOKIE_ENV_VARS = ("FB_RAW_COOKIE_STRING", "FB_COOKIE_STRING")
DEFAULT_ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts" / "live_emulation"
CUSTOM_IMAGE_ENV_VAR = "LIVE_EMULATION_IMAGE_PATH"
SUPPORTED_POST_TYPES = {"image", "video"}
SUPPORTED_IMAGE_FORMATS = {"jpg", "jpeg", "png"}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def private_http_image_first_enabled() -> bool:
    return _env_bool("LIVE_EMULATION_HTTP_IMAGE_FIRST", True)


def load_raw_cookie_string() -> str:
    for name in COOKIE_ENV_VARS:
        raw = os.getenv(name, "").strip()
        if raw:
            return raw
    raise RuntimeError(
        "Set FB_RAW_COOKIE_STRING (or FB_COOKIE_STRING) to the refreshed Facebook cookie string before running."
    )

def parse_cookies(cookie_string: str) -> List[Dict[str, str]]:
    cookies = []
    for pair in cookie_string.split(";"):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        cookies.append({
            "name": name,
            "value": value,
            "domain": ".facebook.com",
            "path": "/"
        })
    return cookies


def requested_post_types() -> List[str]:
    raw = os.getenv("LIVE_EMULATION_POST_TYPES", "image,video")
    requested = [
        item.strip().lower()
        for item in raw.split(",")
        if item.strip().lower() in SUPPORTED_POST_TYPES
    ]
    return requested or ["image", "video"]


def live_image_format() -> str:
    requested = os.getenv("LIVE_EMULATION_IMAGE_FORMAT", "png").strip().lower()
    if requested not in SUPPORTED_IMAGE_FORMATS:
        logger.warning(
            "Unsupported LIVE_EMULATION_IMAGE_FORMAT=%r; using png.",
            requested,
        )
        return "png"
    return "jpg" if requested == "jpeg" else requested


def custom_image_source_path() -> Optional[Path]:
    raw = os.getenv(CUSTOM_IMAGE_ENV_VAR, "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"{CUSTOM_IMAGE_ENV_VAR} does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"{CUSTOM_IMAGE_ENV_VAR} is not a file: {path}")
    return path


def save_facebook_safe_jpeg(source_path: Path, target_path: Path) -> Dict[str, Any]:
    from PIL import Image, ImageOps

    with Image.open(source_path) as original:
        original_format = original.format or ""
        image = ImageOps.exif_transpose(original)
        if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
            rgba = image.convert("RGBA")
            flattened = Image.new("RGB", rgba.size, (255, 255, 255))
            flattened.paste(rgba, mask=rgba.getchannel("A"))
            image = flattened
        elif image.mode != "RGB":
            image = image.convert("RGB")
        else:
            image = image.copy()

    max_dimension = 4096
    if image.width > max_dimension or image.height > max_dimension:
        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
        image.thumbnail((max_dimension, max_dimension), resampling)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    quality = 92
    while True:
        image.save(
            target_path,
            "JPEG",
            quality=quality,
            subsampling=2,
            progressive=False,
            optimize=False,
        )
        if target_path.stat().st_size <= 9 * 1024 * 1024 or quality <= 60:
            break
        quality -= 5

    data = target_path.read_bytes()
    structure_error = facebook_jpeg_structure_error(data)
    if structure_error:
        raise ValueError(f"Sanitized JPEG is structurally invalid: {structure_error}: {target_path}")

    with Image.open(io.BytesIO(data)) as verify_image:
        verify_image.load()
        verify_image.getpixel((0, 0))

    return {
        "source_format": original_format,
        "output_format": "JPEG",
        "width": image.width,
        "height": image.height,
        "quality": quality,
        "size_bytes": target_path.stat().st_size,
    }


def facebook_jpeg_structure_error(data: bytes) -> str:
    if len(data) < 4:
        return "file too small"
    if data[:2] != b"\xff\xd8":
        return "missing SOI marker"
    if data[-2:] != b"\xff\xd9":
        return "missing EOI marker"

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
            return f"progressive JPEG marker FF{marker:02X} detected"
        elif marker == 0xDA:
            has_sos = True
            break
        if pos + 3 >= len(data):
            return f"truncated marker FF{marker:02X}"
        segment_length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if segment_length < 2:
            return f"invalid segment length at marker FF{marker:02X}"
        pos += 2 + segment_length

    if not has_sof:
        return "missing baseline SOF marker"
    if not has_sos:
        return "missing SOS marker"
    if not has_dqt:
        return "missing DQT marker"
    return ""


def build_live_artifacts(artifact_dir: Path = DEFAULT_ARTIFACT_DIR) -> Dict[str, Any]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    custom_image_path = custom_image_source_path()
    image_metadata: Dict[str, Any] = {}

    if custom_image_path:
        image_path = artifact_dir / f"live_image_{timestamp}.jpg"
        image_metadata = save_facebook_safe_jpeg(custom_image_path, image_path)
    else:
        image_format = live_image_format()
        image_path = artifact_dir / f"live_image_{timestamp}.{image_format}"

    video_path = artifact_dir / f"live_video_{timestamp}.mp4"

    from PIL import Image, ImageDraw

    if not custom_image_path:
        width = 1080
        height = 1080
        image = Image.new("RGB", (width, height), (38, 62, 82))
        draw = ImageDraw.Draw(image)
        for y in range(height):
            ratio = y / max(1, height - 1)
            color = (
                int(34 + ratio * 48),
                int(58 + ratio * 76),
                int(82 + ratio * 52),
            )
            draw.line((0, y, width, y), fill=color)
        noise = Image.effect_noise((width, height), 18).convert("RGB")
        image = Image.blend(image, noise, 0.08)
        draw = ImageDraw.Draw(image)
        draw.rectangle((88, 88, 992, 992), outline=(232, 218, 168), width=5)
        draw.rectangle((128, 128, 952, 952), outline=(90, 170, 145), width=3)
        draw.ellipse((690, 170, 900, 380), fill=(90, 170, 145), outline=(245, 242, 225), width=4)
        draw.rectangle((180, 690, 900, 800), fill=(28, 45, 60), outline=(245, 242, 225), width=2)
        draw.text((210, 720), "Live Automation Image Artifact", fill=(245, 242, 225))
        draw.text((210, 760), f"Generated {timestamp}", fill=(232, 218, 168))
        if image_format == "png":
            image.save(image_path, "PNG", optimize=True)
        else:
            image.save(image_path, "JPEG", quality=90, subsampling=2, progressive=False)

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
            "format=yuv420p",
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

    return {
        "image_path": str(image_path),
        "video_path": str(video_path),
        "timestamp": timestamp,
        "source_image_path": str(custom_image_path) if custom_image_path else "",
        "image_sanitized": bool(custom_image_path),
        "image_metadata": image_metadata,
    }


def normalize_live_post_result(
    post: Dict[str, Any],
    result: Any,
    *,
    transport: str,
    prefix_status: str = "",
) -> Dict[str, Any]:
    page_id = post["page_id_or_url"]
    if isinstance(result, dict):
        success = bool(result.get("success"))
        status = str(result.get("result") or result.get("status") or "")
        post_id = result.get("post_id")
    else:
        success = bool(getattr(result, "success", False))
        status = str(getattr(result, "result", getattr(result, "status", "")))
        post_id = getattr(result, "post_id", None)
    if prefix_status and not success:
        status = f"{prefix_status}; Browser fallback: {status}"
    return {
        "page_id_or_url": page_id,
        "page_name": post.get("page_name", ""),
        "post_type": post.get("post_type", ""),
        "media_url": post.get("media_url", ""),
        "success": success,
        "status": status,
        "post_id": post_id,
        "transport": transport,
    }


async def attempt_private_http_image_post(
    post: Dict[str, Any],
    tokens: Dict[str, Any],
    cookies_json: str,
    identity: IdentityContext,
    *,
    uploader_factory: Optional[Callable[..., Any]] = None,
    poster_factory: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    from fb_automation.config import AppConfig
    from fb_automation.graphql_poster import HardenedGraphQLPoster
    from fb_automation.network import ProxyManager
    from fb_automation.rupload import HardenedRupload
    from fb_automation.tokens import TokenVault

    started = time.monotonic()
    page_id = extract_page_id(str(post.get("page_id_or_url") or ""))
    if not page_id:
        return normalize_live_post_result(
            post,
            {"success": False, "status": "HTTP_IMAGE_PAGE_ID_MISSING", "post_id": None},
            transport="private_http_image",
        )

    try:
        cookie_header = str(tokens.get("cookie_header") or "").strip() or cookies_json_to_header(cookies_json)
        token_payload = dict(tokens)
        token_payload["cookie_header"] = cookie_header
        token_payload["timestamp"] = time.time()
        token_vault = TokenVault(None)
        await token_vault.set(identity.account_id, token_payload)

        active_identity = replace(
            identity,
            facebook_user_id=str(token_payload.get("user_id") or identity.facebook_user_id or ""),
        )
        config = AppConfig(enable_private_facebook_http=True, proxy_pool=[])
        proxy_manager = ProxyManager([], None, require_proxy=False)
        redis_client = None

        uploader_cls = uploader_factory or HardenedRupload
        uploader = uploader_cls(token_vault, active_identity, redis_client, proxy_manager, config)
        upload_ok, media_fbid, upload_detail = await uploader.upload_image(str(post.get("media_url") or ""))
        if not upload_ok or not media_fbid:
            return normalize_live_post_result(
                post,
                {
                    "success": False,
                    "status": f"HTTP_IMAGE_UPLOAD_FAILED: {str(upload_detail)[:300]}",
                    "post_id": None,
                },
                transport="private_http_image",
            )

        poster_cls = poster_factory or HardenedGraphQLPoster
        poster = poster_cls(token_vault, active_identity, redis_client, proxy_manager, config)
        post_ok, status, post_id = await poster.post_to_page(
            page_id,
            str(post.get("caption") or ""),
            str(media_fbid),
            cookie_header=cookie_header,
        )
        return normalize_live_post_result(
            post,
            {
                "success": post_ok,
                "status": status if post_ok else f"HTTP_IMAGE_GRAPHQL_FAILED: {status}",
                "post_id": post_id,
            },
            transport="private_http_image",
        )
    except Exception as exc:
        return normalize_live_post_result(
            post,
            {
                "success": False,
                "status": f"HTTP_IMAGE_EXCEPTION: {str(exc)[:300]}",
                "post_id": None,
            },
            transport="private_http_image",
        )
    finally:
        logger.info(
            "HTTP image path finished for %s in %.1fs",
            post.get("page_name") or post.get("page_id_or_url") or "unknown",
            time.monotonic() - started,
        )


async def extract_tokens_and_pages(cookies: List[Dict[str, str]], identity: IdentityContext) -> Tuple[Dict[str, Any], List[str]]:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser_executable = os.getenv("FACEBOOK_BROWSER_EXECUTABLE", "").strip()
        launch_options: Dict[str, Any] = {
            "headless": True,
            "args": [
                '--disable-blink-features=AutomationControlled',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--headless=new',
            ],
        }
        if browser_executable:
            executable_path = Path(browser_executable).expanduser()
            if executable_path.exists():
                launch_options["executable_path"] = str(executable_path)
                logger.info(f"Using configured browser executable for token extraction: {executable_path}")
            else:
                logger.warning(f"FACEBOOK_BROWSER_EXECUTABLE does not exist for token extraction: {executable_path}")
        browser = await pw.chromium.launch(**launch_options)
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 720},
            user_agent=identity.user_agent
        )
        
        stealth = BrowserStealth(StealthConfig(
            webgl_vendor=identity.webgl_vendor,
            webgl_renderer=identity.webgl_renderer
        ))
        await stealth.apply_to_context(context)
        await context.add_cookies(cookies)
        
        page = await context.new_page()
        
        logger.info("Navigating to Facebook to extract tokens...")
        await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
        
        # Add a small delay to ensure React hydrates
        await asyncio.sleep(3)
        
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
        
        try:
            tokens["cookie_header"] = cookies_json_to_header(json.dumps(cookies, ensure_ascii=False))
        except Exception as exc:
            logger.warning("Could not derive cookie header from cookies JSON: %s", exc)
        logger.info(
            "Extracted tokens: fb_dtsg=%s lsd=%s user_id=%s revision=%s",
            "present" if tokens.get("fb_dtsg") else "missing",
            "present" if tokens.get("lsd") else "missing",
            tokens.get("user_id") or "missing",
            tokens.get("revision") or "missing",
        )
        
        logger.info("Navigating to pages directory to find managed pages...")
        await page.goto("https://www.facebook.com/pages/?category=your_pages", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(2)
        
        # We will extract page IDs by looking at the page source for "page_id":"12345"
        # Since the DOM can be highly obfuscated, regexing the page content is often the most reliable for ID extraction.
        page_content = await page.content()
        import re
        
        # Looking for things like profile_id, page_id, or hovering links
        # Facebook's relay state often contains `pageID` or `page_id`
        matches = re.findall(r'"pageID":"(\d+)"', page_content)
        matches += re.findall(r'"page_id":"(\d+)"', page_content)
        
        # De-duplicate
        page_ids = list(set(matches))
        logger.info(f"Extracted Page IDs from source: {page_ids}")
        
        if not page_ids:
            logger.warning("Could not find page IDs. Facebook's UI may have changed or the account manages no pages.")
            
        await context.close()
        await browser.close()
        return tokens, page_ids

async def run_live_emulation():
    print("="*60)
    print(" 🚀 LIVE EMULATION: POSTING TO ALL PAGES 🚀")
    print("="*60)
    os.environ['POST_COOKIE_MIN_INTERVAL_SECONDS'] = '0'
    if not custom_image_source_path() and live_image_format() == "png":
        os.environ.setdefault('FACEBOOK_IMAGE_PREFER_JPEG_UPLOAD', 'false')
    
    identity = IdentityContext(
        account_id="live_demo_account",
        proxy_url="", # Direct connection for now unless you have a proxy
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        chrome_version="126.0.0.0",
        webgl_vendor="Google Inc. (NVIDIA)",
        webgl_renderer="ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 Direct3D11 vs_5_0 ps_5_0, D3D11)"
    )
    
    raw_cookie_string = load_raw_cookie_string()
    cookies = parse_cookies(raw_cookie_string)
    logger.info("Parsed raw cookie string into JSON format.")
    cookies_json = json.dumps(cookies, ensure_ascii=False)

    tokens, page_ids = await extract_tokens_and_pages(cookies, identity)

    if not tokens.get('fb_dtsg'):
        logger.error("Failed to extract fb_dtsg token. Cookies might be invalid or expired.")
        return
    
    if not page_ids:
        # Fallback: The account might be a New Pages Experience profile
        fallback_id = tokens.get('user_id')
        if fallback_id:
            logger.info(f"No secondary pages found. Falling back to the primary profile/page ID: {fallback_id}")
            page_ids = [fallback_id]
        else:
            logger.error("No pages found and no user_id available. We cannot post without a target Page ID.")
            return

    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from playwright_engine import create_facebook_posts, discover_facebook_pages

    discovery_ok, discovered_pages, discovery_detail = await discover_facebook_pages(cookies_json)
    if discovery_ok and discovered_pages:
        logger.info(f"Discovered {len(discovered_pages)} managed page(s) via browser discovery.")
        page_targets = [
            {
                "page_id_or_url": str(item.get("url") or item.get("id") or "").strip(),
                "page_name": str(item.get("name") or item.get("url") or item.get("id") or "").strip(),
            }
            for item in discovered_pages
            if str(item.get("url") or item.get("id") or "").strip()
        ]
    else:
        if discovery_detail:
            logger.warning(f"Browser page discovery did not return pages: {discovery_detail}")
        page_targets = [
            {
                "page_id_or_url": str(page_id),
                "page_name": str(page_id),
            }
            for page_id in page_ids
        ]

    if not page_targets:
        logger.error("No usable page targets were discovered. Cannot publish.")
        return

    artifacts = build_live_artifacts()
    logger.info(f"Generated image artifact: {artifacts['image_path']}")
    logger.info(f"Generated video artifact: {artifacts['video_path']}")
    post_types = requested_post_types()
    logger.info(f"Requested live post types: {', '.join(post_types)}")

    posts = []
    for target in page_targets:
        page_id_or_url = target["page_id_or_url"]
        page_name = target["page_name"] or page_id_or_url
        image_caption = f"Live image artifact test {artifacts['timestamp']} for {page_name}. Please ignore."
        video_caption = f"Live video artifact test {artifacts['timestamp']} for {page_name}. Please ignore."
        logger.info(f"Attempting media posts to Page target: {page_name} ({page_id_or_url})...")
        if "image" in post_types:
            posts.append({
                "page_id_or_url": page_id_or_url,
                "page_name": page_name,
                "caption": image_caption,
                "post_type": "image",
                "media_url": artifacts["image_path"],
            })
        if "video" in post_types:
            posts.append({
                "page_id_or_url": page_id_or_url,
                "page_name": page_name,
                "caption": video_caption,
                "post_type": "video",
                "media_url": artifacts["video_path"],
            })

    if not posts:
        logger.error("No posts were built. Check LIVE_EMULATION_POST_TYPES.")
        return

    normalized_results: List[Optional[Dict[str, Any]]] = [None] * len(posts)
    browser_posts: List[Dict[str, Any]] = []
    browser_post_indexes: List[int] = []
    http_image_failures: Dict[int, str] = {}
    http_image_first = private_http_image_first_enabled()
    image_strategy = get_image_upload_strategy()
    if http_image_first:
        logger.info("HTTP image-first route enabled for image posts.")
    else:
        logger.info("HTTP image-first route disabled; using browser route for image posts.")
    logger.info("Image upload strategy: %s", image_strategy)

    for index, post in enumerate(posts):
        if post.get("post_type") == "image":
            # ── Smart routing for image posts ──
            # If the account is known-blocked or strategy is text_only,
            # skip both HTTP upload and browser upload entirely.
            account_id = getattr(identity, 'account_id', 'unknown')
            if image_strategy == STRATEGY_TEXT_ONLY or is_upload_blocked(account_id):
                reason = (
                    "strategy=text_only" if image_strategy == STRATEGY_TEXT_ONLY
                    else "account upload-blocked"
                )
                logger.info(
                    "SmartPoster: routing image post for %s directly to text fallback (%s)",
                    post.get("page_name") or post.get("page_id_or_url"),
                    reason,
                )
                smart_result = await smart_image_post(
                    post, tokens, cookies_json, identity,
                    http_upload_fn=None,  # skip upload
                    browser_upload_fn=None,
                )
                normalized_results[index] = smart_result
                continue

            # Try HTTP upload first (if enabled)
            if http_image_first:
                logger.info(
                    "Attempting HTTP image route for %s (%s)",
                    post.get("page_name") or post.get("page_id_or_url"),
                    post.get("page_id_or_url"),
                )
                http_result = await attempt_private_http_image_post(post, tokens, cookies_json, identity)
                if http_result["success"]:
                    normalized_results[index] = http_result
                    logger.info(
                        "SUCCESS: image post accepted by HTTP route on Page %s. Post ID: %s",
                        post["page_id_or_url"],
                        http_result.get("post_id"),
                    )
                    continue

                failure_status = str(http_result.get("status") or "HTTP_IMAGE_FAILED")
                http_image_failures[index] = failure_status

                # ── Check if this is an upload authorization block ──
                if looks_like_upload_block(failure_status):
                    mark_upload_blocked(account_id)
                    logger.warning(
                        "Upload block detected for %s. Falling back to text-only GraphQL post. Error: %s",
                        post.get("page_name") or post.get("page_id_or_url"),
                        failure_status[:200],
                    )
                    smart_result = await smart_image_post(
                        post, tokens, cookies_json, identity,
                        http_upload_fn=None,
                        browser_upload_fn=None,
                    )
                    smart_result["http_image_status"] = failure_status
                    normalized_results[index] = smart_result
                    continue

                logger.warning(
                    "HTTP image route failed for %s; falling back to browser route. Reason: %s",
                    post.get("page_name") or post.get("page_id_or_url"),
                    failure_status,
                )

        browser_posts.append(post)
        browser_post_indexes.append(index)

    if browser_posts:
        results = await create_facebook_posts(cookies_json, browser_posts, progress_callback=None)
        for post, result, original_index in zip(browser_posts, results, browser_post_indexes):
            http_failure = http_image_failures.get(original_index, "")
            transport = "browser_after_http_image" if http_failure else "browser"
            normalized = normalize_live_post_result(
                post,
                result,
                transport=transport,
                prefix_status=f"HTTP image path failed first: {http_failure}" if http_failure else "",
            )
            if http_failure:
                normalized["http_image_status"] = http_failure

            # ── Post-browser check: did the browser also hit an upload block? ──
            if (
                post.get("post_type") == "image"
                and not normalized.get("success")
                and looks_like_upload_block(str(normalized.get("status") or ""))
            ):
                account_id = getattr(identity, 'account_id', 'unknown')
                mark_upload_blocked(account_id)
                logger.warning(
                    "Browser upload also blocked for %s. Retrying as text-only.",
                    post.get("page_name") or post.get("page_id_or_url"),
                )
                smart_result = await smart_image_post(
                    post, tokens, cookies_json, identity,
                    http_upload_fn=None,
                    browser_upload_fn=None,
                )
                smart_result["http_image_status"] = http_failure
                smart_result["browser_status"] = str(normalized.get("status") or "")
                normalized_results[original_index] = smart_result
            else:
                normalized_results[original_index] = normalized

    normalized_results = [
        item if item is not None else normalize_live_post_result(
            posts[index],
            {"success": False, "status": "POST_NOT_ATTEMPTED", "post_id": None},
            transport="none",
        )
        for index, item in enumerate(normalized_results)
    ]

    for result in normalized_results:
        page_id = result["page_id_or_url"]
        if result["success"]:
            logger.info(
                f"SUCCESS: {result.get('post_type')} post accepted on Page {page_id}. "
                f"Post ID: {result.get('post_id')} transport={result.get('transport')}"
            )
        else:
            logger.error(
                f"FAILED: {result.get('post_type')} post on Page {page_id}. "
                f"Reason: {result.get('status')} transport={result.get('transport')}"
            )

    result_path = Path(artifacts["image_path"]).with_name(f"live_result_{artifacts['timestamp']}.json")
    overall_success = all(item["success"] for item in normalized_results) if normalized_results else False
    result_path.write_text(
        json.dumps(
            {
                "artifacts": artifacts,
                "pages": page_targets,
                "post_types": post_types,
                "success": overall_success,
                "results": normalized_results,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    logger.info(f"Wrote live run result artifact: {result_path}")
            
    print("="*60)
    print(" Live emulation complete.")
    print("="*60)

if __name__ == "__main__":
    asyncio.run(run_live_emulation())
