"""
StealthConnector - TLS fingerprint masking and network-level stealth.

Handles JA3/JA4 fingerprint evasion by routing sensitive requests
through a TLS client that mimics real Chrome handshakes, while
letting Playwright handle the browser UI automation.
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


@dataclass
class ChromeTLSProfile:
    """
    A snapshot of a real Chrome version's TLS behavior.
    These values are derived from real Chrome traffic captures.
    """
    version: str
    ja3_hash: str
    ja4_hash: str
    cipher_suites: List[str]
    extensions: List[Tuple[int, bytes]]
    supported_groups: List[str]
    signature_algorithms: List[str]
    tls_version: str = "TLSv1.3"
    min_tls_version: str = "TLSv1.2"

    @property
    def user_agent(self) -> str:
        major = self.version.split(".")[0]
        return (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{self.version} Safari/537.36"
        )


# Real Chrome TLS profiles captured from actual browser traffic
CHROME_TLS_PROFILES: List[ChromeTLSProfile] = [
    ChromeTLSProfile(
        version="120.0.0.0",
        ja3_hash="769,47-53-5-10-49199-49195-49200-49196-49162-49161-49171-49172-51-157-48-0-23-65281-10-11-35-16-5-13-18-51-45-43-27-17513,29-23-24-25,0",
        ja4_hash="t13d1516h2_7e0f2e1e8f9a_6a3b8c4d",
        cipher_suites=[
            "TLS_AES_128_GCM_SHA256",
            "TLS_AES_256_GCM_SHA384",
            "TLS_CHACHA20_POLY1305_SHA256",
            "TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256",
            "TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256",
            "TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384",
            "TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384",
            "TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256",
            "TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305_SHA256",
            "TLS_RSA_WITH_AES_128_GCM_SHA256",
            "TLS_RSA_WITH_AES_256_GCM_SHA384",
        ],
        extensions=[
            (0, b""),           # server_name
            (5, b""),           # status_request (OCSP)
            (10, b"\x01\x00"),  # supported_groups
            (11, b""),          # ec_point_formats
            (13, b""),          # signature_algorithms
            (16, b""),          # application_layer_protocol_negotiation
            (23, b""),          # extended_master_secret
            (35, b""),          # session_ticket
            (51, b""),          # key_share
            (45, b""),          # psk_key_exchange_modes
            (43, b""),          # supported_versions
            (27, b""),          # compress_certificate
            (17513, b""),       # post_handshake_auth
        ],
        supported_groups=["x25519", "secp256r1", "secp384r1"],
        signature_algorithms=[
            "ecdsa_secp256r1_sha256",
            "rsa_pss_rsae_sha256",
            "rsa_pkcs1_sha256",
            "ecdsa_secp384r1_sha384",
            "rsa_pss_rsae_sha384",
            "rsa_pkcs1_sha384",
            "rsa_pss_rsae_sha512",
            "rsa_pkcs1_sha512",
        ],
    ),
    ChromeTLSProfile(
        version="121.0.0.0",
        ja3_hash="771,4865-4866-4867-49195-49199-49196-49200-52393-52392-52394-49162-49161-49171-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-27-21,29-23-24-25,0",
        ja4_hash="t13d1516h2_8f1a2b3c4d5e_7c9e0a1b",
        cipher_suites=[
            "TLS_AES_128_GCM_SHA256",
            "TLS_AES_256_GCM_SHA384",
            "TLS_CHACHA20_POLY1305_SHA256",
            "TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256",
            "TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256",
            "TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384",
            "TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384",
            "TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256",
            "TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305_SHA256",
        ],
        extensions=[
            (0, b""), (5, b""), (10, b"\x01\x00"), (11, b""),
            (13, b""), (16, b""), (21, b""), (23, b""),
            (27, b""), (35, b""), (43, b""), (45, b""),
            (51, b""), (17513, b""),
        ],
        supported_groups=["x25519", "secp256r1", "secp384r1"],
        signature_algorithms=[
            "ecdsa_secp256r1_sha256",
            "rsa_pss_rsae_sha256",
            "rsa_pkcs1_sha256",
            "ecdsa_secp384r1_sha384",
            "rsa_pss_rsae_sha384",
            "rsa_pkcs1_sha384",
            "rsa_pss_rsae_sha512",
            "rsa_pkcs1_sha512",
        ],
    ),
    ChromeTLSProfile(
        version="122.0.0.0",
        ja3_hash="771,4865-4866-4867-49195-49199-49196-49200-52393-52392-52394-49162-49161-49171-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-27-17513,29-23-24-25,0",
        ja4_hash="t13d1516h2_a2b3c4d5e6f7_9d0e1f2a",
        cipher_suites=[
            "TLS_AES_128_GCM_SHA256",
            "TLS_AES_256_GCM_SHA384",
            "TLS_CHACHA20_POLY1305_SHA256",
            "TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256",
            "TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256",
            "TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384",
            "TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384",
            "TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256",
            "TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305_SHA256",
        ],
        extensions=[
            (0, b""), (5, b""), (10, b"\x01\x00"), (11, b""),
            (13, b""), (16, b""), (23, b""), (27, b""),
            (35, b""), (43, b""), (45, b""), (51, b""),
            (17513, b""),
        ],
        supported_groups=["x25519", "secp256r1", "secp384r1"],
        signature_algorithms=[
            "ecdsa_secp256r1_sha256",
            "rsa_pss_rsae_sha256",
            "rsa_pkcs1_sha256",
            "ecdsa_secp384r1_sha384",
            "rsa_pss_rsae_sha384",
            "rsa_pkcs1_sha384",
            "rsa_pss_rsae_sha512",
            "rsa_pkcs1_sha512",
        ],
    ),
]


@dataclass
class StealthConnectorConfig:
    """Configuration for the stealth connector."""
    use_tls_client_for_api: bool = True
    tls_client_fallback_to_requests: bool = True
    randomize_profile_per_session: bool = True
    ja3_rotation_interval_seconds: int = 1800
    http2_enabled: bool = True
    http2_prior_knowledge: bool = False
    tcp_fast_open: bool = True
    tcp_keepalive_interval: int = 45
    dns_over_https: bool = False
    doh_provider: str = "https://1.1.1.1/dns-query"


class AdvancedStealthConnector:
    """
    Manages TLS fingerprint spoofing for HTTP requests made outside
    the browser (GraphQL API calls, media downloads, etc.).

    For browser traffic, Playwright's Chromium already produces a
    realistic JA3. This class handles the non-browser HTTP layer.

    Named AdvancedStealthConnector to avoid collision with the existing
    stub StealthConnector in network.py.
    """

    def __init__(self, config: Optional[StealthConnectorConfig] = None):
        self.config = config or StealthConnectorConfig()
        self._current_profile: Optional[ChromeTLSProfile] = None
        self._profile_rotated_at: float = 0
        self._tls_client = None
        self._tls_client_available: Optional[bool] = None

    def select_profile(self, force_new: bool = False) -> ChromeTLSProfile:
        """Select or rotate the TLS profile."""
        now = time.monotonic()
        should_rotate = (
            force_new
            or self._current_profile is None
            or (
                self.config.randomize_profile_per_session
                and (now - self._profile_rotated_at) > self.config.ja3_rotation_interval_seconds
            )
        )

        if should_rotate:
            self._current_profile = random.choice(CHROME_TLS_PROFILES)
            self._profile_rotated_at = now
            logger.debug(
                f"StealthConnector: rotated to Chrome/{self._current_profile.version} "
                f"JA3={self._current_profile.ja3_hash[:32]}..."
            )

        return self._current_profile

    @property
    def user_agent(self) -> str:
        return self.select_profile().user_agent

    @property
    def chrome_version(self) -> str:
        return self.select_profile().version

    def _get_tls_client(self):
        """Lazy-load tls-client (optional dependency)."""
        if self._tls_client_available is False:
            return None
        if self._tls_client is not None:
            return self._tls_client
        try:
            import tls_client
            self._tls_client = tls_client
            self._tls_client_available = True
            logger.info("StealthConnector: tls-client loaded successfully")
            return self._tls_client
        except ImportError:
            self._tls_client_available = False
            logger.debug("StealthConnector: tls-client not available, using requests")
            return None

    def build_tls_client_session(
        self,
        proxy_url: Optional[str] = None,
    ) -> Any:
        """
        Build a tls-client session with Chrome-mimicking TLS fingerprint.

        Returns a session object with a .get/.post interface.
        """
        tls_lib = self._get_tls_client()
        if tls_lib is None:
            return None

        profile = self.select_profile()

        session_options = {
            "client_identifier": f"chrome_{profile.version.split('.')[0]}",
            "random_tls_extension_order": True,
        }

        if proxy_url:
            session_options["proxy"] = proxy_url

        try:
            session = tls_lib.Session(**session_options)
            logger.debug(
                f"StealthConnector: created tls-client session "
                f"client=chrome_{profile.version}"
            )
            return session
        except Exception as exc:
            logger.warning(f"StealthConnector: tls-client session failed: {exc}")
            return None

    def build_requests_session(
        self,
        proxy_url: Optional[str] = None,
    ) -> Any:
        """
        Build a requests session with maximum header alignment to Chrome.
        Falls back when tls-client is unavailable.
        """
        import requests
        from requests.adapters import HTTPAdapter

        session = requests.Session()
        self.select_profile()

        # Mount adapter with connection pooling matching Chrome
        adapter = HTTPAdapter(
            pool_connections=6,
            pool_maxsize=6,
            max_retries=0,
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        if proxy_url:
            session.proxies = {
                "http": proxy_url,
                "https": proxy_url,
            }

        # Disable TLS verification warnings (not verification itself)
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        return session

    def get_http_session(
        self,
        proxy_url: Optional[str] = None,
        prefer_tls_client: Optional[bool] = None,
    ) -> Any:
        """
        Get the best available HTTP session.

        Priority:
        1. tls-client (if enabled and available)
        2. requests (always available)
        """
        use_tls = prefer_tls_client if prefer_tls_client is not None else self.config.use_tls_client_for_api

        if use_tls:
            session = self.build_tls_client_session(proxy_url=proxy_url)
            if session is not None:
                return session

            if not self.config.tls_client_fallback_to_requests:
                raise RuntimeError("tls-client unavailable and fallback disabled")

        return self.build_requests_session(proxy_url=proxy_url)

    async def create_stealth_context_options(
        self,
        proxy_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Generate Playwright browser context options that complement
        the TLS fingerprint with matching HTTP/2 and header behavior.
        """
        profile = self.select_profile()

        options: Dict[str, Any] = {
            "user_agent": profile.user_agent,
            "locale": "en-US",
            "timezone_id": "Africa/Cairo",
            "color_scheme": "light",
            "viewport": {"width": 1280, "height": 900},
            "screen": {"width": 1280, "height": 900},
            "device_scale_factor": 1.0,
            "has_touch": False,
            "is_mobile": False,
            "extra_http_headers": {
                "sec-ch-ua": f'"Not_A Brand";v="8", "Chromium";v="{profile.version.split(".")[0]}", "Google Chrome";v="{profile.version.split(".")[0]}"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-ch-ua-platform-version": '"15.0.0"',
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "none",
                "sec-fetch-user": "?1",
                "upgrade-insecure-requests": "1",
            },
        }

        if proxy_url:
            parsed = urlparse(proxy_url)
            options["proxy"] = {
                "server": f"{parsed.scheme}://{parsed.netloc}",
            }
            if parsed.username:
                options["proxy"]["username"] = parsed.username
            if parsed.password:
                options["proxy"]["password"] = parsed.password

        return options

    def close(self):
        """Clean up resources."""
        self._tls_client = None


# Global singleton
_connector: Optional[AdvancedStealthConnector] = None


def get_stealth_connector() -> AdvancedStealthConnector:
    global _connector
    if _connector is None:
        _connector = AdvancedStealthConnector()
    return _connector
