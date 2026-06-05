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
        if not self.config.enable_private_facebook_http:
            return False, 'PRIVATE_HTTP_DISABLED', None

        status, message = await self.safety.pre_flight_check()
        if status != SafetyStatus.CLEAR:
            return False, f'{status.value}: {message}', None

        tokens = await self.tokens.get(self.identity.account_id)
        if not tokens:
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
            proxy_url = await self.proxy.get_proxy_for_account(self.identity.account_id)
            forge = AdvancedHeaderForge(chrome_version=self.identity.chrome_version)
            encoded_payload = urllib_parse.urlencode(payload).encode('utf-8')
            request_cookie_header = cookie_header or str(tokens.get('cookie_header') or '').strip() or None
            headers = forge.build_xhr_headers(
                host="www.facebook.com",
                origin="https://www.facebook.com",
                referer="https://www.facebook.com/",
                content_length=len(encoded_payload),
                cookies=request_cookie_header,
                fb_lsd=str(tokens.get('lsd') or ''),
                fb_friendly_name="ComposerStoryCreateMutation"
            )
            timer = AdvancedStochasticTimer()
            import asyncio
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
                return False, error_message, None
            if response_code >= 400:
                error_message = f'HTTP_{response_code}: {text[:300]}'
                await self.safety.post_flight_validation(False, response_code, error_message)
                if self.redis:
                    await maybe_await(self.redis.delete(idemp_key))
                return False, error_message, None
            errors = data.get('errors') if isinstance(data, dict) else None
            if errors:
                error_message = str((errors[0] or {}).get('message') or errors[0])
                status_code = classify_error(error_message)
                await self.safety.post_flight_validation(False, response_code, error_message)
                if self.redis:
                    await maybe_await(self.redis.delete(idemp_key))
                return False, status_code if status_code != 'UNKNOWN' else error_message, None
            post_id = self._extract_post_id(data)
            await self.safety.post_flight_validation(True, response_code, '')
            if post_id and self.redis:
                await maybe_await(self.redis.setex(idemp_key, 86400, post_id))
            elif self.redis:
                await maybe_await(self.redis.delete(idemp_key))
            await self.tokens.increment_usage(self.identity.account_id)
            return True, 'SUCCESS', post_id
        except Exception as exc:
            if self.redis and reserved:
                await maybe_await(self.redis.delete(idemp_key))
            await self.proxy.report_failure(self.identity.account_id, proxy_url, str(exc))
            return False, f'NETWORK_ERROR: {str(exc)[:120]}', None

    async def _get_doc_id(self) -> str:
        if self.redis:
            cached = await maybe_await(self.redis.get('fb_graphql_doc_id'))
            if cached:
                return cached.decode() if isinstance(cached, bytes) else str(cached)
        logger.error('fb_graphql_doc_id not found in cache; using fallback doc id for guarded private HTTP path')
        return '7711610262198779'

    def build_variables(
        self,
        page_id: str,
        caption: str,
        idempotence_token: str,
        media_fbid: Optional[str] = None,
    ) -> Dict[str, Any]:
        variables: Dict[str, Any] = {
            'input': {
                'composer_entry_point': 'inline',
                'composer_source_surface': 'composer',
                'idempotence_token': str(idempotence_token),
                'source': 'WWW',
                'message': {'ranges': [], 'text': sanitize_caption(caption)},
                'actor_id': str(page_id),
                'client_mutation_id': str(int(time.time() * 1000)),
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
    ) -> Dict[str, str]:
        variables = self.build_variables(page_id, caption, idempotence_token, media_fbid)
        payload = {
            'fb_dtsg': str(tokens.get('fb_dtsg') or ''),
            'lsd': str(tokens.get('lsd') or ''),
            'variables': json.dumps(variables, ensure_ascii=False, separators=(',', ':')),
            'doc_id': await self._get_doc_id(),
            '__req': self._generate_req_param(),
            '__a': '1',
            '__user': str(self.identity.facebook_user_id or tokens.get('user_id', '0')),
        }
        self.validate_payload_contract(payload)
        return payload

    @staticmethod
    def validate_payload_contract(payload: Dict[str, str]) -> None:
        required = {'fb_dtsg', 'lsd', 'variables', 'doc_id', '__req', '__a', '__user'}
        missing = [key for key in sorted(required) if key not in payload]
        if missing:
            raise ValueError(f'missing GraphQL payload fields: {missing}')
        if payload['__a'] != '1':
            raise ValueError('__a must be "1"')
        variables = json.loads(payload['variables'])
        body = variables.get('input') or {}
        if body.get('composer_entry_point') != 'inline':
            raise ValueError('composer_entry_point must be inline')
        if body.get('composer_source_surface') != 'composer':
            raise ValueError('composer_source_surface must be composer')
        if not isinstance(body.get('actor_id'), str):
            raise ValueError('actor_id must be string')
        if not isinstance((body.get('message') or {}).get('ranges'), list):
            raise ValueError('message.ranges must be list')

    def _generate_req_param(self) -> str:
        self._req_counter += 1
        return base64.b64encode(str(self._req_counter).encode()).decode().rstrip('=')

    @staticmethod
    def _loads_json(text: str) -> Any:
        cleaned = str(text or '').strip()
        if cleaned.startswith('for(;;);'):
            cleaned = cleaned[len('for(;;);'):].strip()
        if cleaned.startswith('for (;;);'):
            cleaned = cleaned[len('for (;;);'):].strip()
        return json.loads(cleaned or '{}')

    @staticmethod
    def _extract_post_id(data: Any) -> Optional[str]:
        if not isinstance(data, dict):
            return None
        composer = (data.get('data') or {}).get('composer_story_create') or {}
        story = composer.get('story') or {}
        return story.get('legacy_story_hideable_id') or composer.get('post_id')

    async def _post_form(
        self,
        url: str,
        data: Any,
        headers: Dict[str, str],
        proxy: str,
        timeout_seconds: int,
    ) -> Tuple[int, str]:
        try:
            import aiohttp  # type: ignore[reportMissingImports]
        except Exception:
            aiohttp = None

        if aiohttp is not None:
            connector = StealthConnector.create_connector()
            async with aiohttp.ClientSession(connector=connector, trust_env=True) as session:
                async with session.post(
                    url,
                    data=data,
                    headers=headers,
                    proxy=proxy or None,
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                ) as resp:
                    return int(resp.status), await resp.text()

        def _sync_post() -> Tuple[int, str]:
            request_headers = dict(headers)
            request_headers['accept-encoding'] = 'identity'
            if isinstance(data, bytes):
                body = data
            elif isinstance(data, str):
                body = data.encode('utf-8')
            elif isinstance(data, dict):
                body = urllib_parse.urlencode(data).encode('utf-8')
            else:
                body = urllib_parse.urlencode(dict(data)).encode('utf-8') if data else b''

            request = urllib_request.Request(url, data=body, headers=request_headers, method='POST')
            handlers = []
            if proxy:
                handlers.append(urllib_request.ProxyHandler({'http': proxy, 'https': proxy}))
            opener = urllib_request.build_opener(*handlers)
            try:
                with opener.open(request, timeout=timeout_seconds) as resp:
                    status = int(getattr(resp, 'status', resp.getcode() or 200))
                    return status, resp.read().decode('utf-8', errors='replace')
            except urllib_error.HTTPError as exc:
                return int(exc.code), exc.read().decode('utf-8', errors='replace')

        return await asyncio.to_thread(_sync_post)

    async def _wait_for_idempotent_result(self, key: str, attempts: int = 30) -> Optional[str]:
        if not self.redis:
            return None
        for _ in range(max(1, attempts)):
            existing = await maybe_await(self.redis.get(key))
            if isinstance(existing, bytes):
                existing = existing.decode('utf-8', errors='ignore')
            if existing and str(existing) != 'PENDING':
                return str(existing)
            await asyncio.sleep(1)
        return None

    @staticmethod
    async def _sleep(seconds: float) -> None:
        import asyncio

        await asyncio.sleep(max(0.0, seconds))
