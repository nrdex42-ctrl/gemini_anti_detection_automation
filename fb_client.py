"""FBClient — Per-account HTTP client wrapping curl_cffi for TLS fingerprint mimicry.

Each FBClient owns:
  - A curl_cffi.AsyncSession impersonating a specific Chrome version
  - A consistent fingerprint profile (User-Agent, sec-ch-ua, JA3 locked together)
  - A proxy URL (sticky per account)

The curl_cffi library wraps libcurl-impersonate, which patches BoringSSL to
reproduce Chrome's exact TLS ClientHello (JA3/JA4), HTTP/2 pseud-header order,
and connection behavior. This is the load-bearing component of the anti-detection
stack — without it, Python's aiohttp/requests/urllib all produce a distinct
Python TLS fingerprint that Facebook can detect.

Usage::

    client = FBClient(
        account_id="123456",
        fingerprint_profile={
            "impersonate": "chrome120",
            "user_agent": "Mozilla/5.0 ... Chrome/120.0.0.0 Safari/537.36",
            "sec_ch_ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            "sec_ch_ua_platform": '"Windows"',
            "sec_ch_ua_arch": '"x86"',
            "sec_ch_ua_bitness": '"64"',
            "sec_ch_ua_full_version_list": '"Not_A Brand";v="8.0.0.0", "Chromium";v="120.0.0.0", "Google Chrome";v="120.0.0.0"',
            "sec_ch_ua_platform_version": '"15.0.0"',
            "sec_ch_ua_model": '""',
            "sec_ch_ua_mobile": "?0",
        },
        proxy_url="http://user:pass@1.2.3.4:8080",
    )
    await client.start()

    resp = await client.post("https://www.facebook.com/api/graphql/", data=payload, headers=headers)
    # Response is automatically inspected for checkpoint signals

    await client.stop()
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .checkpoint import CheckpointDetector
    from .cookie_jar import FBCookieJar

logger = logging.getLogger(__name__)


@dataclass
class FingerprintProfile:
    """Atomic fingerprint profile — all fields must be consistent with each other.

    Never mix a Chrome 120 User-Agent with Chrome 116 sec-ch-ua. Every dimension
    (JA3, User-Agent, sec-ch-ua family, Accept-Language, Accept-Encoding) is
    version-locked.
    """
    impersonate: str = "chrome120"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    sec_ch_ua: str = '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"'
    sec_ch_ua_mobile: str = "?0"
    sec_ch_ua_platform: str = '"Windows"'
    sec_ch_ua_arch: str = '"x86"'
    sec_ch_ua_bitness: str = '"64"'
    sec_ch_ua_full_version_list: str = (
        '"Not_A Brand";v="8.0.0.0", "Chromium";v="120.0.0.0", "Google Chrome";v="120.0.0.0"'
    )
    sec_ch_ua_platform_version: str = '"15.0.0"'
    sec_ch_ua_model: str = '""'
    screen_width: int = 1920
    screen_height: int = 1080
    color_depth: int = 24
    pixel_ratio: float = 1.0
    locale: str = "en-US"
    timezone: str = "America/New_York"
    platform: str = "Windows"

    @classmethod
    def chrome120(cls) -> "FingerprintProfile":
        return cls()

    @classmethod
    def chrome124(cls) -> "FingerprintProfile":
        return cls(
            impersonate="chrome124",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            sec_ch_ua=(
                '"Not_A Brand";v="8", "Chromium";v="124", "Google Chrome";v="124"'
            ),
            sec_ch_ua_full_version_list=(
                '"Not_A Brand";v="8.0.0.0", "Chromium";v="124.0.0.0", "Google Chrome";v="124.0.0.0"'
            ),
        )


class FBClient:
    """Per-account HTTP client wrapping curl_cffi with TLS fingerprint mimicry.

    Owns the curl_cffi session, fingerprint profile, proxy assignment, and
    optionally an FBCookieJar. Every response is automatically inspected for
    both checkpoint signals and xs cookie rotation.

    Exactly one FBClient should exist per account worker.
    """

    def __init__(
        self,
        account_id: str,
        fingerprint_profile: Optional[Dict[str, Any]] = None,
        proxy_url: str = "",
        jar: Optional[Any] = None,
        on_jar_dirty: Optional[Any] = None,
    ):
        self.account_id = account_id
        self.proxy_url = proxy_url
        self.jar = jar
        self.on_jar_dirty = on_jar_dirty
        self._started = False
        self._session = None
        self._lock = asyncio.Lock()
        self._requests_in_flight = 0
        self._total_requests = 0
        self._total_failures = 0

        if fingerprint_profile:
            self.fp = FingerprintProfile(**{
                k: v for k, v in fingerprint_profile.items()
                if k in FingerprintProfile.__dataclass_fields__
            })
        else:
            self.fp = FingerprintProfile.chrome120()

        from .checkpoint import CheckpointDetector as _CD
        self.checkpoint_detector = _CD(account_id)

    async def start(self):
        """Create the curl_cffi session with impersonation."""
        if self._started:
            return
        try:
            from curl_cffi.requests import AsyncSession
        except ImportError:
            logger.error(
                "curl_cffi is not installed. Install with: pip install curl-cffi"
            )
            raise

        kwargs: Dict[str, Any] = {
            "impersonate": self.fp.impersonate,
        }
        if self.proxy_url:
            kwargs["proxy"] = self.proxy_url

        self._session = AsyncSession(**kwargs)
        self._started = True
        logger.info(
            "FBClient started for account %s (impersonate=%s, proxy=%s)",
            self.account_id, self.fp.impersonate,
            bool(self.proxy_url),
        )

    async def stop(self):
        """Close the curl_cffi session."""
        if not self._started or self._session is None:
            return
        try:
            await self._session.close()
        except Exception as exc:
            logger.debug("FBClient session close error: %s", exc)
        self._started = False
        self._session = None
        logger.info("FBClient stopped for account %s", self.account_id)

    @property
    def session(self):
        if not self._started or self._session is None:
            raise RuntimeError("FBClient not started. Call await client.start() first.")
        return self._session

    def build_base_headers(self, cookies: Optional[str] = None) -> Dict[str, str]:
        """Build the consistent set of Chrome-matching headers.

        These headers are sent with every request. Callers add request-specific
        headers (Content-Type, Content-Length, Origin, Referer, etc.).
        """
        headers = {
            "sec-ch-ua": self.fp.sec_ch_ua,
            "sec-ch-ua-mobile": self.fp.sec_ch_ua_mobile,
            "sec-ch-ua-platform": self.fp.sec_ch_ua_platform,
            "sec-ch-ua-arch": self.fp.sec_ch_ua_arch,
            "sec-ch-ua-bitness": self.fp.sec_ch_ua_bitness,
            "sec-ch-ua-full-version-list": self.fp.sec_ch_ua_full_version_list,
            "sec-ch-ua-platform-version": self.fp.sec_ch_ua_platform_version,
            "sec-ch-ua-model": self.fp.sec_ch_ua_model,
            "user-agent": self.fp.user_agent,
            "accept": "*/*",
            "accept-encoding": "gzip, deflate, br",
            "accept-language": "en-US,en;q=0.9",
        }
        if cookies:
            headers["cookie"] = cookies
        return headers

    async def request(
        self,
        method: str,
        url: str,
        *,
        data: Any = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 30.0,
        **kwargs,
    ) -> Tuple[int, str, Dict[str, str]]:
        """Send an HTTP request via curl_cffi with TLS impersonation.

        Returns (status_code, body_text, response_headers).

        Every response is automatically inspected for checkpoint signals.
        """
        if not self._started:
            await self.start()

        start = time.monotonic()
        self._requests_in_flight += 1

        try:
            full_headers = dict(self.build_base_headers())
            if headers:
                full_headers.update(headers)

            resp = await self.session.request(
                method,
                url,
                data=data,
                headers=full_headers,
                timeout=timeout,
                **kwargs,
            )

            status = resp.status_code
            body = resp.text
            resp_headers = dict(resp.headers) if hasattr(resp, "headers") else {}

            self._total_requests += 1

            if status >= 400:
                self._total_failures += 1

            # xs rotation capture — must happen BEFORE checkpoint detection
            # because a checkpoint response may still carry a rotated xs
            if self.jar is not None:
                try:
                    rotated = self.jar.extract_xs_rotation(resp_headers)
                    if rotated and self.on_jar_dirty:
                        await self.on_jar_dirty(self.jar)
                except Exception as xse:
                    logger.debug("xs rotation capture failed: %s", xse)

            # Checkpoint detection on every response
            try:
                self.checkpoint_detector.inspect(
                    status_code=status,
                    headers=resp_headers,
                    body=body,
                )
            except Exception as cpe:
                logger.error(
                    "Checkpoint detected for account %s: %s",
                    self.account_id, cpe,
                )
                raise

            latency = time.monotonic() - start
            if status >= 400:
                logger.warning(
                    "FBClient request %s %s -> %d (%.2fs)",
                    method, url, status, latency,
                )

            return status, body, resp_headers

        except Exception:
            self._total_failures += 1
            raise
        finally:
            self._requests_in_flight -= 1

    async def get(
        self, url: str, **kwargs,
    ) -> Tuple[int, str, Dict[str, str]]:
        return await self.request("GET", url, **kwargs)

    async def post(
        self,
        url: str,
        *,
        data: Any = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs,
    ) -> Tuple[int, str, Dict[str, str]]:
        return await self.request("POST", url, data=data, headers=headers, **kwargs)

    @property
    def stats(self) -> Dict[str, Any]:
        return {
            "account_id": self.account_id,
            "started": self._started,
            "requests_total": self._total_requests,
            "requests_failed": self._total_failures,
            "requests_in_flight": self._requests_in_flight,
            "impersonate": self.fp.impersonate,
            "proxy": bool(self.proxy_url),
        }


class FBClientPool:
    """Pool of per-account FBClient instances.

    Ensures one FBClient per account and provides lazy initialization.
    """

    def __init__(self):
        self._clients: Dict[str, FBClient] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(
        self,
        account_id: str,
        fingerprint_profile: Optional[Dict[str, Any]] = None,
        proxy_url: str = "",
    ) -> FBClient:
        async with self._lock:
            if account_id not in self._clients:
                client = FBClient(
                    account_id=account_id,
                    fingerprint_profile=fingerprint_profile,
                    proxy_url=proxy_url,
                )
                await client.start()
                self._clients[account_id] = client
            return self._clients[account_id]

    async def remove(self, account_id: str):
        async with self._lock:
            client = self._clients.pop(account_id, None)
            if client:
                await client.stop()

    async def stop_all(self):
        async with self._lock:
            for client in self._clients.values():
                try:
                    await client.stop()
                except Exception as exc:
                    logger.debug("Error stopping client: %s", exc)
            self._clients.clear()
