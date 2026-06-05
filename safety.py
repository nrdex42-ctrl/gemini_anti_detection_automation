"""Safety guardrails and quarantine handling."""

from __future__ import annotations

import json
import random
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from .config import SafetyConfig
from .models import IdentityContext
from .tokens import TokenVault
from .utils import classify_error, maybe_await


class SafetyStatus(str, Enum):
    CLEAR = 'CLEAR'
    QUARANTINE = 'QUARANTINE'
    COOLDOWN = 'COOLDOWN'
    CHECKPOINT = 'CHECKPOINT'


class QuarantineLevel(str, Enum):
    NONE = 'NONE'
    SOFT = 'SOFT'
    HARD = 'HARD'
    SEVERE = 'SEVERE'
    BANNED = 'BANNED'


class QuarantineManager:
    ESCALATION_MATRIX = {
        QuarantineLevel.NONE: (QuarantineLevel.SOFT, 900),
        QuarantineLevel.SOFT: (QuarantineLevel.HARD, 3600),
        QuarantineLevel.HARD: (QuarantineLevel.SEVERE, 86400),
        QuarantineLevel.SEVERE: (QuarantineLevel.BANNED, 0),
        QuarantineLevel.BANNED: (QuarantineLevel.BANNED, 0),
    }

    def __init__(self, redis_client: Any):
        self.redis = redis_client

    async def get_level(self, account_id: str) -> QuarantineLevel:
        raw = await maybe_await(self.redis.get(f'quarantine_level:{account_id}')) if self.redis else None
        if not raw:
            return QuarantineLevel.NONE
        value = raw.decode() if isinstance(raw, bytes) else str(raw)
        try:
            return QuarantineLevel(value)
        except ValueError:
            return QuarantineLevel.NONE

    async def escalate(self, account_id: str, reason: str) -> QuarantineLevel:
        current = await self.get_level(account_id)
        next_level, ttl = self.ESCALATION_MATRIX[current]
        payload = {
            'account_id': account_id,
            'level': next_level.value,
            'reason': reason,
            'created_at': datetime.now(timezone.utc).isoformat(),
        }
        if self.redis:
            if ttl > 0:
                await maybe_await(self.redis.setex(f'quarantine:{account_id}', ttl, json.dumps(payload)))
                await maybe_await(self.redis.setex(f'quarantine_level:{account_id}', ttl, next_level.value))
            else:
                await maybe_await(self.redis.set(f'quarantine:{account_id}', json.dumps(payload)))
                await maybe_await(self.redis.set(f'quarantine_level:{account_id}', next_level.value))
            await maybe_await(self.redis.xadd('quarantine_log', payload, maxlen=10000, approximate=True))
            if next_level in {QuarantineLevel.HARD, QuarantineLevel.SEVERE, QuarantineLevel.BANNED}:
                await maybe_await(self.redis.publish('admin_alerts', json.dumps({'severity': 'CRITICAL', **payload})))
        return next_level

    async def set_level(
        self,
        account_id: str,
        level: QuarantineLevel,
        reason: str,
        ttl: int,
    ) -> QuarantineLevel:
        payload = {
            'account_id': account_id,
            'level': level.value,
            'reason': reason,
            'created_at': datetime.now(timezone.utc).isoformat(),
        }
        if self.redis:
            if ttl > 0:
                await maybe_await(self.redis.setex(f'quarantine:{account_id}', ttl, json.dumps(payload)))
                await maybe_await(self.redis.setex(f'quarantine_level:{account_id}', ttl, level.value))
            else:
                await maybe_await(self.redis.set(f'quarantine:{account_id}', json.dumps(payload)))
                await maybe_await(self.redis.set(f'quarantine_level:{account_id}', level.value))
            await maybe_await(self.redis.xadd('quarantine_log', payload, maxlen=10000, approximate=True))
            await maybe_await(self.redis.publish('admin_alerts', json.dumps({'severity': 'CRITICAL', **payload})))
        return level

    async def reset(self, account_id: str, admin_override: bool = False) -> None:
        if not admin_override and self.redis:
            ttl = await maybe_await(self.redis.ttl(f'quarantine:{account_id}'))
            if int(ttl or -2) > 0:
                raise RuntimeError('quarantine TTL has not expired')
        if self.redis:
            await maybe_await(self.redis.delete(f'quarantine:{account_id}', f'quarantine_level:{account_id}'))
            await maybe_await(self.redis.delete(f'health_streak:{account_id}'))


