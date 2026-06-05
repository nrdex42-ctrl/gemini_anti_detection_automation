"""Network identity helpers."""

from __future__ import annotations

import ssl
from typing import Any, Dict, List

from .config import SafetyConfig
from .identity import IdentityRegistry
from .models import IdentityContext
from .utils import maybe_await, stable_hash


class ProxyManager:
    def __init__(self, proxy_list: List[str], redis_client: Any, require_proxy: bool = False):
        self.proxy_list = list(proxy_list or [])
        self.redis = redis_client
        self.safety = SafetyConfig()
        self.require_proxy = require_proxy
        if self.require_proxy and not self.proxy_list:
            raise ValueError('proxy pool cannot be empty when private HTTP is enabled')

    async def get_proxy_for_account(self, account_id: str) -> str:
        key = f'proxy_sticky:{account_id}'
        if self.redis is not None:
            cached = await maybe_await(self.redis.get(key))
            if cached:
                return cached.decode() if isinstance(cached, bytes) else str(cached)

            identity = await IdentityRegistry(self.redis).get(account_id)
            if identity and identity.proxy_url:
                if self.proxy_list and identity.proxy_url not in self.proxy_list:
                    raise ValueError(f'identity proxy for account {account_id} is not in the configured proxy pool')
                await maybe_await(self.redis.setex(key, self.safety.proxy_sticky_seconds, identity.proxy_url))
                return identity.proxy_url

        proxy = await self._select_healthy_proxy(account_id)
        if self.require_proxy and not proxy:
            raise ValueError(f'no proxy available for account {account_id}')
        if self.redis is not None and proxy:
            await maybe_await(self.redis.setex(key, self.safety.proxy_sticky_seconds, proxy))
        return proxy

    async def report_failure(self, account_id: str, proxy: str, error: str) -> None:
        del error
        if self.redis is None or not proxy:
            return
        key = f'proxy_health:{proxy}'
        failures = int(await maybe_await(self.redis.incr(key)))
        await maybe_await(self.redis.expire(key, 3600))
        if failures > 3:
            await maybe_await(self.redis.delete(f'proxy_sticky:{account_id}'))

    async def _select_healthy_proxy(self, account_id: str) -> str:
        if not self.proxy_list:
            return ''
        start = int(stable_hash(account_id, length=8), 16) % len(self.proxy_list)
        for offset in range(len(self.proxy_list)):
            proxy = self.proxy_list[(start + offset) % len(self.proxy_list)]
            failures = 0
            if self.redis is not None:
                raw = await maybe_await(self.redis.get(f'proxy_health:{proxy}'))
                failures = int(raw or 0)
            if failures <= 3:
                return proxy
        return self.proxy_list[start]


class StealthConnector:
    @staticmethod
    def create_connector() -> Any:
        try:
            import aiohttp  # type: ignore[reportMissingImports]
        except Exception as exc:
            raise RuntimeError('aiohttp is required for StealthConnector') from exc
        context = ssl.create_default_context()
        return aiohttp.TCPConnector(ssl=context, ttl_dns_cache=300, enable_cleanup_closed=True)


class HeaderForge:
    @staticmethod
    def _sec_ch_ua(identity: IdentityContext) -> str:
        major = identity.chrome_version.split('.', 1)[0]
        return f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not-A.Brand";v="99"'

    @classmethod
    def forge_graphql_headers(cls, tokens: Dict[str, str], identity: IdentityContext) -> Dict[str, str]:
        return {
            'accept': 'application/json, text/plain, */*',
            'accept-language': f'{identity.locale},{identity.locale.split("-")[0]};q=0.9,en;q=0.8',
            'content-type': 'application/x-www-form-urlencoded',
            'origin': 'https://www.facebook.com',
            'referer': 'https://www.facebook.com/',
            'sec-ch-ua': cls._sec_ch_ua(identity),
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': f'"{identity.platform}"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'user-agent': identity.user_agent,
            'x-fb-friendly-name': 'ComposerStoryCreateMutation',
            'x-fb-lsd': str(tokens.get('lsd') or ''),
        }

    @classmethod
    def forge_rupload_headers(
        cls,
        tokens: Dict[str, str],
        identity: IdentityContext,
        file_size: int,
        offset: int = 0,
    ) -> Dict[str, str]:
        return {
            'accept': '*/*',
            'accept-language': f'{identity.locale},{identity.locale.split("-")[0]};q=0.9,en;q=0.8',
            'content-type': 'application/octet-stream',
            'host': 'rupload.facebook.com',
            'origin': 'https://www.facebook.com',
            'referer': 'https://www.facebook.com/',
            'sec-ch-ua': cls._sec_ch_ua(identity),
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': f'"{identity.platform}"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-site',
            'user-agent': identity.user_agent,
            'x-asbd-id': '129477',
            'x-fb-lsd': str(tokens.get('lsd') or ''),
            'x-fb-fb-dtsg': str(tokens.get('fb_dtsg') or ''),
            'x-fb-upload-filesize': str(file_size),
            'x-fb-upload-offset': str(offset),
            'x-fb-upload-retry-count': '0',
            'x-entity-length': str(file_size),
        }
