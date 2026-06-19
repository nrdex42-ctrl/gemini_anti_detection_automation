"""Fast HTTP/GraphQL batch posting orchestration interface.

Bridges the Playwright engine routing with the HardenedGraphQLPoster and HardenedRupload
APIs. Integrates pre-flight safety checks, proxy sticky routing, token extraction, and 
fails back to Playwright if needed.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from config import AppConfig
from models import IdentityContext
from tokens import TokenVault
from network import ProxyManager
from graphql_poster import HardenedGraphQLPoster
from rupload import HardenedRupload
from identity import IdentityRegistry
from facebook_cookie_parser import parse_account_cookie_payload
from utils import extract_page_id, maybe_await

logger = logging.getLogger(__name__)


def is_fast_graphql_enabled() -> bool:
    """Return whether the private GraphQL tier is enabled by environment configuration."""
    config = AppConfig()
    return bool(config.enable_private_facebook_http)


async def _get_redis_client() -> Optional[Any]:
    """Connect to Redis and return the async client."""
    config = AppConfig()
    if not config.redis_url:
        return None
    try:
        import redis.asyncio as aioredis
        client = aioredis.from_url(config.redis_url)
        return client
    except Exception as exc:
        logger.warning(f"GraphQL Poster could not connect to Redis: {exc}")
        return None


async def _post_one_fast(
    post: Dict[str, Any],
    cookies_json: str,
    identity: IdentityContext,
    token_vault: TokenVault,
    proxy_manager: ProxyManager,
    redis_client: Any,
    config: AppConfig,
) -> Tuple[bool, str, Optional[str]]:
    """Execute a single fast GraphQL/rupload post."""
    account_id = identity.account_id
    page_id_or_url = post.get('page_id_or_url', '')
    page_id = extract_page_id(page_id_or_url) or page_id_or_url
    
    post_type = post.get('post_type', 'text')
    caption = post.get('caption', '')
    media_url = post.get('media_url', '')
    
    # Check rate limits / safety pre-flight
    from safety import SafetyGuard, SafetyStatus
    safety = SafetyGuard(redis_client, identity, token_vault)
    status, message = await safety.pre_flight_check()
    if status != SafetyStatus.CLEAR:
        return False, f"Safety check failed: {message}", None
        
    # Get tokens
    tokens = await token_vault.get(account_id)
    if not tokens or await token_vault.is_rotation_needed(account_id):
        from browser_fallback import BrowserTokenExtractor
        extractor = BrowserTokenExtractor(token_vault, identity)
        try:
            tokens = await extractor.extract_with_retry(cookies_json)
        except Exception as exc:
            return False, f"Token extraction failed: {exc}", None
            
    if not tokens:
        return False, "No valid tokens found", None
        
    media_fbid = None
    # If it is an image or video, upload first
    if post_type in ('image', 'video') and media_url:
        if post_type == 'image':
            uploader = HardenedRupload(token_vault, identity, redis_client, proxy_manager, config)
            upload_success, fbid, upload_err = await uploader.upload_image(media_url)
        elif post_type == 'video':
            uploader = HardenedRupload(token_vault, identity, redis_client, proxy_manager, config)
            upload_success, fbid, upload_err = await uploader.upload_video(media_url)
        if not upload_success:
            return False, f"{post_type.upper()} upload failed: {upload_err}", None
        media_fbid = fbid
        
    # Post via GraphQL
    poster = HardenedGraphQLPoster(token_vault, identity, redis_client, proxy_manager, config)
    success, status_code, post_id = await poster.post_to_page(
        page_id=page_id,
        caption=caption,
        media_fbid=media_fbid,
        cookie_header=tokens.get('cookie_header'),
    )
    
    # Auto-healing: if the GraphQL post failed due to token, query, doc_id, or general errors,
    # perform a dynamic browser token/doc_id extraction refresh and retry once.
    if not success and status_code not in {'RATE_LIMITED', 'PRIVATE_HTTP_DISABLED', 'IDEMPOTENCY_TIMEOUT'}:
        logger.info(f"GraphQL post failed with {status_code}. Executing dynamic token and doc_id refresh...")
        from browser_fallback import BrowserTokenExtractor
        extractor = BrowserTokenExtractor(token_vault, identity)
        try:
            tokens = await extractor.extract_with_retry(cookies_json)
            if tokens:
                logger.info("Dynamic refresh successful. Retrying GraphQL post...")
                success, status_code, post_id = await poster.post_to_page(
                    page_id=page_id,
                    caption=caption,
                    media_fbid=media_fbid,
                    cookie_header=tokens.get('cookie_header'),
                )
        except Exception as exc:
            logger.warning(f"GraphQL token refresh retry failed: {exc}")
            
    # Run post flight validation
    await safety.post_flight_validation(success, 200 if success else 400, status_code)
    
    return success, status_code, post_id


async def create_facebook_posts_fast(
    cookies_json: str,
    posts: List[Dict[str, Any]],
    progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    launch_browser_session: Optional[Any] = None,
    browser_fallback: Optional[Callable[[List[Dict[str, Any]]], Awaitable[List[Dict[str, Any]]]]] = None,
) -> List[Dict[str, Any]]:
    """
    Main asynchronous entry point to create Facebook posts fast via direct GraphQL/HTTP.
    """
    if not is_fast_graphql_enabled():
        logger.debug("Fast GraphQL tier is disabled. Falling back directly.")
        if browser_fallback:
            return await browser_fallback(posts)
        return [{'page': str(p.get('page_name') or p.get('page_id_or_url')), 'success': False, 'result': 'GraphQL posting disabled'} for p in posts]

    try:
        parsed_cookie = parse_account_cookie_payload(cookies_json)
        account_id = parsed_cookie.account_id
    except Exception as exc:
        logger.warning(f"Could not parse account cookies for GraphQL posting: {exc}")
        if browser_fallback:
            return await browser_fallback(posts)
        return [{'page': str(p.get('page_name') or p.get('page_id_or_url')), 'success': False, 'result': f'Cookie parse failure: {exc}'} for p in posts]

    redis_client = await _get_redis_client()
    if not redis_client:
        logger.warning("Redis client unavailable; fallback to browser.")
        if browser_fallback:
            return await browser_fallback(posts)
        return [{'page': str(p.get('page_name') or p.get('page_id_or_url')), 'success': False, 'result': 'Redis unavailable'} for p in posts]

    try:
        registry = IdentityRegistry(redis_client)
        identity = await registry.get(account_id)
        if not identity:
            logger.warning(f"No identity context registered for account {account_id}. Falling back to browser.")
            if browser_fallback:
                return await browser_fallback(posts)
            return [{'page': str(p.get('page_name') or p.get('page_id_or_url')), 'success': False, 'result': f'No identity registered for {account_id}'} for p in posts]

        config = AppConfig()
        token_vault = TokenVault(redis_client)
        proxy_manager = ProxyManager(config.proxy_pool, redis_client)

        results: List[Dict[str, Any]] = []
        fallback_posts: List[Dict[str, Any]] = []

        for post in posts:
            page_label = post.get('page_name') or post.get('page_id_or_url') or 'Unknown page'
            post_type = post.get('post_type', 'text')

            try:
                success, status_msg, post_id = await _post_one_fast(
                    post, cookies_json, identity, token_vault, proxy_manager, redis_client, config
                )
                if success:
                    results.append({
                        'page': page_label,
                        'success': True,
                        'result': f"Post accepted by Facebook via direct GraphQL. ID: {post_id}" if post_id else "Post accepted by Facebook via direct GraphQL."
                    })
                else:
                    logger.info(f"GraphQL post attempt failed for {page_label}: {status_msg}. Adding to fallback queue.")
                    fallback_posts.append(post)
            except Exception as exc:
                logger.error(f"Error during fast GraphQL post to {page_label}: {exc}", exc_info=True)
                fallback_posts.append(post)

        if fallback_posts and browser_fallback:
            logger.info(f"Routing {len(fallback_posts)} posts to browser fallback.")
            fallback_results = await browser_fallback(fallback_posts)
            results.extend(fallback_results)
        elif fallback_posts:
            for fp in fallback_posts:
                results.append({
                    'page': fp.get('page_name') or fp.get('page_id_or_url') or 'Unknown page',
                    'success': False,
                    'result': 'GraphQL posting failed and browser fallback is unavailable.'
                })

        return results

    finally:
        try:
            await redis_client.aclose()
        except Exception:
            pass


def create_facebook_posts_fast_sync(
    cookies_json: str,
    posts: List[Dict[str, Any]],
    progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    launch_browser_session: Optional[Any] = None,
    browser_fallback: Optional[Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]] = None,
) -> List[Dict[str, Any]]:
    """
    Synchronous wrapper to orchestrate create_facebook_posts_fast.
    """
    async def async_browser_fallback(fallback_posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not browser_fallback:
            return []
        if inspect.iscoroutinefunction(browser_fallback):
            return await browser_fallback(fallback_posts)
        else:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, browser_fallback, fallback_posts)

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    coro = create_facebook_posts_fast(
        cookies_json,
        posts,
        progress_callback,
        launch_browser_session,
        async_browser_fallback,
    )

    if loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result()
    else:
        return loop.run_until_complete(coro)
