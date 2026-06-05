"""Persistent identity registry."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .models import IdentityContext
from .utils import maybe_await, stable_hash


class IdentityRegistry:
    def __init__(self, redis_client: Any):
        self.redis = redis_client
        self.key_prefix = 'identity_ctx'
        self.fingerprint_prefix = 'identity_fp'
        self.account_fingerprint_prefix = 'identity_fp_account'

    async def register(self, ctx: IdentityContext) -> None:
        existing_ctx = await self.get(ctx.account_id)
        existing_fingerprint = self.identity_fingerprint(existing_ctx) if existing_ctx is not None else ''
        existing = await self.find_accounts_by_proxy(ctx.proxy_url)
        if existing and ctx.account_id not in existing:
            raise ValueError(f'proxy already assigned to accounts: {existing}')
        await maybe_await(
            self.redis.set(
                f'{self.key_prefix}:{ctx.account_id}',
                json.dumps(ctx.to_dict(), ensure_ascii=False),
            )
        )
        if existing_ctx is not None:
            await self._remove_fingerprint_index(existing_ctx.account_id, existing_fingerprint)
        await self._add_fingerprint_index(ctx.account_id, self.identity_fingerprint(ctx))

    async def get(self, account_id: str) -> Optional[IdentityContext]:
        raw = await maybe_await(self.redis.get(f'{self.key_prefix}:{account_id}'))
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8', errors='ignore')
        return IdentityContext.from_dict(json.loads(str(raw)))

    async def find_accounts_by_proxy(self, proxy_url: str) -> List[str]:
        if not proxy_url:
            return []
        accounts: List[str] = []
        keys = await maybe_await(self.redis.keys(f'{self.key_prefix}:*'))
        for key in keys or []:
            raw = await maybe_await(self.redis.get(key))
            if not raw:
                continue
            if isinstance(raw, bytes):
                raw = raw.decode('utf-8', errors='ignore')
            data = json.loads(str(raw))
            if data.get('proxy_url') == proxy_url:
                key_text = key.decode('utf-8', errors='ignore') if isinstance(key, bytes) else str(key)
                accounts.append(key_text.split(':', 1)[1])
        return accounts

    async def find_accounts_by_fingerprint(self, fingerprint: str) -> List[str]:
        if not fingerprint:
            return []
        if self.redis is not None and hasattr(self.redis, 'smembers'):
            raw_accounts = await maybe_await(self.redis.smembers(f'{self.fingerprint_prefix}:{fingerprint}'))
            accounts = [
                value.decode('utf-8', errors='ignore') if isinstance(value, bytes) else str(value)
                for value in raw_accounts or []
            ]
            return sorted(set(accounts))
        accounts: List[str] = []
        for ctx in await self.list_all():
            if self.identity_fingerprint(ctx) == fingerprint:
                accounts.append(ctx.account_id)
        return sorted(set(accounts))

    async def is_proxy_unique(self, proxy_url: str, exclude_account: str = '') -> bool:
        if not proxy_url:
            return True
        for ctx in await self.list_all():
            if ctx.account_id != exclude_account and ctx.proxy_url == proxy_url:
                return False
        return True

    def identity_fingerprint(self, ctx: IdentityContext) -> str:
        payload = self._fingerprint_payload(ctx.to_dict())
        return stable_hash(json.dumps(payload, sort_keys=True, ensure_ascii=False), length=64)

    async def list_all(self) -> List[IdentityContext]:
        contexts: List[IdentityContext] = []
        keys = await maybe_await(self.redis.keys(f'{self.key_prefix}:*'))
        for key in keys or []:
            raw = await maybe_await(self.redis.get(key))
            if not raw:
                continue
            if isinstance(raw, bytes):
                raw = raw.decode('utf-8', errors='ignore')
            contexts.append(IdentityContext.from_dict(json.loads(str(raw))))
        return contexts

    async def _add_fingerprint_index(self, account_id: str, fingerprint: str) -> None:
        if not fingerprint or self.redis is None:
            return
        await maybe_await(self.redis.set(f'{self.account_fingerprint_prefix}:{account_id}', fingerprint))
        if hasattr(self.redis, 'sadd'):
            await maybe_await(self.redis.sadd(f'{self.fingerprint_prefix}:{fingerprint}', account_id))

    async def _remove_fingerprint_index(self, account_id: str, fingerprint: str) -> None:
        if self.redis is None:
            return
        if fingerprint and hasattr(self.redis, 'srem'):
            await maybe_await(self.redis.srem(f'{self.fingerprint_prefix}:{fingerprint}', account_id))
        await maybe_await(self.redis.delete(f'{self.account_fingerprint_prefix}:{account_id}'))

    @staticmethod
    def _fingerprint_payload(data: Dict[str, Any]) -> Dict[str, Any]:
        keep = {
            'user_agent',
            'viewport',
            'timezone',
            'locale',
            'geolocation',
            'screen_resolution',
            'color_depth',
            'platform',
            'chrome_version',
            'webgl_vendor',
            'webgl_renderer',
            'fonts',
            'audio_sample_rate',
        }
        payload = {key: data.get(key) for key in keep}
        if isinstance(payload.get('fonts'), list):
            payload['fonts'] = list(payload['fonts'])
        return payload
