"""FBCookieJar — Canonical cookie jar for Facebook sessions.

Persisted to Redis/Postgres, encrypted at rest. The `dirty` flag triggers a
flush-back to the store on any change. Every HTTP response must be inspected
for a Set-Cookie: xs=... header (xs rotation is silent).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class FBCookieJar:
    """Canonical serialized representation of a Facebook session.

    The `secret_meta` field holds volatile tokens (dtsg_token, lsd) that rotate
    ~6h — cached here for hot-path access, but source of truth is the live
    bootstrap HTML.
    """
    account_id: str
    datr: str = ""
    xs: str = ""
    c_user: str = ""
    fr: Optional[str] = None
    sb: Optional[str] = None
    wd: str = "1920x946"
    dpr: str = "1.25"
    m_pixel_ratio: str = "1.25"
    locale: str = "en_US"
    presence: str = ""

    dtsg_token: Optional[str] = None
    lsd: Optional[str] = None

    issued_at: float = field(default_factory=time.time)
    last_validated_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    validation_failures: int = 0
    source: str = "imported"

    fingerprint_profile_id: str = "chrome_120_default"

    dirty: bool = False

    def to_cookie_header(self) -> str:
        pairs = [
            ("datr", self.datr),
            ("xs", self.xs),
            ("c_user", self.c_user),
            ("fr", self.fr or ""),
            ("sb", self.sb or ""),
            ("wd", self.wd),
            ("dpr", self.dpr),
            ("m_pixel_ratio", self.m_pixel_ratio),
            ("locale", self.locale),
            ("presence", self.presence),
        ]
        return "; ".join(f"{k}={v}" for k, v in pairs if v)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def is_stale(self, max_age_seconds: int = 19800) -> bool:
        """True if dtsg/lsd haven't been refreshed in ~5.5h."""
        return (time.time() - self.last_validated_at) > max_age_seconds

    def extract_xs_rotation(self, response_headers: Dict[str, str]) -> Optional[str]:
        """Inspect response headers for a rotated xs cookie.

        Called after EVERY HTTP response. If xs has rotated, mark dirty so the
        caller persists immediately.
        """
        set_cookie = response_headers.get("set-cookie", "")
        if not set_cookie:
            return None
        m = re.search(r"xs=([^;]+);", set_cookie)
        if m:
            new_xs = m.group(1)
            if new_xs != self.xs:
                logger.info(
                    "xs rotated for account %s: %s... -> %s...",
                    self.account_id, self.xs[:8], new_xs[:8],
                )
                self.xs = new_xs
                self.dirty = True
                return new_xs
        return None
