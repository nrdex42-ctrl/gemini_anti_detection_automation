"""
ProxyManager - Residential proxy management with sticky sessions.

Handles proxy rotation, health checking, and sticky session
management for maintaining consistent IP identity per account.

This is the advanced implementation. The existing stub ProxyManager
in network.py is preserved for backward compatibility.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


@dataclass
class ProxyEndpoint:
    """A single proxy server endpoint."""
    url: str
    protocol: str  # http, https, socks5
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    country: Optional[str] = None
    city: Optional[str] = None
    is_residential: bool = False

    # Health tracking
    success_count: int = 0
    fail_count: int = 0
    use_count: int = 0
    last_used: float = 0.0
    last_check: float = 0.0
    last_latency_ms: float = 0.0
    is_healthy: bool = True
    cooldown_until: float = 0.0

    @property
    def formatted_url(self) -> str:
        """URL with credentials for Playwright/requests."""
        if self.username and self.password:
            return f"{self.protocol}://{self.username}:{self.password}@{self.host}:{self.port}"
        return f"{self.protocol}://{self.host}:{self.port}"

    @property
    def display_url(self) -> str:
        """URL without credentials for logging."""
        return f"{self.protocol}://{self.host}:{self.port}"

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.fail_count
        if total == 0:
            return 1.0
        return self.success_count / total


@dataclass
class StickySession:
    """Maps an account identity to a specific proxy."""
    account_key: str
    proxy_url: str
    created_at: float = 0.0
    last_used: float = 0.0
    use_count: int = 0


@dataclass
class ProxyManagerConfig:
    """Configuration for proxy management."""
    # Health checking
    health_check_interval_seconds: int = 300
    health_check_timeout_seconds: int = 10
    health_check_url: str = "https://www.facebook.com/"
    max_fail_count_before_cooldown: int = 3
    cooldown_duration_seconds: int = 600

    # Sticky sessions
    sticky_session_ttl_seconds: int = 3600
    sticky_session_max_uses: int = 50

    # Rotation
    rotate_on_failure: bool = True
    prefer_residential: bool = True
    prefer_same_country: bool = False

    # Load balancing
    max_concurrent_per_proxy: int = 3
    proxy_selection_strategy: str = "least_used"  # random, round_robin, least_used, lowest_latency


class AdvancedProxyManager:
    """
    Manages a pool of proxy endpoints with:
    - Sticky sessions (same proxy per account)
    - Health checking and auto-cooldown
    - Smart selection (least used, lowest latency, etc.)
    - Concurrent usage tracking

    Named AdvancedProxyManager to avoid collision with the existing
    ProxyManager in network.py.
    """

    def __init__(
        self,
        proxies: Optional[List[str]] = None,
        config: Optional[ProxyManagerConfig] = None,
    ):
        self.config = config or ProxyManagerConfig()
        self._endpoints: List[ProxyEndpoint] = []
        self._sticky_sessions: Dict[str, StickySession] = {}
        self._concurrent_usage: Dict[str, int] = {}
        self._round_robin_index = 0

        if proxies:
            for proxy_url in proxies:
                self.add_proxy(proxy_url)

    def add_proxy(
        self,
        url: str,
        country: Optional[str] = None,
        is_residential: bool = False,
    ) -> ProxyEndpoint:
        """Add a proxy endpoint to the pool."""
        parsed = urlparse(url)
        endpoint = ProxyEndpoint(
            url=url,
            protocol=parsed.scheme or "http",
            host=parsed.hostname or "",
            port=parsed.port or 8080,
            username=parsed.username,
            password=parsed.password,
            country=country,
            is_residential=is_residential,
        )
        self._endpoints.append(endpoint)
        logger.info(f"ProxyManager: added {endpoint.display_url} (residential={is_residential})")
        return endpoint

    def get_proxy_for_account(
        self,
        account_key: str,
        prefer_country: Optional[str] = None,
    ) -> Optional[str]:
        """
        Get a proxy URL for a specific account.

        Uses sticky sessions: the same account always gets the same proxy
        (until the session expires or the proxy becomes unhealthy).
        """
        # Check existing sticky session
        session = self._sticky_sessions.get(account_key)
        if session is not None:
            now = time.monotonic()
            age = now - session.created_at
            endpoint = self._find_endpoint(session.proxy_url)

            # Session still valid?
            if (
                endpoint is not None
                and endpoint.is_healthy
                and now > endpoint.cooldown_until
                and age < self.config.sticky_session_ttl_seconds
                and session.use_count < self.config.sticky_session_max_uses
            ):
                session.last_used = now
                session.use_count += 1
                self._concurrent_usage[session.proxy_url] = (
                    self._concurrent_usage.get(session.proxy_url, 0) + 1
                )
                logger.debug(
                    f"ProxyManager: sticky session for {account_key} → {endpoint.display_url} "
                    f"(use #{session.use_count})"
                )
                return endpoint.formatted_url
            else:
                # Session expired or proxy unhealthy, remove it
                del self._sticky_sessions[account_key]
                logger.debug(f"ProxyManager: sticky session expired for {account_key}")

        # Select a new proxy
        endpoint = self._select_proxy(prefer_country=prefer_country)
        if endpoint is None:
            return None

        # Create sticky session
        now = time.monotonic()
        self._sticky_sessions[account_key] = StickySession(
            account_key=account_key,
            proxy_url=endpoint.formatted_url,
            created_at=now,
            last_used=now,
            use_count=1,
        )
        self._concurrent_usage[endpoint.formatted_url] = (
            self._concurrent_usage.get(endpoint.formatted_url, 0) + 1
        )

        logger.info(
            f"ProxyManager: new session for {account_key} → {endpoint.display_url}"
        )
        return endpoint.formatted_url

    def release_proxy(self, proxy_url: str):
        """Release a proxy after use (decrement concurrent count)."""
        current = self._concurrent_usage.get(proxy_url, 0)
        if current > 0:
            self._concurrent_usage[proxy_url] = current - 1

    def report_success(self, proxy_url: str):
        """Report a successful request through a proxy."""
        endpoint = self._find_endpoint(proxy_url)
        if endpoint:
            endpoint.success_count += 1
            endpoint.last_used = time.monotonic()
            endpoint.is_healthy = True

    def report_failure(self, proxy_url: str):
        """Report a failed request through a proxy."""
        endpoint = self._find_endpoint(proxy_url)
        if endpoint:
            endpoint.fail_count += 1
            endpoint.last_used = time.monotonic()

            if endpoint.fail_count >= self.config.max_fail_count_before_cooldown:
                endpoint.is_healthy = False
                endpoint.cooldown_until = time.monotonic() + self.config.cooldown_duration_seconds
                logger.warning(
                    f"ProxyManager: {endpoint.display_url} cooldown until "
                    f"{self.config.cooldown_duration_seconds}s from now"
                )

            if self.config.rotate_on_failure:
                # Invalidate all sticky sessions using this proxy
                stale_keys = [
                    k for k, v in self._sticky_sessions.items()
                    if v.proxy_url == proxy_url
                ]
                for key in stale_keys:
                    del self._sticky_sessions[key]
                    logger.debug(f"ProxyManager: invalidated sticky session for {key}")

    def _find_endpoint(self, proxy_url: str) -> Optional[ProxyEndpoint]:
        """Find an endpoint by its formatted URL."""
        for ep in self._endpoints:
            if ep.formatted_url == proxy_url:
                return ep
        return None

    def _select_proxy(
        self,
        prefer_country: Optional[str] = None,
    ) -> Optional[ProxyEndpoint]:
        """Select the best available proxy."""
        now = time.monotonic()
        available = [
            ep for ep in self._endpoints
            if ep.is_healthy
            and now > ep.cooldown_until
            and self._concurrent_usage.get(ep.formatted_url, 0) < self.config.max_concurrent_per_proxy
        ]

        if not available:
            # Try including cooldown proxies if nothing else
            available = [
                ep for ep in self._endpoints
                if self._concurrent_usage.get(ep.formatted_url, 0) < self.config.max_concurrent_per_proxy
            ]
            if not available:
                logger.error("ProxyManager: no available proxies")
                return None

        # Prefer residential
        if self.config.prefer_residential:
            residential = [ep for ep in available if ep.is_residential]
            if residential:
                available = residential

        # Prefer same country
        if prefer_country and self.config.prefer_same_country:
            same_country = [ep for ep in available if ep.country == prefer_country]
            if same_country:
                available = same_country

        strategy = self.config.proxy_selection_strategy

        if strategy == "random":
            return random.choice(available)

        if strategy == "round_robin":
            selected = available[self._round_robin_index % len(available)]
            self._round_robin_index += 1
            return selected

        if strategy == "least_used":
            return min(available, key=lambda ep: ep.use_count or 0)

        if strategy == "lowest_latency":
            latency_known = [ep for ep in available if ep.last_latency_ms > 0]
            if latency_known:
                return min(latency_known, key=lambda ep: ep.last_latency_ms)
            return random.choice(available)

        return random.choice(available)

    @property
    def status(self) -> Dict[str, Any]:
        """Current pool status."""
        healthy = sum(1 for ep in self._endpoints if ep.is_healthy)
        return {
            "total": len(self._endpoints),
            "healthy": healthy,
            "unhealthy": len(self._endpoints) - healthy,
            "sticky_sessions": len(self._sticky_sessions),
            "in_use": sum(self._concurrent_usage.values()),
        }

    async def health_check_all(self) -> Dict[str, bool]:
        """Run health checks on all endpoints."""
        try:
            import aiohttp
        except ImportError:
            logger.warning("ProxyManager: aiohttp not installed, skipping health checks")
            return {}

        results = {}
        for endpoint in self._endpoints:
            try:
                start = time.monotonic()
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        self.config.health_check_url,
                        proxy=endpoint.formatted_url,
                        timeout=aiohttp.ClientTimeout(total=self.config.health_check_timeout_seconds),
                        ssl=False,
                    ) as resp:
                        endpoint.last_latency_ms = (time.monotonic() - start) * 1000
                        endpoint.is_healthy = resp.status < 500
                        endpoint.last_check = time.monotonic()
                        results[endpoint.display_url] = endpoint.is_healthy
            except Exception as exc:
                endpoint.is_healthy = False
                endpoint.last_check = time.monotonic()
                results[endpoint.display_url] = False
                logger.debug(f"ProxyManager: health check failed for {endpoint.display_url}: {exc}")

        logger.info(f"ProxyManager: health check complete - {sum(results.values())}/{len(results)} healthy")
        return results


# Global singleton
_proxy_manager: Optional[AdvancedProxyManager] = None


def get_advanced_proxy_manager() -> Optional[AdvancedProxyManager]:
    return _proxy_manager


def init_advanced_proxy_manager(
    proxy_urls: Optional[List[str]] = None,
    config: Optional[ProxyManagerConfig] = None,
) -> AdvancedProxyManager:
    global _proxy_manager
    _proxy_manager = AdvancedProxyManager(proxies=proxy_urls, config=config)
    return _proxy_manager
