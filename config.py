"""Configuration and non-overridable guardrails."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Set, Tuple

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict  # type: ignore[reportMissingImports]
except Exception:  # pragma: no cover - local compatibility.
    BaseSettings = object  # type: ignore
    SettingsConfigDict = None  # type: ignore


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


def _env_list(name: str) -> List[str]:
    raw = os.environ.get(name, '')
    return [part.strip() for part in raw.split(',') if part.strip()]


class AppConfig(BaseSettings):  # type: ignore[misc]
    redis_url: str = os.environ.get('REDIS_URL', '')
    proxy_pool: List[str] = []
    admin_webhook_url: str = os.environ.get('ADMIN_WEBHOOK_URL', '')
    alert_channel: str = os.environ.get('ALERT_CHANNEL', 'admin_alerts')
    log_level: str = os.environ.get('LOG_LEVEL', 'INFO')
    worker_concurrency: int = _env_int('WORKER_CONCURRENCY', 4)
    worker_poll_interval_seconds: float = _env_float('WORKER_POLL_INTERVAL_SECONDS', 1.0)
    enable_browser_fallback: bool = _env_bool('ENABLE_BROWSER_FALLBACK', True)
    browser_fallback_errors: List[str] = ['TOKEN_EXPIRED', 'RUPLOAD_FAILED']
    max_browser_fallback_ratio: float = _env_float('MAX_BROWSER_FALLBACK_RATIO', 0.1)
    enable_private_facebook_http: bool = _env_bool('FB_AUTOMATION_ENABLE_PRIVATE_HTTP', True)

    if SettingsConfigDict is not None:
        model_config = SettingsConfigDict(env_prefix='', extra='ignore')

    def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
        if BaseSettings is object:
            proxy_pool = kwargs.get('proxy_pool')
            if proxy_pool is None:
                proxy_pool = _env_list('PROXY_POOL')
            for key, value in {
                'redis_url': os.environ.get('REDIS_URL', ''),
                'proxy_pool': list(proxy_pool),
                'admin_webhook_url': os.environ.get('ADMIN_WEBHOOK_URL', ''),
                'alert_channel': os.environ.get('ALERT_CHANNEL', 'admin_alerts'),
                'log_level': os.environ.get('LOG_LEVEL', 'INFO'),
                'worker_concurrency': _env_int('WORKER_CONCURRENCY', 4),
                'worker_poll_interval_seconds': _env_float('WORKER_POLL_INTERVAL_SECONDS', 1.0),
                'enable_browser_fallback': _env_bool('ENABLE_BROWSER_FALLBACK', True),
                'browser_fallback_errors': kwargs.get(
                    'browser_fallback_errors',
                    _env_list('BROWSER_FALLBACK_ERRORS') or ['TOKEN_EXPIRED', 'RUPLOAD_FAILED'],
                ),
                'max_browser_fallback_ratio': _env_float('MAX_BROWSER_FALLBACK_RATIO', 0.1),
                'enable_private_facebook_http': _env_bool('FB_AUTOMATION_ENABLE_PRIVATE_HTTP', True),
            }.items():
                setattr(self, key, kwargs.get(key, value))
        else:
            super().__init__(**kwargs)


# Compatibility exports for the legacy Playwright engine module.
TELEGRAM_TOKEN: str = os.environ.get('TELEGRAM_TOKEN', '')
DIAGNOSTICS_DIR: Path = Path(os.environ.get('DIAGNOSTICS_DIR', str(Path(__file__).resolve().parent / 'diagnostics')))
DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
HEADLESS: bool = _env_bool('HEADLESS', False)
ADMIN_USER_IDS: Set[int] = {
    int(part.strip())
    for part in os.environ.get('ADMIN_USER_IDS', '').split(',')
    if part.strip().isdigit()
}


@dataclass(frozen=True)
class SafetyConfig:
    posts_per_hour: int = 6
    posts_per_day: int = 20
    min_interval_seconds: int = 120
    max_concurrent_posts_per_account: int = 1
    max_concurrent_accounts_global: int = 50
    token_ttl_seconds: int = 240
    token_max_usage_count: int = 20
    quarantine_soft_seconds: int = 900
    quarantine_hard_seconds: int = 3600
    quarantine_severe_seconds: int = 86400
    max_image_size_bytes: int = 15 * 1024 * 1024
    min_image_dimension: int = 100
    proxy_sticky_seconds: int = 600
    proxy_max_failures: int = 3
    request_timeout_seconds: int = 15
    upload_timeout_seconds: int = 45
    max_browser_fallback_ratio: float = 0.1


class EthicalGuardrails:
    """Non-configurable guardrails. Do not load these values from env."""

    ABSOLUTE_MAX_POSTS_PER_DAY = 50
    ABSOLUTE_MIN_INTERVAL_SECONDS = 60.0
    PROHIBITED_CONTENT_HASHES: Set[str] = set()

    @classmethod
    def validate_content(
        cls,
        caption_hash: str = '',
        caption: str = '',
        media_path: Optional[str] = None,
    ) -> Tuple[bool, str]:
        text = str(caption or '').strip()
        if len(text) < 3:
            return False, 'content too short'
        if len(text) > 5000:
            return False, 'caption exceeds 5000 characters'
        words = text.lower().split()
        if len(words) > 10 and len(set(words)) / max(1, len(words)) < 0.3:
            return False, 'excessive repetition detected'
        if caption_hash and caption_hash in cls.PROHIBITED_CONTENT_HASHES:
            return False, 'caption hash is prohibited'
        if media_path:
            path = Path(media_path)
            if path.exists():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if digest in cls.PROHIBITED_CONTENT_HASHES:
                    return False, 'prohibited media fingerprint'
        return True, 'ok'

    @classmethod
    def validate_velocity(cls, daily_count: int, interval_seconds: float) -> Tuple[bool, str]:
        if daily_count > cls.ABSOLUTE_MAX_POSTS_PER_DAY:
            return False, 'absolute daily post limit exceeded'
        if interval_seconds < cls.ABSOLUTE_MIN_INTERVAL_SECONDS:
            return False, 'absolute minimum interval not satisfied'
        return True, 'ok'
