"""
HeaderForge - Precise Chrome header spoofing.

Generates HTTP headers that exactly match a real Chrome browser's
header order, casing, and values. Header order matters because
some fingerprinting services use it as a signal.

This is the advanced implementation. The existing stub in network.py
is preserved for backward compatibility.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Chrome sends headers in this EXACT order (lowercased for comparison,
# but we preserve Chrome's original casing in output)
CHROME_HEADER_ORDER: List[str] = [
    "host",
    "connection",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "upgrade-insecure-requests",
    "user-agent",
    "accept",
    "sec-fetch-site",
    "sec-fetch-mode",
    "sec-fetch-user",
    "sec-fetch-dest",
    "accept-encoding",
    "accept-language",
    "cookie",
]

# Chrome's header casing (headers are sent with this exact capitalization)
CHROME_HEADER_CASING: Dict[str, str] = {
    "host": "Host",
    "connection": "Connection",
    "sec-ch-ua": "sec-ch-ua",
    "sec-ch-ua-mobile": "sec-ch-ua-mobile",
    "sec-ch-ua-platform": "sec-ch-ua-platform",
    "upgrade-insecure-requests": "Upgrade-Insecure-Requests",
    "user-agent": "User-Agent",
    "accept": "Accept",
    "sec-fetch-site": "Sec-Fetch-Site",
    "sec-fetch-mode": "Sec-Fetch-Mode",
    "sec-fetch-user": "Sec-Fetch-User",
    "sec-fetch-dest": "Sec-Fetch-Dest",
    "accept-encoding": "Accept-Encoding",
    "accept-language": "Accept-Language",
    "cookie": "Cookie",
}

# XHR/Fetch request headers (different order from navigation)
CHROME_XHR_HEADER_ORDER: List[str] = [
    "host",
    "connection",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "content-length",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
    "user-agent",
    "content-type",
    "accept",
    "x-fb-friendly-name",
    "x-fb-lsd",
    "origin",
    "referer",
    "accept-encoding",
    "accept-language",
    "cookie",
]


@dataclass
class ChromeVersionIdentity:
    """Complete Chrome version fingerprint."""
    major: int
    minor: int
    build: int
    patch: int
    full_version: str
    sec_ch_ua: str

    @classmethod
    def from_version_string(cls, version: str) -> "ChromeVersionIdentity":
        parts = version.split(".")
        major = int(parts[0]) if len(parts) > 0 else 120
        minor = int(parts[1]) if len(parts) > 1 else 0
        build = int(parts[2]) if len(parts) > 2 else 0
        patch = int(parts[3]) if len(parts) > 3 else 0

        return cls(
            major=major,
            minor=minor,
            build=build,
            patch=patch,
            full_version=version,
            sec_ch_ua=(
                f'"Not_A Brand";v="8", "Chromium";v="{major}", '
                f'"Google Chrome";v="{major}"'
            ),
        )

    @property
    def full_user_agent(self) -> str:
        return (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{self.full_version} Safari/537.36"
        )


# Accept-Language values observed in real Chrome traffic
ACCEPT_LANGUAGE_OPTIONS = [
    "en-US,en;q=0.9",
    "en-US,en;q=0.9,ar;q=0.8",
    "en-US,en;q=0.9,ar;q=0.7",
    "en-US,en;q=0.8",
    "en-GB,en-US;q=0.9,en;q=0.8",
    "en-US,en;q=0.9,fr;q=0.8",
    "en-US,en;q=0.9,es;q=0.8",
]

# Accept-Encoding values (Chrome varies this slightly)
ACCEPT_ENCODING_OPTIONS = [
    "gzip, deflate, br",
    "gzip, deflate, br, zstd",
]


@dataclass
class HeaderForgeConfig:
    """Configuration for header generation."""
    randomize_accept_language: bool = True
    randomize_accept_encoding: bool = False
    preserve_header_order: bool = True
    preserve_header_casing: bool = True
    include_dpr_header: bool = False
    viewport_width: int = 1280
    viewport_height: int = 900
    device_pixel_ratio: float = 1.0


class AdvancedHeaderForge:
    """
    Generates HTTP headers that match real Chrome browser behavior.

    Key details that fingerprinting services check:
    1. Header ORDER (Chrome has a fixed order)
    2. Header CASING (Chrome uses specific capitalization)
    3. Header VALUES (exact Chrome format)
    4. sec-ch-ua CLIENT HINTS (must match User-Agent version)

    Named AdvancedHeaderForge to avoid collision with the existing
    stub HeaderForge in network.py.
    """

    def __init__(
        self,
        chrome_version: str = "120.0.0.0",
        config: Optional[HeaderForgeConfig] = None,
    ):
        self.identity = ChromeVersionIdentity.from_version_string(chrome_version)
        self.config = config or HeaderForgeConfig()
        self._frozen_accept_language: Optional[str] = None
        self._frozen_accept_encoding: Optional[str] = None

    def freeze_random_fields(self):
        """Lock in random header values for a session."""
        self._frozen_accept_language = self._pick_accept_language()
        self._frozen_accept_encoding = self._pick_accept_encoding()

    def _pick_accept_language(self) -> str:
        if self._frozen_accept_language:
            return self._frozen_accept_language
        if self.config.randomize_accept_language:
            return random.choice(ACCEPT_LANGUAGE_OPTIONS)
        return "en-US,en;q=0.9"

    def _pick_accept_encoding(self) -> str:
        if self._frozen_accept_encoding:
            return self._frozen_accept_encoding
        if self.config.randomize_accept_encoding:
            return random.choice(ACCEPT_ENCODING_OPTIONS)
        return "gzip, deflate, br"

    def build_navigation_headers(
        self,
        host: str,
        cookies: Optional[str] = None,
        extra: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        """
        Build headers for a top-level navigation request (typing URL, clicking link).

        These have a different structure than XHR/Fetch requests.
        """
        headers: Dict[str, str] = {}

        # Build in Chrome's exact order
        for key in CHROME_HEADER_ORDER:
            value = self._get_navigation_header_value(key, host, cookies)
            if value is not None:
                cased_key = CHROME_HEADER_CASING.get(key, key) if self.config.preserve_header_casing else key
                headers[cased_key] = value

        if extra:
            headers.update(extra)

        return headers

    def build_xhr_headers(
        self,
        host: str,
        origin: str,
        referer: str,
        content_type: str = "application/x-www-form-urlencoded",
        content_length: int = 0,
        cookies: Optional[str] = None,
        fb_friendly_name: Optional[str] = None,
        fb_lsd: Optional[str] = None,
        extra: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        """
        Build headers for an XHR/Fetch request (GraphQL calls, AJAX).

        These have a DIFFERENT order than navigation requests.
        """
        headers: Dict[str, str] = {}

        xhr_value_map = {
            "host": host,
            "connection": "keep-alive",
            "sec-ch-ua": self.identity.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "content-length": str(content_length),
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": self.identity.full_user_agent,
            "content-type": content_type,
            "accept": "*/*",
            "origin": origin,
            "referer": referer,
            "accept-encoding": self._pick_accept_encoding(),
            "accept-language": self._pick_accept_language(),
        }

        if fb_friendly_name:
            xhr_value_map["x-fb-friendly-name"] = fb_friendly_name
        if fb_lsd:
            xhr_value_map["x-fb-lsd"] = fb_lsd
        if cookies:
            xhr_value_map["cookie"] = cookies

        # Build in XHR order
        for key in CHROME_XHR_HEADER_ORDER:
            value = xhr_value_map.get(key)
            if value is not None:
                headers[key] = value

        if extra:
            headers.update(extra)

        return headers

    def _get_navigation_header_value(
        self,
        key: str,
        host: str,
        cookies: Optional[str],
    ) -> Optional[str]:
        value_map = {
            "host": host,
            "connection": "keep-alive",
            "sec-ch-ua": self.identity.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "upgrade-insecure-requests": "1",
            "user-agent": self.identity.full_user_agent,
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "sec-fetch-site": "none",
            "sec-fetch-mode": "navigate",
            "sec-fetch-user": "?1",
            "sec-fetch-dest": "document",
            "accept-encoding": self._pick_accept_encoding(),
            "accept-language": self._pick_accept_language(),
        }
        if cookies:
            value_map["cookie"] = cookies
        return value_map.get(key)
