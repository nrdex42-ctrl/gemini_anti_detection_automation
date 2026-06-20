"""Checkpoint detection, taxonomy, and recovery routing.

Every HTTP response passes through the CheckpointDetector before being returned
to the caller. The detector inspects the status code, Location header on
redirects, and the response body for known checkpoint signatures.

Checkpoint types:
  - soft:           302 → /checkpoint/1501091/ (identity verify)
  - hard:           302 → /security_check/ (photo verify)
  - device_verify:  302 → /login/device-based/ (device approval)
  - cookie_consent: 302 → /cookie/consent/ (cookie consent)
  - disabled:       302 → /disabled/ (account disabled)
  - shadow:         200 OK but post invisible to others (detected via verification)
  - restricted:     200 OK + body pattern (30-day restriction)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class CheckpointEncountered(Exception):
    """Raised when a response matches a known checkpoint signature."""
    kind: str = "unknown"
    redirect_url: str = ""
    body_snippet: str = ""
    account_id: str = ""

    def __str__(self):
        parts = [f"CheckpointEncountered(kind={self.kind}"]
        if self.redirect_url:
            parts.append(f", url={self.redirect_url}")
        if self.account_id:
            parts.append(f", account={self.account_id}")
        parts.append(")")
        return "".join(parts)


# Redirect URL patterns: (regex, kind, severity)
REDIRECT_PATTERNS: List[Tuple[str, str, str]] = [
    (r"/checkpoint/1501091/", "soft", "medium"),
    (r"/checkpoint/(\d+)/", "soft", "medium"),
    (r"/security_check/", "hard", "high"),
    (r"/login/device-based/regular/login/", "device_verify", "low"),
    (r"/cookie/consent/", "cookie_consent", "trivial"),
    (r"/disabled/", "disabled", "terminal"),
    (r"/account/disabled/", "disabled", "terminal"),
]

# Body patterns — only checked on 200 responses (silent signals)
BODY_PATTERNS: List[Tuple[str, str, str]] = [
    (r"verify your identity", "soft", "medium"),
    (r"upload a photo of yourself", "hard", "high"),
    (r"your account has been restricted", "restricted", "critical"),
    (r"we've restricted", "restricted", "critical"),
    (r"temporarily blocked", "restricted", "critical"),
    (r"confirm your identity", "soft", "medium"),
    (r"security check", "hard", "high"),
    (r"enter login code", "device_verify", "low"),
    (r"suspicious activity", "hard", "high"),
    (r"unusual login", "soft", "medium"),
]

# Checkpoint severity levels for alert routing
SEVERITY_WEIGHT = {
    "trivial": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
    "terminal": 5,
}


class CheckpointDetector:
    """Inspects every response for checkpoint signals.

    Wired into FBClient.request() as a post-response hook. If a signal matches,
    raises CheckpointEncountered immediately.

    Usage::

        detector = CheckpointDetector(account_id="123")
        detector.inspect(status_code=302, headers={"location": "/checkpoint/..."}, body="")
    """

    def __init__(self, account_id: str):
        self.account_id = account_id
        self._compiled_redirect = [
            (re.compile(p, re.IGNORECASE), k, s)
            for p, k, s in REDIRECT_PATTERNS
        ]
        self._compiled_body = [
            (re.compile(p, re.IGNORECASE), k, s)
            for p, k, s in BODY_PATTERNS
        ]

    def inspect(
        self,
        status_code: int,
        headers: Optional[Dict[str, str]] = None,
        body: str = "",
    ) -> None:
        """Raise CheckpointEncountered if response matches a known pattern."""
        headers = headers or {}

        # 1. Check redirect status codes
        if status_code in (301, 302, 303, 307, 308):
            location = headers.get("location", "") or headers.get("Location", "")
            for pattern, kind, severity in self._compiled_redirect:
                if pattern.search(location):
                    logger.error(
                        "Checkpoint detected (redirect) for account %s: "
                        "kind=%s severity=%s location=%s",
                        self.account_id, kind, severity, location,
                    )
                    raise CheckpointEncountered(
                        kind=kind,
                        redirect_url=location,
                        account_id=self.account_id,
                    )

        # 2. Check login page detection (200 with login form)
        if status_code == 200:
            body_lower = body[:51200].lower() if body else ""
            if "/login" in body_lower and ("login_form" in body_lower or "id_token" in body_lower):
                # This usually means the session expired and user was redirected
                # to a login page that returns 200 with the login form JS
                if "facebook" in body_lower:
                    logger.error(
                        "Checkpoint detected (login form in 200 body) for account %s",
                        self.account_id,
                    )
                    raise CheckpointEncountered(
                        kind="session_expired",
                        body_snippet=body[:500],
                        account_id=self.account_id,
                    )

        # 3. Check body for silent signals (200 responses with checkpoint content)
        if status_code == 200 and body:
            body_snippet = body[:51200]
            for pattern, kind, severity in self._compiled_body:
                if pattern.search(body_snippet):
                    logger.error(
                        "Checkpoint detected (body) for account %s: "
                        "kind=%s severity=%s",
                        self.account_id, kind, severity,
                    )
                    raise CheckpointEncountered(
                        kind=kind,
                        body_snippet=body_snippet[:500],
                        account_id=self.account_id,
                    )

        # 4. Detect suspicious empty responses (sometimes indicative of blocks)
        if status_code == 200 and len(body or "") < 50 and "for(;;)" not in (body or ""):
            # This COULD be a block - log it but don't raise
            logger.warning(
                "Suspiciously short 200 response for account %s (%d bytes)",
                self.account_id, len(body or ""),
            )


async def handle_checkpoint(
    exc: CheckpointEncountered,
    redis_client: Any,
    account_id: str,
) -> str:
    """Route account to appropriate recovery state based on checkpoint type.

    Returns the recovery state name.
    """
    kind = exc.kind

    recovery_map = {
        "cookie_consent": "cookie_consent_pending",
        "device_verify": "device_verify_pending",
        "soft": "soft_checkpoint_pending",
        "hard": "hard_checkpoint_pending",
        "session_expired": "session_expired",
        "restricted": "restricted",
        "disabled": "disabled",
        "shadow": "shadow_cooldown",
    }

    state = recovery_map.get(kind, "quarantined")
    ttl_map = {
        "cookie_consent": 3600,
        "device_verify": 86400,
        "soft": 86400,
        "hard": 259200,
        "session_expired": 7200,
        "restricted": 2592000,
        "disabled": 0,
        "shadow": 259200,
    }

    ttl = ttl_map.get(kind, 86400)

    if redis_client is not None:
        try:
            payload = {
                "account_id": account_id,
                "kind": kind,
                "state": state,
                "redirect_url": exc.redirect_url,
                "body_snippet": exc.body_snippet[:500],
                "timestamp": __import__("time").time(),
            }
            import json
            if ttl > 0:
                await redis_client.setex(
                    f"checkpoint:{account_id}",
                    ttl,
                    json.dumps(payload),
                )
            else:
                await redis_client.set(f"checkpoint:{account_id}", json.dumps(payload))
            await redis_client.publish("checkpoint_events", json.dumps(payload))
        except Exception as e:
            logger.error("Failed to persist checkpoint state: %s", e)

    logger.error(
        "Checkpoint handled for account %s: kind=%s -> state=%s (ttl=%ds)",
        account_id, kind, state, ttl,
    )
    return state
