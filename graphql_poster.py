"""Guarded HTTP GraphQL poster.

Private Facebook GraphQL publishing is disabled by default and can only run
when AppConfig.enable_private_facebook_http is explicitly true.
"""

from __future__ import annotations

import base64
import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional, Tuple
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from .config import AppConfig
from .models import IdentityContext
from .network import ProxyManager, StealthConnector
from .header_forge import AdvancedHeaderForge
from .stochastic_timer import AdvancedStochasticTimer
from .safety import SafetyGuard, SafetyStatus
from .tokens import TokenVault
from .utils import classify_error, generate_idempotence_token, maybe_await, sanitize_caption
from .metrics import RequestContext, get_metrics, init_metrics
from .graphql_validator import GraphQLRequestValidator, PermissionValidator

logger = logging.getLogger(__name__)


class HardenedGraphQLPoster:
    def __init__(
        self,
        token_vault: TokenVault,
        identity: IdentityContext,
        redis_client: Any,
        proxy_manager: ProxyManager,
        config: Optional[AppConfig] = None,
    ):
        self.tokens = token_vault
        self.identity = identity
        self.redis = redis_client
        self.proxy = proxy_manager
        self.config = config or AppConfig()
        self.safety = SafetyGuard(redis_client, identity, token_vault)
        self._req_counter = 0

    async def post_to_page(
        self,
        page_id: str,
        caption: str,
        media_fbid: Optional[str] = None,
        cookie_header: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[str]]:
        # Initialize metrics context for this request
        metrics = get_metrics()
        ctx = RequestContext.create(self.identity.account_id, page_id, "post")
        start_time = time.time()
        
        if not self.config.enable_private_facebook_http:
            return False, 'PRIVATE_HTTP_DISABLED', None

        status, message = await self.safety.pre_flight_check()
        if status != SafetyStatus.CLEAR:
            return False, f'{status.value}: {message}', None

        tokens = await self.tokens.get(self.identity.account_id)
        if not tokens or not isinstance(tokens, dict) or not tokens.get('fb_dtsg') or not tokens.get('lsd'):
            return False, 'TOKEN_EXPIRED', None

        caption = sanitize_caption(caption)
        idempotence = generate_idempotence_token(self.identity.account_id, page_id, caption)
        idemp_key = f'fb_post_idemp:{idempotence}'
        reserved = True
        if self.redis:
            reserved = bool(await maybe_await(self.redis.set(idemp_key, 'PENDING', nx=True, ex=300)))
            if not reserved:
                existing = await self._wait_for_idempotent_result(idemp_key)
                if existing:
                    return True, 'IDEMPOTENT', existing
                return False, 'IDEMPOTENCY_TIMEOUT', None

        proxy_url = ''
        try:
            payload = await self.build_payload(page_id, caption, tokens, media_fbid, idempotence)
            # Validate mutation security with built variables
            try:
                variables = json.loads(payload['variables'])
                is_valid, msg = await GraphQLRequestValidator.validate_mutation(payload['doc_id'], variables)
                if not is_valid:
                    return False, f'VALIDATION_FAILED: {msg}', None
            except Exception as e:
                return False, f'VALIDATION_ERROR: {str(e)}', None
            proxy_url = await self.proxy.get_proxy_for_account(self.identity.account_id)
            forge = AdvancedHeaderForge(
                chrome_version=self.identity.chrome_version,
                ua_override=self.identity.user_agent,
            )
            encoded_payload = urllib_parse.urlencode(payload).encode('utf-8')

            # Prefer the live (freshly-refreshed) cookie header so xs is current
            request_cookie_header = cookie_header or ''
            if not request_cookie_header:
                try:
                    from .session_heartbeat import get_live_cookie_header
                    request_cookie_header = await get_live_cookie_header(
                        self.identity.account_id,
                        self.tokens,
                        self.redis,
                        fallback_cookie_header=str(tokens.get('cookie_header') or ''),
                    )
                except Exception as _live_exc:
                    logger.debug('get_live_cookie_header failed (non-fatal): %s', _live_exc)
                    request_cookie_header = str(tokens.get('cookie_header') or '')
            request_cookie_header = request_cookie_header.strip() or None

            headers = forge.build_xhr_headers(
                host="www.facebook.com",
                origin="https://www.facebook.com",
                referer=f"https://www.facebook.com/{page_id}",
                content_length=len(encoded_payload),
                cookies=request_cookie_header,
                fb_lsd=str(tokens.get('lsd') or ''),
                fb_friendly_name="ComposerStoryCreateMutation"
            )
            timer = AdvancedStochasticTimer()
            await asyncio.sleep(timer.think_time(100, 500))
            response_code, text = await self._post_form(
                'https://www.facebook.com/api/graphql/',
                encoded_payload,
                headers,
                proxy_url,
                timeout_seconds=10,
            )
            try:
                data = self._loads_json(text)
            except (ValueError, json.JSONDecodeError):
                error_message = f'NON_JSON_RESPONSE: {text[:300]}'
                await self.safety.post_flight_validation(False, response_code, error_message)
                if self.redis:
                    await maybe_await(self.redis.delete(idemp_key))
                duration = time.time() - start_time
                await metrics.record_request(
                    ctx,
                    mutation="ComposerStoryCreateMutation",
                    duration_sec=duration,
                    success=False,
                    payload_size=0,
                )
                return False, error_message, None
            if response_code >= 400:
                error_message = f'HTTP_{response_code}: {text[:300]}'
                await self.safety.post_flight_validation(False, response_code, error_message)
                if self.redis:
                    await maybe_await(self.redis.delete(idemp_key))
                duration = time.time() - start_time
                await metrics.record_request(
                    ctx,
                    mutation="ComposerStoryCreateMutation",
                    duration_sec=duration,
                    success=False,
                    payload_size=0,
                )
                return False, error_message, None
            response_error = self._extract_response_error(data)
            if response_error:
                error_message = response_error
                status_code = classify_error(error_message)
                await self.safety.post_flight_validation(False, response_code, error_message)
                if self.redis:
                    await maybe_await(self.redis.delete(idemp_key))
                duration = time.time() - start_time
                await metrics.record_request(
                    ctx,
                    mutation="ComposerStoryCreateMutation",
                    duration_sec=duration,
                    success=False,
                    payload_size=0,
                )
                return False, status_code if status_code != 'UNKNOWN' else f'GRAPHQL_ERROR: {error_message}', None
            post_id = self._extract_post_id(data)
            if not post_id:
                error_message = f'GRAPHQL_ERROR: response did not contain a post id: {self._response_excerpt(data)}'
                await self.safety.post_flight_validation(False, response_code, error_message)
                if self.redis:
                    await maybe_await(self.redis.delete(idemp_key))
                duration = time.time() - start_time
                await metrics.record_request(
                    ctx,
                    mutation="ComposerStoryCreateMutation",
                    duration_sec=duration,
                    success=False,
                    payload_size=0,
                )
                return False, error_message, None
            await self.safety.post_flight_validation(True, response_code, '')
            if post_id and self.redis:
                await maybe_await(self.redis.setex(idemp_key, 86400, post_id))
            elif self.redis:
                await maybe_await(self.redis.delete(idemp_key))
            await self.tokens.increment_usage(self.identity.account_id)
            duration = time.time() - start_time
            await metrics.record_request(
                ctx,
                mutation='ComposerStoryCreateMutation',
                duration_sec=duration,
                success=True,
                payload_size=len(encoded_payload) if 'encoded_payload' in locals() else 0,
            )
            return True, 'SUCCESS', post_id
        except Exception as exc:
            if self.redis and reserved:
                await maybe_await(self.redis.delete(idemp_key))
            duration = time.time() - start_time
            await metrics.record_request(
                ctx,
                mutation="ComposerStoryCreateMutation",
                duration_sec=duration,
                success=False,
                payload_size=0,
            )
            await self.proxy.report_failure(self.identity.account_id, proxy_url, str(exc))
            return False, f'NETWORK_ERROR: {str(exc)[:120]}', None

    # ---------------------------------------------------------------------------
    # Hardcoded fallback doc_id — updated automatically by DocIdScraper but
    # kept here as a last resort so posting never silently fails.
    # ---------------------------------------------------------------------------
    _FALLBACK_DOC_ID = '7711610262198779'

    async def _get_doc_id(self) -> str:
        """Return the live ComposerStoryCreateMutation doc_id.

        Priority:
        1. Redis cache (written by DocIdScraper or previous scrape).
        2. Live scrape via DocIdScraper (stores result back to Redis).
        3. Hardcoded fallback constant.
        """
        if self.redis:
            cached = await maybe_await(self.redis.get('fb_graphql_doc_id'))
            if cached:
                value = cached.decode() if isinstance(cached, bytes) else str(cached)
                if value and value.isdigit():
                    return value

        # Cache miss — try to scrape the live doc_id
        logger.warning(
            'fb_graphql_doc_id not in Redis; attempting live scrape via DocIdScraper.'
        )
        try:
            from .doc_id_scraper import DocIdScraper
            scraper = DocIdScraper(redis_client=self.redis)
            # Quick passive scrape with a short timeout — don't block the post
            scraped = await asyncio.wait_for(
                scraper.scrape(cookies_json='[]', identity=self.identity),
                timeout=30,
            )
            if scraped and scraped.isdigit():
                logger.info('doc_id live-scraped: %s', scraped)
                return scraped
        except Exception as scrape_exc:
            logger.warning('DocIdScraper raised during poster fallback: %s', scrape_exc)

        logger.error(
            'fb_graphql_doc_id not found in cache and live scrape failed; '
            'using hardcoded fallback doc_id=%s',
            self._FALLBACK_DOC_ID,
        )
        return self._FALLBACK_DOC_ID

    def build_variables(
        self,
        page_id: str,
        caption: str,
        tokens: Dict[str, Any],
        idempotence_token: str,
        media_fbid: Optional[str] = None,
    ) -> Dict[str, Any]:
        variables = {
            'input': {
                'composer_entry_point': 'inline',
                'composer_source_surface': 'composer',
                'message': {'text': caption, 'ranges': []},
                'idempotence_token': idempotence_token,
                'actor_id': str(page_id),
                'client_mutation_id': str(int(time.time() * 1000)),
                'source': 'WWW',
            }
        }
        if media_fbid:
            variables['input']['attachments'] = [{'media_fbid': str(media_fbid)}]
        return variables

    async def build_payload(
        self,
        page_id: str,
        caption: str,
        tokens: Dict[str, Any],
        media_fbid: Optional[str],
        idempotence_token: str,
    ) -> Dict[str, Any]:
        """Build the complete form-encoded POST body for the GraphQL mutation.

        Includes ``fb_dtsg``, ``jazoest``, and ``lsd`` which are mandatory
        CSRF tokens — without them Facebook returns a session/auth error.
        """
        doc_id = await self._get_doc_id()
        variables = self.build_variables(page_id, caption, tokens, idempotence_token, media_fbid=media_fbid)

        fb_dtsg = str(tokens.get('fb_dtsg') or '')
        lsd = str(tokens.get('lsd') or '')

        # jazoest is a numeric checksum of fb_dtsg characters.
        # Facebook computes it as: sum(ord(c) for c in fb_dtsg) + 100.
        # If already extracted from the page, use that; otherwise compute it.
        jazoest = str(tokens.get('jazoest') or '')
        if not jazoest and fb_dtsg:
            jazoest = str(sum(ord(c) for c in fb_dtsg) + 100)

        payload: Dict[str, str] = {
            'doc_id': doc_id,
            'variables': json.dumps(variables),
            'fb_dtsg': fb_dtsg,
            'jazoest': jazoest,
            'lsd': lsd,
            '__user': str(tokens.get('user_id') or tokens.get('uid') or '0'),
            '__a': '1',
            '__req': 'z',
            '__comet_req': '15',
            'fb_api_caller_class': 'RelayModern',
            'fb_api_req_friendly_name': 'ComposerStoryCreateMutation',
            'server_timestamps': 'true',
        }
        # Strip empty string values to keep the payload clean
        return {k: v for k, v in payload.items() if v}

    async def _wait_for_idempotent_result(
        self,
        key: str,
        timeout: int = 5,
        attempts: Optional[int] = None,
    ) -> Optional[str]:
        count = attempts if attempts is not None else (timeout * 10)
        for _ in range(count):
            result = await maybe_await(self.redis.get(key))
            if result and result != b'PENDING':
                return result.decode() if isinstance(result, bytes) else str(result)
            await asyncio.sleep(0.1)
        return None

    async def _post_form(
        self,
        url: str,
        payload: bytes,
        headers: Dict[str, str],
        proxy_url: str,
        timeout_seconds: int = 10,
    ) -> Tuple[int, str]:
        try:
            import aiohttp
        except Exception:
            aiohttp = None

        if aiohttp is not None:
            connector = StealthConnector.create_connector()
            # Disable compressed transfer/brotli by stripping Accept-Encoding or forcing identity
            cleaned_headers = dict(headers)
            cleaned_headers['Accept-Encoding'] = 'gzip, deflate'  # Python/aiohttp supports gzip/deflate natively; only brotli causes errors if module is absent
            cleaned_headers['accept-encoding'] = 'gzip, deflate'
            async with aiohttp.ClientSession(connector=connector, trust_env=True) as session:
                async with session.post(
                    url,
                    data=payload,
                    headers=cleaned_headers,
                    proxy=proxy_url or None,
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                ) as resp:
                    return int(resp.status), await resp.text()

        def _sync_post() -> Tuple[int, str]:
            request_headers = dict(headers)
            request_headers['accept-encoding'] = 'identity'
            request = urllib_request.Request(url, data=payload, headers=request_headers, method='POST')
            handlers = []
            if proxy_url:
                handlers.append(urllib_request.ProxyHandler({'http': proxy_url, 'https': proxy_url}))
            opener = urllib_request.build_opener(*handlers)
            try:
                with opener.open(request, timeout=timeout_seconds) as resp:
                    status = int(getattr(resp, 'status', resp.getcode() or 200))
                    return status, resp.read().decode('utf-8', errors='replace')
            except urllib_error.HTTPError as exc:
                return int(exc.code), exc.read().decode('utf-8', errors='replace')

        return await asyncio.to_thread(_sync_post)

    def _loads_json(self, text: str) -> Any:
        cleaned = str(text or '').strip()
        if cleaned.startswith('for(;;);'):
            cleaned = cleaned[len('for(;;);'):].strip()
        if cleaned.startswith('for (;;);'):
            cleaned = cleaned[len('for (;;);'):].strip()
        if not cleaned:
            raise ValueError('empty GraphQL response body')
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            for line in cleaned.splitlines():
                candidate = str(line or '').strip()
                if candidate.startswith('for(;;);'):
                    candidate = candidate[len('for(;;);'):].strip()
                if candidate.startswith('for (;;);'):
                    candidate = candidate[len('for (;;);'):].strip()
                if candidate.startswith(('{', '[')):
                    return json.loads(candidate)
            raise

    def _extract_post_id(self, data: Any) -> Optional[str]:
        known_paths = (
            ('data', 'composer_story_create', 'story', 'legacy_story_hideable_id'),
            ('data', 'composer_story_create', 'story', 'post_id'),
            ('data', 'composer_story_create', 'post_id'),
            ('payload', 'post_id'),
            ('payload', 'story', 'legacy_story_hideable_id'),
        )
        for path in known_paths:
            current = data
            for key in path:
                if not isinstance(current, dict):
                    current = None
                    break
                current = current.get(key)
            if current:
                return str(current)

        id_keys = {'legacy_story_hideable_id', 'post_id', 'postid', 'story_id'}

        def _walk(value: Any, depth: int = 0) -> Optional[str]:
            if depth > 6:
                return None
            if isinstance(value, dict):
                for key, child in value.items():
                    if str(key).lower() in id_keys and child:
                        return str(child)
                for child in value.values():
                    found = _walk(child, depth + 1)
                    if found:
                        return found
            elif isinstance(value, list):
                for child in value[:10]:
                    found = _walk(child, depth + 1)
                    if found:
                        return found
            return None

        return _walk(data)

    def _extract_response_error(self, data: Any) -> str:
        if not isinstance(data, (dict, list)):
            return 'GraphQL response was not a JSON object'

        def _from_dict(value: Dict[str, Any]) -> str:
            errors = value.get('errors')
            if isinstance(errors, list) and errors:
                first = errors[0]
                if isinstance(first, dict):
                    message = str(first.get('message') or first.get('summary') or '').strip()
                    code = str(first.get('code') or '').strip()
                    return f'{message} (code {code})' if message and code else message or str(first)
                return str(first)

            error_code = value.get('error')
            error_summary = str(value.get('errorSummary') or value.get('error_summary') or '').strip()
            error_description = str(value.get('errorDescription') or value.get('error_description') or '').strip()
            if error_code or error_summary or error_description:
                parts = []
                if error_code:
                    parts.append(f'FACEBOOK_ERROR_{error_code}')
                if error_summary:
                    parts.append(error_summary)
                if error_description:
                    parts.append(error_description)
                return ': '.join(parts[:2]) + (f' - {parts[2]}' if len(parts) > 2 else '')
            return ''

        def _walk(value: Any, depth: int = 0) -> str:
            if depth > 6:
                return ''
            if isinstance(value, dict):
                direct = _from_dict(value)
                if direct:
                    return direct
                for child in value.values():
                    nested = _walk(child, depth + 1)
                    if nested:
                        return nested
            elif isinstance(value, list):
                for child in value[:10]:
                    nested = _walk(child, depth + 1)
                    if nested:
                        return nested
            return ''

        return _walk(data)

    def _response_excerpt(self, data: Any) -> str:
        try:
            return json.dumps(data, ensure_ascii=False, separators=(',', ':'))[:300]
        except Exception:
            return str(data)[:300]
