"""Token cache and rotation tracking."""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

from utils import canonical_json, maybe_await, stable_hash


class TokenVault:
    TOKEN_TTL_SECONDS = 1500  # 25 minutes  (was 60s — aligned with SessionHeartbeatManager ~30 min refresh)

    def __init__(self, redis_client: Any):
        self.redis = redis_client
        self._local_cache: Dict[str, Dict[str, Any]] = {}

    def _key(self, account_id: str) -> str:
        return f'fb_tokens:{account_id}'

    async def get(self, account_id: str) -> Optional[Dict[str, Any]]:
        now = time.time()
        local = self._local_cache.get(account_id)
        if (
            local
            and isinstance(local, dict)
            and local.get('fb_dtsg')
            and local.get('lsd')
            and now - float(local.get('timestamp') or 0) <= self.TOKEN_TTL_SECONDS
        ):
            return dict(local)
        raw = await maybe_await(self.redis.get(self._key(account_id))) if self.redis is not None else None
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8', errors='ignore')
        try:
            parsed = json.loads(str(raw))
            if not isinstance(parsed, dict):
                return None
            if not parsed.get('fb_dtsg') or not parsed.get('lsd'):
                return None
            if now - float(parsed.get('timestamp') or 0) > self.TOKEN_TTL_SECONDS:
                return None
            self._local_cache[account_id] = dict(parsed)
            return dict(parsed)
        except Exception:
            return None

    async def set(self, account_id: str, tokens: Dict[str, Any]) -> None:
        payload = dict(tokens)
        payload.setdefault('timestamp', time.time())
        payload['token_hash'] = self._hash_tokens(payload)
        payload.setdefault('usage_count', 0)
        self._local_cache[account_id] = dict(payload)
        if self.redis is not None:
            await maybe_await(self.redis.setex(self._key(account_id), self.TOKEN_TTL_SECONDS + 60, json.dumps(payload)))

    async def increment_usage(self, account_id: str) -> int:
        local = self._local_cache.get(account_id)
        if local:
            local['usage_count'] = int(local.get('usage_count') or 0) + 1
        if self.redis is None:
            return int((local or {}).get('usage_count') or 0)
        key = f'{self._key(account_id)}:usage'
        usage = int(await maybe_await(self.redis.incr(key)))
        await maybe_await(self.redis.expire(key, self.TOKEN_TTL_SECONDS))
        return usage

    async def is_rotation_needed(self, account_id: str) -> bool:
        tokens = await self.get(account_id)
        if not tokens:
            return True
        age = time.time() - float(tokens.get('timestamp') or 0)
        usage = int(tokens.get('usage_count') or 0)
        if self.redis is not None:
            raw_usage = await maybe_await(self.redis.get(f'{self._key(account_id)}:usage'))
            if raw_usage:
                usage = int(raw_usage)
        return age > self.TOKEN_TTL_SECONDS or usage > 50

    def _hash_tokens(self, tokens: Dict[str, Any]) -> str:
        filtered = {key: value for key, value in tokens.items() if key not in {'token_hash', 'usage_count'}}
        return stable_hash(canonical_json(filtered), length=64)