class SafetyGuard:
    def __init__(self, redis_client: Any, identity: IdentityContext, token_vault: Optional[TokenVault] = None):
        self.redis = redis_client
        self.identity = identity
        self.account_id = identity.account_id
        self.config = SafetyConfig()
        self.token_vault = token_vault or TokenVault(redis_client)
        self.quarantine = QuarantineManager(redis_client)

    async def pre_flight_check(self) -> Tuple[SafetyStatus, str]:
        account_id = self.identity.account_id
        if self.redis and await maybe_await(self.redis.exists(f'quarantine:{account_id}')):
            return SafetyStatus.QUARANTINE, 'account is quarantined'

        now = datetime.now(timezone.utc)
        hour_key = f'post_rate:{account_id}:{now:%Y%m%d%H}'
        day_key = f'post_daily:{account_id}:{now:%Y%m%d}'
        hourly = int(await maybe_await(self.redis.get(hour_key)) or 0) if self.redis else 0
        daily = int(await maybe_await(self.redis.get(day_key)) or 0) if self.redis else 0
        if hourly > self.config.posts_per_hour:
            return SafetyStatus.COOLDOWN, 'hourly rate limit exceeded'
        if daily > self.config.posts_per_day:
            return SafetyStatus.COOLDOWN, 'daily rate limit exceeded'

        if self.identity.proxy_url and self.redis:
            failures = int(await maybe_await(self.redis.get(f'proxy_health:{self.identity.proxy_url}')) or 0)
            if failures > 3:
                return SafetyStatus.QUARANTINE, 'proxy failure threshold exceeded'

        if await self.token_vault.is_rotation_needed(account_id):
            return SafetyStatus.COOLDOWN, 'token rotation needed'

        can_proceed, sleep_seconds = await self.enforce_global_interval()
        if not can_proceed:
            return SafetyStatus.COOLDOWN, f'global interval: must wait {sleep_seconds:.1f}s'
        return SafetyStatus.CLEAR, 'clear'

    async def reserve_rate_slot(self) -> Tuple[SafetyStatus, str]:
        """Atomically reserve hourly/daily quota immediately before a post attempt."""
        if self.redis is None:
            return SafetyStatus.CLEAR, 'clear'
        account_id = self.identity.account_id
        now = datetime.now(timezone.utc)
        hour_key = f'post_rate:{account_id}:{now:%Y%m%d%H}'
        day_key = f'post_daily:{account_id}:{now:%Y%m%d}'
        hourly = await self._pipeline_incr_expire(hour_key, 3600)
        daily = await self._pipeline_incr_expire(day_key, 86400)
        if hourly > self.config.posts_per_hour:
            return SafetyStatus.COOLDOWN, f'hourly rate limit exceeded: {hourly}/{self.config.posts_per_hour}'
        if daily > self.config.posts_per_day:
            return SafetyStatus.COOLDOWN, f'daily rate limit exceeded: {daily}/{self.config.posts_per_day}'
        return SafetyStatus.CLEAR, 'reserved'

    async def enforce_global_interval(self) -> Tuple[bool, float]:
        if self.redis is None:
            return True, 0.0
        current_time = await self._redis_time()
        raw = await maybe_await(self.redis.get(f'last_post_time:{self.account_id}'))
        if not raw:
            return True, 0.0
        last_post = float(raw.decode('utf-8', errors='ignore') if isinstance(raw, bytes) else raw)
        elapsed = current_time - last_post
        if elapsed < self.config.min_interval_seconds:
            return False, self.config.min_interval_seconds - elapsed + random.uniform(0.0, 30.0)
        return True, 0.0

    async def record_post_time(self) -> None:
        if self.redis is None:
            return
        current_time = await self._redis_time()
        await maybe_await(self.redis.set(f'last_post_time:{self.account_id}', str(current_time), ex=3600))

    async def record_post_attempt(self) -> None:
        if self.redis is None:
            return
        now = datetime.now(timezone.utc)
        await self._pipeline_incr_expire(f'post_count:{self.account_id}:{now:%Y%m%d%H}', 3600)

    async def check_fallback_ratio(self) -> Tuple[bool, str]:
        if self.redis is None:
            return True, 'fallback ratio ok'
        now = datetime.now(timezone.utc)
        fallback = int(await maybe_await(self.redis.get(f'fallback_count:{self.account_id}:{now:%Y%m%d%H}')) or 0)
        total = int(await maybe_await(self.redis.get(f'post_count:{self.account_id}:{now:%Y%m%d%H}')) or 0)
        if total > 0 and fallback / total > self.config.max_browser_fallback_ratio:
            return False, f'fallback ratio {fallback / total:.1%} exceeds {self.config.max_browser_fallback_ratio:.0%}'
        return True, 'fallback ratio ok'

    async def record_fallback(self) -> None:
        if self.redis is None:
            return
        now = datetime.now(timezone.utc)
        await self._pipeline_incr_expire(f'fallback_count:{self.account_id}:{now:%Y%m%d%H}', 3600)

    async def acquire_account_lock(self, owner: str, ttl_seconds: int = 300) -> bool:
        """Reserve the single active posting slot for this account."""
        if self.redis is None:
            return True
        key = f'account_lock:{self.identity.account_id}'
        try:
            return bool(await maybe_await(self.redis.set(key, owner, nx=True, ex=ttl_seconds)))
        except TypeError:
            if await maybe_await(self.redis.exists(key)):
                return False
            await maybe_await(self.redis.setex(key, ttl_seconds, owner))
            return True

    async def release_account_lock(self, owner: str) -> None:
        if self.redis is None:
            return
        key = f'account_lock:{self.identity.account_id}'
        current = await maybe_await(self.redis.get(key))
        if isinstance(current, bytes):
            current = current.decode('utf-8', errors='ignore')
        if str(current or '') == str(owner):
            await maybe_await(self.redis.delete(key))

    async def post_flight_validation(self, success: bool, response_code: int, error_message: str) -> SafetyStatus:
        if success and response_code < 400:
            await self.record_success()
            return SafetyStatus.CLEAR
        classification = classify_error(error_message)
        if classification == 'SECURITY_CHECKPOINT':
            await self._trigger_quarantine(self.config.quarantine_severe_seconds, error_message)
            return SafetyStatus.CHECKPOINT
        if classification == 'UPLOAD_REJECTED':
            await self._trigger_quarantine(self.config.quarantine_hard_seconds, error_message)
            return SafetyStatus.QUARANTINE
        if classification in {'TOKEN_EXPIRED', 'RATE_LIMITED'}:
            await self._trigger_quarantine(self.config.quarantine_soft_seconds, error_message)
            return SafetyStatus.QUARANTINE
        if response_code in {401, 403}:
            await self._trigger_quarantine(self.config.quarantine_hard_seconds, error_message)
            return SafetyStatus.QUARANTINE
        return SafetyStatus.COOLDOWN

    async def record_success(self) -> None:
        if not self.redis:
            return
        health_key = f'account_health:{self.identity.account_id}'
        await maybe_await(self.redis.hincrby(health_key, 'success_streak', 1))
        await maybe_await(self.redis.hset(health_key, 'last_success', datetime.now(timezone.utc).isoformat()))

    async def record_failure(self, reason: str) -> None:
        if not self.redis:
            return
        health_key = f'account_health:{self.identity.account_id}'
        await maybe_await(self.redis.hset(health_key, 'success_streak', '0'))
        await maybe_await(self.redis.hset(health_key, 'last_failure', datetime.now(timezone.utc).isoformat()))
        await maybe_await(self.redis.hset(health_key, 'last_failure_reason', reason[:120] or 'FAILED'))

    async def _trigger_quarantine(self, duration_seconds: int, reason: str) -> None:
        if duration_seconds <= self.config.quarantine_soft_seconds:
            target = QuarantineLevel.SOFT
        elif duration_seconds <= self.config.quarantine_hard_seconds:
            target = QuarantineLevel.HARD
        elif duration_seconds <= self.config.quarantine_severe_seconds:
            target = QuarantineLevel.SEVERE
        else:
            target = QuarantineLevel.BANNED
        current = await self.quarantine.get_level(self.account_id)
        order = {
            QuarantineLevel.NONE: 0,
            QuarantineLevel.SOFT: 1,
            QuarantineLevel.HARD: 2,
            QuarantineLevel.SEVERE: 3,
            QuarantineLevel.BANNED: 4,
        }
        level = current if order[current] > order[target] else target
        await self.quarantine.set_level(self.account_id, level, reason[:500], duration_seconds)

    async def _pipeline_incr_expire(self, key: str, ttl: int) -> int:
        if self.redis is None:
            return 0
        if hasattr(self.redis, 'pipeline'):
            pipe = self.redis.pipeline()
            pipe.incr(key)
            pipe.expire(key, ttl)
            results = await maybe_await(pipe.execute())
            return int((results or [0])[0])
        value = int(await maybe_await(self.redis.incr(key)))
        await maybe_await(self.redis.expire(key, ttl))
        return value

    async def _redis_time(self) -> float:
        if self.redis is not None and hasattr(self.redis, 'time'):
            seconds, microseconds = await maybe_await(self.redis.time())
            return float(seconds) + float(microseconds) / 1000000.0
        return time.time()
