"""RateLimiter — Redis-backed token bucket per (account_id, action).

Uses Lua scripting for atomic consume-and-refill. Each (account_id, action) pair
has a token bucket with:
  - Capacity = hourly budget for that action
  - Refill rate = capacity / 3600 (tokens per second)

Empirical budgets from the reference document (conservative column):

    Action              Budget/hr
    ──────────────────  ─────────
    post:text           8
    post:single_photo   5
    post:multi_photo    5
    post:video          2
    post:reel           2
    post:story          3
    comment             30
    like                100
    friend_request      20
    page_like           10
    group_join          5
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Lua script for atomic token bucket consume
_LUA_CONSUME = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local data = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(data[1]) or capacity
local last_refill = tonumber(data[2]) or now
local elapsed = math.max(0, now - last_refill)
local refilled = elapsed * refill_rate
tokens = math.min(capacity, tokens + refilled)
if tokens >= cost then
    tokens = tokens - cost
    redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now)
    redis.call('EXPIRE', key, 7200)
    return {1, tokens}
else
    redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now)
    redis.call('EXPIRE', key, 7200)
    local wait_s = (cost - tokens) / refill_rate
    return {0, wait_s}
end
"""


class RateLimiter:
    """Per-account, per-action token bucket rate limiter.

    Buckets are stored in Redis as hashmaps with ``tokens`` and ``last_refill``
    fields. The Lua script handles refill + consume atomically.

    Usage::

        limiter = RateLimiter(redis_client)
        allowed, info = await limiter.consume("123", "post:text")
        if allowed:
            # proceed with post
        else:
            # info = seconds to wait
            await asyncio.sleep(info + jitter)
    """

    BUDGETS: Dict[str, Tuple[float, float]] = {
        "post:text":            (8,   8.0 / 3600.0),
        "post:single_photo":   (5,   5.0 / 3600.0),
        "post:multi_photo":    (5,   5.0 / 3600.0),
        "post:video":          (2,   2.0 / 3600.0),
        "post:reel":           (2,   2.0 / 3600.0),
        "post:story":          (3,   3.0 / 3600.0),
        "comment":             (30, 30.0 / 3600.0),
        "like":                (100, 100.0 / 3600.0),
        "friend_request":      (20,  20.0 / 3600.0),
        "page_like":           (10,  10.0 / 3600.0),
        "group_join":          (5,    5.0 / 3600.0),
    }

    # Jitter as fraction of wait time (±15%)
    JITTER_FRACTION = 0.15

    def __init__(self, redis_client: Optional[Any] = None):
        self.redis = redis_client
        self._lua_script: Optional[str] = None
        self._local_buckets: Dict[str, Dict[str, float]] = {}

    async def consume(
        self,
        account_id: str,
        action: str,
        cost: int = 1,
    ) -> Tuple[bool, float]:
        """Try to consume *cost* tokens for an action.

        Returns (allowed, info):
          - allowed=True:  info = remaining tokens
          - allowed=False: info = seconds to wait for enough tokens
        """
        if action not in self.BUDGETS:
            raise ValueError(f"Unknown action: {action}. Known: {list(self.BUDGETS)}")

        capacity, refill_rate = self.BUDGETS[action]
        key = f"rate:{account_id}:{action}"
        now = time.time()

        if self.redis is not None:
            return await self._consume_redis(key, capacity, refill_rate, now, cost)
        else:
            return self._consume_local(key, capacity, refill_rate, now, cost)

    async def _consume_redis(
        self,
        key: str,
        capacity: float,
        refill_rate: float,
        now: float,
        cost: int,
    ) -> Tuple[bool, float]:
        if self._lua_script is None:
            self._lua_script = await self.redis.script_load(_LUA_CONSUME)

        result = await self.redis.evalsha(
            self._lua_script, 1, key,
            capacity, refill_rate, now, cost,
        )
        allowed = bool(int(result[0]))
        info = float(result[1])

        if not allowed:
            logger.info(
                "Rate limit denied for %s: wait %.1fs",
                key, info,
            )
        return allowed, info

    def _consume_local(
        self,
        key: str,
        capacity: float,
        refill_rate: float,
        now: float,
        cost: int,
    ) -> Tuple[bool, float]:
        bucket = self._local_buckets.get(key, {"tokens": capacity, "last_refill": now})
        elapsed = max(0.0, now - bucket["last_refill"])
        bucket["tokens"] = min(capacity, bucket["tokens"] + elapsed * refill_rate)
        bucket["last_refill"] = now

        if bucket["tokens"] >= cost:
            bucket["tokens"] -= cost
            self._local_buckets[key] = bucket
            return True, bucket["tokens"]
        else:
            wait_s = (cost - bucket["tokens"]) / refill_rate if refill_rate > 0 else 3600.0
            self._local_buckets[key] = bucket
            return False, wait_s

    async def check_only(
        self,
        account_id: str,
        action: str,
    ) -> Tuple[bool, float]:
        """Check whether consume would succeed without consuming tokens."""
        if action not in self.BUDGETS:
            raise ValueError(f"Unknown action: {action}")

        capacity, refill_rate = self.BUDGETS[action]
        key = f"rate:{account_id}:{action}"
        now = time.time()

        if self.redis is not None:
            data = await self.redis.hgetall(key)
            tokens = float(data.get(b"tokens", data.get("tokens", capacity)))
            last_refill = float(data.get(b"last_refill", data.get("last_refill", now)))
        else:
            bucket = self._local_buckets.get(key, {})
            tokens = bucket.get("tokens", capacity)
            last_refill = bucket.get("last_refill", now)

        elapsed = max(0.0, now - last_refill)
        tokens = min(capacity, tokens + elapsed * refill_rate)

        if tokens >= 1:
            return True, tokens
        else:
            return False, (1.0 - tokens) / refill_rate if refill_rate > 0 else 3600.0

    def jittered_wait(self, wait_s: float) -> float:
        """Add uniform jitter of ±JITTER_FRACTION to the wait time."""
        jitter = wait_s * self.JITTER_FRACTION
        return max(0.0, wait_s + random.uniform(-jitter, jitter))
