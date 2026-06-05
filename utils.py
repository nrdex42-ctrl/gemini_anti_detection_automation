"""Utility helpers for the guarded automation package."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional
from urllib.parse import parse_qs, urlparse


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def cookies_json_to_header(cookies_json: str) -> str:
    """Convert stored Playwright-style cookies JSON to an HTTP Cookie header."""
    parsed = json.loads(cookies_json or '[]')
    if not isinstance(parsed, list):
        raise ValueError('cookies_json must be a JSON list')
    parts = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '').strip()
        value = str(item.get('value') or '')
        if name:
            parts.append(f'{name}={value}')
    if not parts:
        raise ValueError('no cookies found')
    return '; '.join(parts)


def generate_client_id(account_id: str) -> str:
    return hashlib.sha256(str(account_id).encode('utf-8')).hexdigest()[:16]


def generate_idempotence_token(
    account_id: str,
    page_id: str,
    caption: str,
    time_bucket_minutes: int = 10,
) -> str:
    bucket = int(time.time() // max(60, time_bucket_minutes * 60))
    seed = canonical_json({
        'account_id': str(account_id),
        'page_id': str(page_id),
        'caption_hash': hashlib.sha256(str(caption or '').encode('utf-8')).hexdigest(),
        'bucket': bucket,
    })
    return hashlib.sha256(seed.encode('utf-8')).hexdigest()


def extract_page_id(page_url_or_id: str) -> str:
    value = str(page_url_or_id or '').strip()
    if not value:
        return ''
    if value.isdigit():
        return value
    parsed = urlparse(value if '://' in value else f'https://facebook.com/{value}')
    if parsed.path == '/profile.php':
        page_id = (parse_qs(parsed.query).get('id') or [''])[0]
        return page_id.strip()
    path = parsed.path.strip('/')
    if path:
        return path.split('/')[-1]
    return value


def classify_error(error_msg: str) -> str:
    text = str(error_msg or '').lower()
    if any(marker in text for marker in ('checkpoint', 'confirm your identity', 'security', 'locked')):
        return 'SECURITY_CHECKPOINT'
    if any(marker in text for marker in ('token', 'session', 'logged out', 'login required', 'expired')):
        return 'TOKEN_EXPIRED'
    if any(marker in text for marker in ('1366046', "can't read files", "couldn't be uploaded", 'upload rejected')):
        return 'UPLOAD_REJECTED'
    if any(marker in text for marker in ('rate limit', 'too many', 'temporarily blocked', '429')):
        return 'RATE_LIMITED'
    if 'graphql' in text or 'mutation' in text or 'doc_id' in text:
        return 'GRAPHQL_ERROR'
    return 'UNKNOWN'


def sanitize_caption(caption: str) -> str:
    cleaned = re.sub(r'\s+', ' ', str(caption or '')).strip()
    if len(cleaned) > 5000:
        return cleaned[:5000]
    return cleaned


def stable_hash(*parts: Any, length: int = 32) -> str:
    seed = canonical_json([str(part) for part in parts])
    return hashlib.sha256(seed.encode('utf-8')).hexdigest()[:length]


async def redis_get(redis_client: Any, key: str) -> Optional[str]:
    if redis_client is None:
        return None
    value = await maybe_await(redis_client.get(key))
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='ignore')
    return None if value is None else str(value)


async def redis_setex(redis_client: Any, key: str, ttl: int, value: Any) -> None:
    if redis_client is None:
        return
    await maybe_await(redis_client.setex(key, int(ttl), value))


async def redis_delete(redis_client: Any, *keys: str) -> None:
    if redis_client is None:
        return
    await maybe_await(redis_client.delete(*keys))


async def redis_incr(redis_client: Any, key: str) -> int:
    if redis_client is None:
        return 0
    return int(await maybe_await(redis_client.incr(key)))


async def redis_expire(redis_client: Any, key: str, ttl: int) -> None:
    if redis_client is None:
        return
    await maybe_await(redis_client.expire(key, int(ttl)))


async def redis_lrange(redis_client: Any, key: str, start: int, end: int) -> list:
    if redis_client is None:
        return []
    values = await maybe_await(redis_client.lrange(key, start, end))
    decoded = []
    for value in values or []:
        decoded.append(value.decode('utf-8', errors='ignore') if isinstance(value, bytes) else value)
    return decoded


def ensure_list(value: Optional[Iterable[Any]]) -> list:
    return list(value or [])
