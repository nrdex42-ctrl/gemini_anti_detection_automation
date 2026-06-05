"""Smart posting strategy that routes around upload.facebook.com restrictions.

When Facebook's upload service (upload.facebook.com) blocks image uploads for a
session/account (error 1366046 / NotAuthorizedError), this module provides
alternative posting strategies:

1. **Text-with-link fallback** — Post the caption as text, appending the image
   URL if available.  This bypasses upload.facebook.com entirely and goes through
   the GraphQL ``ComposerStoryCreateMutation`` endpoint which the session *can*
   still use.

2. **URL-paste composer** — Paste a publicly-accessible image URL into the
   composer so Facebook fetches the image server-side.

Both strategies avoid the upload service authorization gate that is rejecting
the current session.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment controls
# ---------------------------------------------------------------------------

_IMAGE_UPLOAD_STRATEGY_ENV = "FB_IMAGE_UPLOAD_STRATEGY"

# Possible values:
#   "upload_first"  — try rupload/browser upload first, fall back to URL/text
#   "url_first"     — try URL-paste first, fall back to upload
#   "text_only"     — always degrade image posts to text + link
#   "auto"          — (default) try upload, if blocked use URL, then text
STRATEGY_UPLOAD_FIRST = "upload_first"
STRATEGY_URL_FIRST = "url_first"
STRATEGY_TEXT_ONLY = "text_only"
STRATEGY_AUTO = "auto"

VALID_STRATEGIES = {
    STRATEGY_UPLOAD_FIRST,
    STRATEGY_URL_FIRST,
    STRATEGY_TEXT_ONLY,
    STRATEGY_AUTO,
}


def get_image_upload_strategy() -> str:
    """Return the currently configured image upload strategy."""
    raw = os.getenv(_IMAGE_UPLOAD_STRATEGY_ENV, STRATEGY_AUTO).strip().lower()
    if raw not in VALID_STRATEGIES:
        logger.warning(
            "Unknown %s=%r; defaulting to %r",
            _IMAGE_UPLOAD_STRATEGY_ENV,
            raw,
            STRATEGY_AUTO,
        )
        return STRATEGY_AUTO
    return raw


# ---------------------------------------------------------------------------
# Upload restriction tracking  (in-memory; resets on process restart)
# ---------------------------------------------------------------------------

# Accounts that have been flagged by upload.facebook.com during this process.
# Key: account_id, Value: timestamp of first observed failure
_upload_blocked_accounts: Dict[str, float] = {}

# Cooldown before retrying upload for a blocked account (seconds).
UPLOAD_BLOCK_COOLDOWN_SECONDS = int(
    os.getenv("FB_UPLOAD_BLOCK_COOLDOWN_SECONDS", "3600")
)


def mark_upload_blocked(account_id: str) -> None:
    """Record that *account_id* is blocked by the upload service."""
    _upload_blocked_accounts.setdefault(account_id, time.time())
    logger.warning(
        "Account %s marked as blocked by upload.facebook.com. "
        "Image posts will use fallback strategies for %d seconds.",
        account_id,
        UPLOAD_BLOCK_COOLDOWN_SECONDS,
    )


def is_upload_blocked(account_id: str) -> bool:
    """Return True if the account is currently considered blocked."""
    blocked_at = _upload_blocked_accounts.get(account_id)
    if blocked_at is None:
        return False
    if time.time() - blocked_at > UPLOAD_BLOCK_COOLDOWN_SECONDS:
        # Cooldown expired — allow a retry.
        _upload_blocked_accounts.pop(account_id, None)
        logger.info(
            "Upload block cooldown expired for account %s; will retry upload.",
            account_id,
        )
        return False
    return True


def clear_upload_block(account_id: str) -> None:
    """Clear the upload block flag (e.g. after a successful upload)."""
    _upload_blocked_accounts.pop(account_id, None)


# ---------------------------------------------------------------------------
# Upload-failure detection helpers
# ---------------------------------------------------------------------------

_UPLOAD_BLOCK_SIGNATURES = (
    "1366046",
    "NotAuthorizedError",
    "not authorized",
    "Can't read files",
    "couldn't be uploaded",
    "HTTP_IMAGE_UPLOAD_FAILED",
    "RUPLOAD_FAILED",
    "upload_failed",
)


def looks_like_upload_block(error_text: str) -> bool:
    """Heuristic: does *error_text* look like an upload authorization failure?"""
    lowered = error_text.lower()
    return any(sig.lower() in lowered for sig in _UPLOAD_BLOCK_SIGNATURES)


# ---------------------------------------------------------------------------
# Text-with-link fallback
# ---------------------------------------------------------------------------

def build_text_fallback_caption(
    caption: str,
    media_url: str = "",
    *,
    include_url: bool = True,
) -> str:
    """Build a text-only caption that optionally includes the image URL.

    If *media_url* is a public HTTP URL it is appended after the caption so
    Facebook can render a link preview.  Local file paths are mentioned by
    filename only.
    """
    parts = [caption.strip()]
    if include_url and media_url:
        if media_url.startswith(("http://", "https://")):
            parts.append(f"\n\n📷 {media_url}")
        else:
            basename = Path(media_url).name
            parts.append(f"\n\n📷 [Image: {basename}]")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Smart routing
# ---------------------------------------------------------------------------

async def smart_image_post(
    post: Dict[str, Any],
    tokens: Dict[str, Any],
    cookies_json: str,
    identity: Any,
    *,
    http_upload_fn: Optional[Callable[..., Any]] = None,
    graphql_text_fn: Optional[Callable[..., Any]] = None,
    browser_upload_fn: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Route an image post through the best available strategy.

    Parameters
    ----------
    post : dict
        Must contain at minimum ``page_id_or_url``, ``page_name``, ``caption``,
        ``media_url``.
    tokens : dict
        Facebook session tokens (fb_dtsg, lsd, user_id, …).
    cookies_json : str
        JSON-encoded browser cookies.
    identity : IdentityContext
        Browser identity context for the current session.
    http_upload_fn : callable, optional
        ``attempt_private_http_image_post`` or equivalent.
    graphql_text_fn : callable, optional
        A function that posts text via GraphQL.  Signature:
        ``(post, tokens, cookies_json, identity) -> result_dict``
    browser_upload_fn : callable, optional
        Browser-based image upload fallback.

    Returns
    -------
    dict
        Normalized result with ``success``, ``status``, ``transport``, etc.
    """
    strategy = get_image_upload_strategy()
    account_id = getattr(identity, "account_id", "unknown")
    page_name = post.get("page_name") or post.get("page_id_or_url") or "unknown"
    media_url = str(post.get("media_url") or "")
    caption = str(post.get("caption") or "")
    started = time.monotonic()

    logger.info(
        "SmartPoster: strategy=%s account=%s page=%s blocked=%s",
        strategy,
        account_id,
        page_name,
        is_upload_blocked(account_id),
    )

    # ------------------------------------------------------------------
    # Strategy: text_only — always degrade to text
    # ------------------------------------------------------------------
    if strategy == STRATEGY_TEXT_ONLY:
        return await _post_as_text(
            post, tokens, cookies_json, identity,
            graphql_text_fn=graphql_text_fn,
            reason="STRATEGY_TEXT_ONLY",
        )

    # ------------------------------------------------------------------
    # Strategy: auto — skip upload if account is known-blocked
    # ------------------------------------------------------------------
    upload_allowed = True
    if strategy == STRATEGY_AUTO and is_upload_blocked(account_id):
        upload_allowed = False
        logger.info(
            "SmartPoster: skipping upload for %s (account blocked, cooldown active)",
            page_name,
        )

    # ------------------------------------------------------------------
    # Try upload (if allowed by strategy)
    # ------------------------------------------------------------------
    if upload_allowed and strategy in (STRATEGY_UPLOAD_FIRST, STRATEGY_AUTO):
        result = await _try_upload(
            post, tokens, cookies_json, identity,
            http_upload_fn=http_upload_fn,
            browser_upload_fn=browser_upload_fn,
        )
        if result and result.get("success"):
            clear_upload_block(account_id)
            return result
        # Check if the failure looks like an upload block.
        error_text = str(result.get("status") or "") if result else ""
        if looks_like_upload_block(error_text):
            mark_upload_blocked(account_id)
            logger.warning(
                "SmartPoster: upload blocked for %s, falling back to text. Error: %s",
                page_name,
                error_text[:200],
            )

    # ------------------------------------------------------------------
    # Fallback: URL-paste (if media_url is a public URL)
    # ------------------------------------------------------------------
    if media_url.startswith(("http://", "https://")):
        url_result = await _post_as_text(
            post, tokens, cookies_json, identity,
            graphql_text_fn=graphql_text_fn,
            reason="URL_PASTE_FALLBACK",
            include_url=True,
        )
        if url_result.get("success"):
            return url_result

    # ------------------------------------------------------------------
    # Final fallback: text-only post
    # ------------------------------------------------------------------
    return await _post_as_text(
        post, tokens, cookies_json, identity,
        graphql_text_fn=graphql_text_fn,
        reason="TEXT_FALLBACK_AFTER_UPLOAD_BLOCK",
        include_url=True,
    )


