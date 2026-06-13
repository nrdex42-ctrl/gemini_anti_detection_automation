"""Bridge between playwright_engine and http_worker."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fb_automation.facebook_cookie_parser import parse_cookie_payload, cookies_to_header, extract_account_id
from fb_automation.models import IdentityContext, PostJob
from fb_automation.tokens import TokenVault
from fb_automation.worker import HTTPWorker
from fb_automation.browser_fallback import BrowserTokenExtractor
from fb_automation.utils import maybe_await

logger = logging.getLogger(__name__)


_UPLOAD_AUTH_FAILURE_MARKERS = (
    'rupload_init_http_400',
    'rupload_transfer_http_400',
    'notauthorizederror',
    'not authorized',
    'upload_blocked',
    'upload rejected',
    'http_image_upload_failed',
)

_TOKEN_EXTRACTION_RECOVERABLE_MARKERS = (
    'page.goto',
    'timeout',
    'navigation',
    'domcontentloaded',
    'net::',
)

_GRAPHQL_RECOVERABLE_MARKERS = (
    'graphql_error',
    'facebook_error_1357004',
    'error 1357004',
    'sorry, something went wrong',
    'closing and re-opening your browser',
    'non_json_response',
)


def is_fast_graphql_enabled() -> bool:
    return True


def _result_text(result: Dict[str, Any]) -> str:
    return ' '.join(
        str(result.get(key) or '')
        for key in ('status', 'error', 'result')
    )


def _is_upload_auth_failure(result: Dict[str, Any]) -> bool:
    text = _result_text(result).lower()
    return any(marker in text for marker in _UPLOAD_AUTH_FAILURE_MARKERS)


def _normalize_browser_fallback_result(
    row: Any,
    original_result: Dict[str, Any],
    fallback_reason: str = 'FAST_HTTP_UPLOAD_NOT_AUTHORIZED',
) -> Dict[str, Any]:
    if not isinstance(row, dict):
        fallback_error = 'Browser fallback returned an invalid result.'
        return {
            **original_result,
            'success': False,
            'result': f"{fallback_error} Original fast HTTP error: {original_result.get('result') or original_result.get('error') or original_result.get('status')}",
            'status': 'BROWSER_FALLBACK_INVALID',
            'post_id': None,
            'error': fallback_error,
        }

    success = bool(row.get('success'))
    fallback_detail = str(row.get('result') or row.get('error') or row.get('status') or '').strip()
    fallback_status = str(
        row.get('status')
        or ('BROWSER_FALLBACK_SUCCESS' if success else 'BROWSER_FALLBACK_FAILED')
    )
    post_id = row.get('post_id') or row.get('id')
    normalized = {
        **original_result,
        'success': success,
        'result': post_id or fallback_detail or fallback_status,
        'status': fallback_status,
        'post_id': post_id,
        'error': None if success else fallback_detail or fallback_status,
        'transport': row.get('transport') or 'browser_fallback',
        'fallback_reason': fallback_reason,
    }
    if not success:
        original_detail = str(original_result.get('result') or original_result.get('error') or original_result.get('status') or '').strip()
        if original_detail:
            normalized['original_fast_error'] = original_detail
            normalized['error'] = (
                f"{normalized['error']} Original fast HTTP error: {original_detail}"
                if normalized.get('error')
                else original_detail
            )
            normalized['result'] = normalized['error']
    return normalized


def _is_recoverable_token_extraction_failure(error_text: str) -> bool:
    text = str(error_text or '').lower()
    return any(marker in text for marker in _TOKEN_EXTRACTION_RECOVERABLE_MARKERS)


def _is_recoverable_graphql_failure(result: Dict[str, Any]) -> bool:
    text = _result_text(result).lower()
    return any(marker in text for marker in _GRAPHQL_RECOVERABLE_MARKERS)


def _token_extraction_failure_results(posts: List[Dict[str, Any]], exc: Exception) -> List[Dict[str, Any]]:
    return [
        {
            'success': False,
            'result': f'TOKEN_EXTRACTION_FAILED: {exc}',
            'status': 'TOKEN_EXTRACTION_FAILED',
            'post_id': None,
            'error': str(exc),
        }
        for _ in posts
    ]


async def _recover_all_with_browser_fallback(
    posts: List[Dict[str, Any]],
    results: List[Dict[str, Any]],
    browser_fallback: Optional[Any],
    fallback_reason: str,
    log_message: str,
) -> List[Dict[str, Any]]:
    if not callable(browser_fallback):
        return results
    logger.warning(log_message, len(posts))
    try:
        fallback_rows = await maybe_await(browser_fallback(posts))
    except Exception as exc:
        logger.warning("Browser fallback after fast HTTP failure raised: %s", exc)
        return results

    if not isinstance(fallback_rows, list):
        fallback_rows = [fallback_rows]

    merged = list(results)
    for index, original in enumerate(results):
        row = fallback_rows[index] if index < len(fallback_rows) else None
        merged[index] = _normalize_browser_fallback_result(row, original, fallback_reason=fallback_reason)
    return merged


async def _recover_graphql_failures_with_browser_fallback(
    posts: List[Dict[str, Any]],
    results: List[Dict[str, Any]],
    browser_fallback: Optional[Any],
) -> List[Dict[str, Any]]:
    if not callable(browser_fallback):
        return results

    recoverable_indices = [
        index
        for index, result in enumerate(results)
        if not bool(result.get('success')) and _is_recoverable_graphql_failure(result)
    ]
    if not recoverable_indices:
        return results

    fallback_posts = [posts[index] for index in recoverable_indices]
    logger.warning(
        "Fast HTTP GraphQL mutation failed for %d/%d post(s); trying browser fallback.",
        len(fallback_posts),
        len(posts),
    )
    try:
        fallback_rows = await maybe_await(browser_fallback(fallback_posts))
    except Exception as exc:
        logger.warning("Browser fallback after fast GraphQL failure raised: %s", exc)
        return results

    if not isinstance(fallback_rows, list):
        fallback_rows = [fallback_rows]

    merged = list(results)
    for offset, index in enumerate(recoverable_indices):
        row = fallback_rows[offset] if offset < len(fallback_rows) else None
        merged[index] = _normalize_browser_fallback_result(
            row,
            results[index],
            fallback_reason='FAST_HTTP_GRAPHQL_ERROR',
        )
    return merged


async def _recover_upload_auth_failures_with_browser_fallback(
    posts: List[Dict[str, Any]],
    results: List[Dict[str, Any]],
    browser_fallback: Optional[Any],
) -> List[Dict[str, Any]]:
    if not callable(browser_fallback):
        return results

    recoverable_indices = [
        index
        for index, result in enumerate(results)
        if not bool(result.get('success')) and _is_upload_auth_failure(result)
    ]
    if not recoverable_indices:
        return results

    fallback_posts = [posts[index] for index in recoverable_indices]
    logger.warning(
        "Fast HTTP upload was not authorized for %d/%d post(s); trying browser fallback.",
        len(fallback_posts),
        len(posts),
    )
    try:
        fallback_rows = await maybe_await(browser_fallback(fallback_posts))
    except Exception as exc:
        logger.warning("Browser fallback after fast upload authorization failure raised: %s", exc)
        return results

    if not isinstance(fallback_rows, list):
        fallback_rows = [fallback_rows]

    merged = list(results)
    for offset, index in enumerate(recoverable_indices):
        row = fallback_rows[offset] if offset < len(fallback_rows) else None
        merged[index] = _normalize_browser_fallback_result(row, results[index])
    return merged


async def create_facebook_posts_fast(
    cookies_json: str,
    posts: List[Dict[str, Any]],
    progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    launch_browser_session: Optional[Any] = None,
    browser_fallback: Optional[Any] = None,
) -> Optional[List[Dict[str, Any]]]:
    logger.info("Direct HTTP/GraphQL posting tier initiated.")
    try:
        cookies = parse_cookie_payload(cookies_json)
        account_id = extract_account_id(cookies)
        if not account_id:
            logger.error("Could not extract account_id from cookies.")
            return None
    except Exception as exc:
        logger.error(f"Failed to parse cookies: {exc}")
        return None

    # Retrieve or setup proxy URL
    proxy_url = os.environ.get('PROXY_URL') or os.environ.get('FB_PROXY_URL') or ''

    # Use the User-Agent that matches the browser the cookies were exported from.
    # If not set, IdentityContext will use its own Chrome 126 default — which is
    # fine as long as FB_USER_AGENT is set in .env to match the export browser.
    _ua_override = os.environ.get('FB_USER_AGENT', '').strip()
    _identity_kwargs = dict(account_id=account_id, proxy_url=proxy_url)
    if _ua_override:
        import re as _re
        _chrome_m = _re.search(r'Chrome/(\d+\.\d+\.\d+\.\d+)', _ua_override)
        if _chrome_m:
            _identity_kwargs['user_agent'] = _ua_override
            _identity_kwargs['chrome_version'] = _chrome_m.group(1)

    identity = IdentityContext(**_identity_kwargs)

    # Initialize redis if configured
    redis_client = None
    try:
        import redis.asyncio as aioredis
        redis_url = os.environ.get('REDIS_URL')
        if redis_url:
            redis_client = aioredis.from_url(redis_url)
    except Exception as exc:
        logger.warning(f"Failed to initialize Redis client: {exc}")

    vault = TokenVault(redis_client)

    # Make sure we have tokens. If not, extract them.
    try:
        tokens = await vault.get(account_id)
        if not tokens:
            logger.info(f"Tokens missing or expired for account {account_id}. Extracting tokens via headless browser.")
            extractor = BrowserTokenExtractor(vault, identity)
            tokens = await extractor.extract_tokens(cookies_json)
            logger.info(f"Successfully extracted tokens for account {account_id}.")
        if not tokens or not isinstance(tokens, dict) or not tokens.get('fb_dtsg') or not tokens.get('lsd'):
            raise RuntimeError("Tokens are invalid or expired")
    except Exception as exc:
        logger.error(f"Token extraction failed: {exc}")
        failed_results = _token_extraction_failure_results(posts, exc)
        if _is_recoverable_token_extraction_failure(str(exc)):
            return await _recover_all_with_browser_fallback(
                posts,
                failed_results,
                browser_fallback,
                fallback_reason='FAST_HTTP_TOKEN_EXTRACTION_TIMEOUT',
                log_message=(
                    "Fast HTTP token extraction navigation failed for %d post(s); "
                    "trying browser fallback."
                ),
            )
        return failed_results

    worker = HTTPWorker(redis_client, identity)
    worker_token_vault = getattr(worker, 'token_vault', None)
    set_worker_tokens = getattr(worker_token_vault, 'set', None)
    if callable(set_worker_tokens):
        await maybe_await(set_worker_tokens(account_id, tokens))

    # -----------------------------------------------------------------------
    # Background: keep the session alive and pre-warm doc_id while posting
    # -----------------------------------------------------------------------
    _background_tasks = []
    try:
        from fb_automation.session_heartbeat import SessionHeartbeatManager
        _hb_mgr = SessionHeartbeatManager(
            cookies_json=cookies_json,
            account_id=account_id,
            token_vault=vault,
            identity=identity,
            redis_client=redis_client,
            # Use shorter intervals for the duration of a batch post
            heartbeat_interval=300,   # 5 min keep-alive during active session
            refresh_interval=1800,    # 30 min full Playwright refresh
        )
        _hb_task = asyncio.ensure_future(_hb_mgr.run_forever())
        _background_tasks.append(_hb_task)
    except Exception as _hb_exc:
        logger.debug("SessionHeartbeatManager could not be started (non-fatal): %s", _hb_exc)

    # Warm the doc_id cache if not already set — fire-and-forget
    try:
        from fb_automation.doc_id_scraper import DocIdScraper
        _scraper = DocIdScraper(redis_client=redis_client)
        cached_doc_id = await _scraper.get_cached()
        if not cached_doc_id:
            logger.info("doc_id cache empty; starting background doc_id scrape.")
            _doc_task = asyncio.ensure_future(
                _scraper.scrape(cookies_json=cookies_json, identity=identity)
            )
            _background_tasks.append(_doc_task)
        else:
            logger.debug("doc_id already cached: %s.", cached_doc_id)
    except Exception as _doc_exc:
        logger.debug("DocIdScraper warm-up skipped (non-fatal): %s", _doc_exc)

    results = []

    for post in posts:
        post_type = post.get('post_type') or 'text'
        if post_type not in {'text', 'image', 'video'}:
            post_type = 'text'

        media_url = post.get('media_url') or post.get('media_path')

        job = PostJob(
            account_id=account_id,
            page_id=post.get('page_id_or_url') or '',
            caption=post.get('caption') or '',
            media_url=media_url,
            post_type=post_type,
        )

        try:
            logger.info(f"Processing post job for page {job.page_id} via private HTTP worker...")
            res = await worker.process_job(job)
            results.append({
                'success': res.success,
                'result': res.post_id if res.success else res.error_message or res.status,
                'status': res.status,
                'post_id': res.post_id,
                'error': res.error_message
            })
        except Exception as exc:
            logger.error(f"Worker exception during posting: {exc}")
            results.append({
                'success': False,
                'result': f'WORKER_EXCEPTION: {exc}',
                'status': 'WORKER_EXCEPTION',
                'post_id': None,
                'error': str(exc)
            })

    results = await _recover_upload_auth_failures_with_browser_fallback(posts, results, browser_fallback)
    final_results = await _recover_graphql_failures_with_browser_fallback(posts, results, browser_fallback)

    # Clean up background tasks (heartbeat runs indefinitely; cancel it now)
    for _task in _background_tasks:
        if not _task.done():
            _task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(_task), timeout=2)
            except Exception:
                pass

    return final_results


def create_facebook_posts_fast_sync(
    cookies_json: str,
    posts: List[Dict[str, Any]],
    progress_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
    launch_browser_session: Optional[Any] = None,
    browser_fallback: Optional[Any] = None,
) -> Optional[List[Dict[str, Any]]]:
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
        browser_fallback,
    )
    if loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(lambda: asyncio.run(coro))
            return future.result()
    else:
        return loop.run_until_complete(coro)
