"""Small async-compatible fake Redis for tests."""

from __future__ import annotations

import fnmatch
from typing import Any, Dict, List, Optional


class FakePipeline:
    def __init__(self, redis: 'FakeRedis'):
        self.redis = redis
        self.commands = []

    def lpush(self, key: str, value: Any) -> 'FakePipeline':
        self.commands.append(('lpush', key, value))
        return self

    def ltrim(self, key: str, start: int, end: int) -> 'FakePipeline':
        self.commands.append(('ltrim', key, start, end))
        return self

    def incr(self, key: str) -> 'FakePipeline':
        self.commands.append(('incr', key))
        return self

    def expire(self, key: str, ttl: int) -> 'FakePipeline':
        self.commands.append(('expire', key, ttl))
        return self

    async def execute(self) -> List[Any]:
        results = []
        for command in self.commands:
            name = command[0]
            if name == 'lpush':
                _, key, value = command
                results.append(await self.redis.lpush(key, value))
            elif name == 'ltrim':
                _, key, start, end = command
                results.append(await self.redis.ltrim(key, start, end))
            elif name == 'incr':
                _, key = command
                results.append(await self.redis.incr(key))
            elif name == 'expire':
                _, key, ttl = command
                results.append(await self.redis.expire(key, ttl))
        return results


class FakeRedis:
    def __init__(self):
        self.store: Dict[str, Any] = {}
        self.streams: Dict[str, List[dict]] = {}
        self.published: List[tuple] = []

    async def get(self, key: str) -> Any:
        return self.store.get(key)

    async def set(self, key: str, value: Any, *args: Any, **kwargs: Any) -> bool:
        del args
        if kwargs.get('nx') and key in self.store:
            return False
        self.store[key] = value
        return True

    async def setex(self, key: str, ttl: int, value: Any) -> bool:
        del ttl
        self.store[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        count = 0
        for key in keys:
            if key in self.store:
                count += 1
                self.store.pop(key, None)
        return count

    async def exists(self, key: str) -> int:
        return int(key in self.store)

    async def incr(self, key: str) -> int:
        self.store[key] = int(self.store.get(key) or 0) + 1
        return int(self.store[key])

    async def hincrby(self, key: str, field: str, amount: int = 1) -> int:
        bucket = self.store.setdefault(key, {})
        bucket[field] = int(bucket.get(field) or 0) + int(amount)
        return int(bucket[field])

    async def hset(self, key: str, field: str, value: Any) -> int:
        bucket = self.store.setdefault(key, {})
        existed = field in bucket
        bucket[field] = value
        return 0 if existed else 1

    async def hget(self, key: str, field: str) -> Any:
        bucket = self.store.get(key, {})
        return bucket.get(field) if isinstance(bucket, dict) else None

    async def hgetall(self, key: str) -> Dict[str, Any]:
        bucket = self.store.get(key, {})
        return dict(bucket) if isinstance(bucket, dict) else {}

    async def sadd(self, key: str, *values: Any) -> int:
        bucket = self.store.setdefault(key, set())
        if not isinstance(bucket, set):
            bucket = set(bucket if isinstance(bucket, (list, tuple, set)) else [])
            self.store[key] = bucket
        before = len(bucket)
        for value in values:
            bucket.add(value)
        return len(bucket) - before

    async def smembers(self, key: str) -> set:
        bucket = self.store.get(key, set())
        return set(bucket) if isinstance(bucket, (set, list, tuple)) else set()

    async def srem(self, key: str, *values: Any) -> int:
        bucket = self.store.get(key, set())
        if not isinstance(bucket, set):
            bucket = set(bucket if isinstance(bucket, (list, tuple, set)) else [])
            self.store[key] = bucket
        removed = 0
        for value in values:
            if value in bucket:
                bucket.remove(value)
                removed += 1
        return removed

    async def expire(self, key: str, ttl: int) -> bool:
        del key, ttl
        return True

    async def ttl(self, key: str) -> int:
        return -1 if key in self.store else -2

    async def time(self) -> tuple:
        import time

        now = time.time()
        seconds = int(now)
        microseconds = int((now - seconds) * 1000000)
        return seconds, microseconds

    async def keys(self, pattern: str) -> List[str]:
        return [key for key in self.store if fnmatch.fnmatch(key, pattern)]

    async def rpush(self, key: str, value: Any) -> int:
        self.store.setdefault(key, []).append(value)
        return len(self.store[key])

    async def lpush(self, key: str, value: Any) -> int:
        self.store.setdefault(key, []).insert(0, value)
        return len(self.store[key])

    async def lpop(self, key: str) -> Any:
        values = self.store.get(key, [])
        if not values:
            return None
        return values.pop(0)

    async def ltrim(self, key: str, start: int, end: int) -> bool:
        values = self.store.get(key, [])
        stop = None if end == -1 else end + 1
        self.store[key] = values[start:stop]
        return True

    async def lrange(self, key: str, start: int, end: int) -> List[Any]:
        values = self.store.get(key, [])
        stop = None if end == -1 else end + 1
        return values[start:stop]

    async def xadd(self, key: str, fields: dict, maxlen: Optional[int] = None, approximate: bool = True) -> str:
        del maxlen, approximate
        self.streams.setdefault(key, []).append(dict(fields))
        return f'{len(self.streams[key])}-0'

    async def xrevrange(self, key: str, count: int = 100) -> List[tuple]:
        rows = self.streams.get(key, [])[-count:][::-1]
        return [(f'{index}-0', row) for index, row in enumerate(rows)]

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 1

    async def close(self) -> None:
        return None

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)