async def _try_upload(
    post: Dict[str, Any],
    tokens: Dict[str, Any],
    cookies_json: str,
    identity: Any,
    *,
    http_upload_fn: Optional[Callable[..., Any]] = None,
    browser_upload_fn: Optional[Callable[..., Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Attempt image upload via HTTP and/or browser."""
    if http_upload_fn is not None:
        try:
            result = await http_upload_fn(post, tokens, cookies_json, identity)
            if isinstance(result, dict) and result.get("success"):
                return result
            return result  # return failure for inspection
        except Exception as exc:
            logger.warning("SmartPoster: HTTP upload raised: %s", exc)

    if browser_upload_fn is not None:
        try:
            result = await browser_upload_fn(post, tokens, cookies_json, identity)
            if isinstance(result, dict) and result.get("success"):
                return result
            return result
        except Exception as exc:
            logger.warning("SmartPoster: browser upload raised: %s", exc)

    return None


async def _post_as_text(
    post: Dict[str, Any],
    tokens: Dict[str, Any],
    cookies_json: str,
    identity: Any,
    *,
    graphql_text_fn: Optional[Callable[..., Any]] = None,
    reason: str = "TEXT_FALLBACK",
    include_url: bool = True,
) -> Dict[str, Any]:
    """Post image content as a text post (bypasses upload.facebook.com)."""
    from .graphql_poster import HardenedGraphQLPoster
    from .config import AppConfig
    from .network import ProxyManager
    from .tokens import TokenVault
    from .utils import cookies_json_to_header, extract_page_id

    caption = str(post.get("caption") or "")
    media_url = str(post.get("media_url") or "")
    page_name = post.get("page_name") or post.get("page_id_or_url") or "unknown"
    page_id_raw = str(post.get("page_id_or_url") or "")
    page_id = extract_page_id(page_id_raw)

    fallback_caption = build_text_fallback_caption(
        caption, media_url, include_url=include_url
    )

    if graphql_text_fn is not None:
        text_post = dict(post)
        text_post["caption"] = fallback_caption
        text_post["post_type"] = "text"
        text_post["media_url"] = ""
        try:
            result = await graphql_text_fn(text_post, tokens, cookies_json, identity)
            if isinstance(result, dict):
                result["transport"] = f"text_fallback ({reason})"
                result["original_post_type"] = "image"
                result["fallback_reason"] = reason
                return result
        except Exception as exc:
            logger.warning("SmartPoster: graphql_text_fn raised: %s", exc)

    # Direct GraphQL text post (no external function needed)
    if not page_id:
        return {
            "page_id_or_url": page_id_raw,
            "page_name": page_name,
            "post_type": "image",
            "media_url": media_url,
            "success": False,
            "status": f"TEXT_FALLBACK_PAGE_ID_MISSING ({reason})",
            "post_id": None,
            "transport": f"text_fallback ({reason})",
            "original_post_type": "image",
            "fallback_reason": reason,
        }

    try:
        from dataclasses import replace as dc_replace

        cookie_header = (
            str(tokens.get("cookie_header") or "").strip()
            or cookies_json_to_header(cookies_json)
        )
        token_payload = dict(tokens)
        token_payload["cookie_header"] = cookie_header
        token_payload["timestamp"] = time.time()

        token_vault = TokenVault(None)
        await token_vault.set(identity.account_id, token_payload)

        active_identity = dc_replace(
            identity,
            facebook_user_id=str(
                token_payload.get("user_id")
                or identity.facebook_user_id
                or ""
            ),
        )
        config = AppConfig(enable_private_facebook_http=True, proxy_pool=[])
        proxy_manager = ProxyManager([], None, require_proxy=False)

        poster = HardenedGraphQLPoster(
            token_vault, active_identity, None, proxy_manager, config
        )
        post_ok, status, post_id = await poster.post_to_page(
            page_id,
            fallback_caption,
            media_fbid=None,  # No image attachment — text only
            cookie_header=cookie_header,
        )

        return {
            "page_id_or_url": page_id_raw,
            "page_name": page_name,
            "post_type": "image",
            "media_url": media_url,
            "success": post_ok,
            "status": status if post_ok else f"TEXT_FALLBACK_FAILED: {status}",
            "post_id": post_id,
            "transport": f"text_fallback ({reason})",
            "original_post_type": "image",
            "fallback_reason": reason,
        }
    except Exception as exc:
        logger.error(
            "SmartPoster: text fallback failed for %s: %s", page_name, exc
        )
        return {
            "page_id_or_url": page_id_raw,
            "page_name": page_name,
            "post_type": "image",
            "media_url": media_url,
            "success": False,
            "status": f"TEXT_FALLBACK_EXCEPTION ({reason}): {str(exc)[:200]}",
            "post_id": None,
            "transport": f"text_fallback ({reason})",
            "original_post_type": "image",
            "fallback_reason": reason,
        }
