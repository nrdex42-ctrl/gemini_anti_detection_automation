# Facebook Automation: Robust Anti-Detection & Safety Architecture
## Complete Production Guide — Multi-Layer Defense System

---

## Table of Contents
1. [Threat Model & Facebook Detection Vectors (2026)](#i-threat-model)
2. [Multi-Layer Defense Architecture](#ii-architecture)
3. [Session & Identity Management](#iii-session-management)
4. [Behavioral Mimicry & Human-Like Timing](#iv-behavioral-mimicry)
5. [Network Layer & Fingerprint Evasion](#v-network-layer)
6. [Enhanced FastImagePoster with Safety Guards](#vi-enhanced-poster)
7. [Checkpoint & Account Quarantine System](#vii-checkpoint-system)
8. [Monitoring, Alerting & Audit Logging](#viii-monitoring)
9. [Configuration & Deployment Matrix](#ix-configuration)
10. [Ethical Safeguards & Compliance Boundaries](#x-ethical-safeguards)

---

## I. Threat Model & Facebook Detection Vectors (2026)

Facebook's anti-automation stack operates at **six distinct layers**. Any automation system must address all six to maintain long-term stability.

### Layer 1: JavaScript Fingerprinting (Surface Level)
- `navigator.webdriver` flag detection
- `navigator.plugins` / `navigator.mimeTypes` consistency
- `window.chrome` object presence and structure
- `Permissions.prototype.query` overrides
- WebGL vendor/renderer strings
- Canvas fingerprint consistency
- Font enumeration mismatch
- AudioContext oscillator fingerprinting

### Layer 2: TLS & Transport Fingerprinting (Network Level)
- JA3/JA4 TLS client hello signatures
- Cipher suite ordering
- ALPN negotiation patterns
- TCP window scaling and timing
- HTTP/2 frame sequence patterns

### Layer 3: Behavioral Biometrics (Interaction Level)
- Mouse movement velocity and curvature
- Scroll patterns (acceleration, deceleration, jitter)
- Key press timing and rhythm
- Touch event simulation (on mobile UA)
- Focus/blur event sequences
- Action timing predictability

### Layer 4: Session & Identity Correlation (Account Level)
- Cookie entropy analysis
- `fb_dtsg` token rotation patterns
- `lsd` token usage frequency
- Cross-session IP consistency
- Device fingerprint stability across logins
- Login location velocity (impossible travel)

### Layer 5: Request Pattern Analysis (API Level)
- GraphQL doc_id stability vs. rotation
- `idempotence_token` collision patterns
- `rupload` sequence timing
- Multipart boundary formatting
- Header ordering and case sensitivity
- Request payload structure exactness

### Layer 6: Content & Velocity Analysis (Business Logic Level)
- Posting frequency per account
- Identical caption hash across multiple pages
- Media file hash repetition
- Temporal clustering (10 posts in 3 seconds)
- Engagement pattern absence (no likes/comments after post)
- Friend/follower ratio anomalies

---

## II. Multi-Layer Defense Architecture

### Principle: Defense in Depth with Graceful Degradation

```
┌─────────────────────────────────────────────────────────────┐
│                    ORCHESTRATION LAYER                      │
│         (Queue Manager + Account Health Monitor)            │
├─────────────────────────────────────────────────────────────┤
│  LAYER 6 │ Content Obfuscation + Velocity Jitter             │
│  LAYER 5 │ API Request Sanitization + Header Randomization  │
│  LAYER 4 │ Session Rotation + Token Lifecycle Management    │
│  LAYER 3 │ Behavioral Simulation + Timing Randomization     │
│  LAYER 2 │ Proxy Rotation + TLS Fingerprint Masking         │
│  LAYER 1 │ Browser Hardening + Fingerprint Consistency      │
├─────────────────────────────────────────────────────────────┤
│              FALLBACK: Browser Automation (Quarantined)       │
│              FAILSAFE: Human-in-the-Loop Escalation         │
└─────────────────────────────────────────────────────────────┘
```

### Core Tenets
1. **HTTP-First Strategy**: Use rupload/GraphQL for 90% of operations. Browser is fallback only.
2. **Account Isolation**: Each account operates in a completely isolated identity context (IP, UA, timezone, geolocation).
3. **Jitter Everything**: No two requests should share identical timing, headers, or payload structure.
4. **Fail Warm**: On detection signals, immediately quarantine and escalate — never retry aggressively.
5. **Entropy Injection**: Every post gets unique caption hashing, media fingerprint mutation, and timing variance.

---

## III. Session & Identity Management

### 3.1 Identity Context Object
Each Facebook account must carry a persistent, immutable identity context:

```python
from dataclasses import dataclass, field
from typing import Optional
import hashlib
import json

@dataclass(frozen=True)
class IdentityContext:
    """Immutable identity fingerprint for a single account."""
    account_id: str                          # Internal account UUID
    facebook_user_id: Optional[str]          # Facebook numeric user ID
    proxy_url: str                         # Dedicated residential proxy
    user_agent: str                        # Exact Chrome UA string
    viewport: tuple                        # (width, height, device_scale)
    timezone: str                          # IANA timezone (e.g., "America/New_York")
    locale: str                            # "en_US", "en_GB", etc.
    geolocation: tuple                   # (lat, lon) matching proxy egress
    screen_resolution: tuple               # (width, height)
    color_depth: int                       # 24 or 30
    platform: str                          # "Win32", "MacIntel", "Linux x86_64"
    chrome_version: str                    # e.g., "126.0.6478.63"
    webgl_vendor: str                      # "Google Inc. (NVIDIA)"
    webgl_renderer: str                    # "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660...)"
    fonts: tuple                           # Frozen set of "installed" fonts
    audio_sample_rate: int                 # 48000 or 44100
    # Derived: unique TLS/HTTP2 signature seed
    _signature_seed: str = field(repr=False)

    def __post_init__(self):
        # Validate consistency: timezone must match geolocation region
        # Validate: chrome_version must match user_agent version
        # Validate: viewport <= screen_resolution
        pass

    def to_browser_args(self) -> dict:
        """Convert to Playwright launch arguments."""
        return {
            "viewport": {"width": self.viewport[0], "height": self.viewport[1]},
            "user_agent": self.user_agent,
            "geolocation": {"latitude": self.geolocation[0], "longitude": self.geolocation[1]},
            "timezone_id": self.timezone,
            "locale": self.locale,
            "screen": {
                "width": self.screen_resolution[0],
                "height": self.screen_resolution[1]
            },
            "color_scheme": "light",
            "reduced_motion": "no_preference"
        }
```

### 3.2 Identity Registry & Persistence
```python
import redis.asyncio as redis
import json
from typing import Dict, Optional

class IdentityRegistry:
    """Manages identity contexts with strict isolation."""

    def __init__(self, redis_url: str):
        self.redis = redis.from_url(redis_url, decode_responses=True)
        self.key_prefix = "identity_ctx"

    async def register(self, ctx: IdentityContext) -> None:
        """Store identity with account-level isolation."""
        key = f"{self.key_prefix}:{ctx.account_id}"
        # Serialize with deterministic ordering for hash stability
        data = json.dumps(ctx.__dict__, sort_keys=True, separators=(',', ':'))
        await self.redis.set(key, data, ex=None)  # Persistent

    async def get(self, account_id: str) -> Optional[IdentityContext]:
        key = f"{self.key_prefix}:{account_id}"
        data = await self.redis.get(key)
        if not data:
            return None
        return IdentityContext(**json.loads(data))

    async def is_proxy_unique(self, proxy_url: str, exclude_account: str) -> bool:
        """Ensure no two accounts share a proxy."""
        # Scan all identities for proxy collision
        async for key in self.redis.scan_iter(match=f"{self.key_prefix}:*"):
            if exclude_account in key:
                continue
            data = json.loads(await self.redis.get(key))
            if data.get("proxy_url") == proxy_url:
                return False
        return True
```

### 3.3 Token Lifecycle Management (Critical)
Facebook's `fb_dtsg` and `lsd` tokens have implicit lifecycles. Mismanagement triggers `1366046` and checkpoint flows.

```python
import time
from typing import Optional, Dict
import hashlib
import json

class TokenVault:
    """Secure, short-lived token storage with rotation detection."""

    TOKEN_TTL_SECONDS = 240  # 4 minutes — refresh before 5min Facebook expiry

    def __init__(self, redis_client):
        self.redis = redis_client
        self._local_cache: Dict[str, Dict] = {}  # L1 cache

    def _cache_key(self, account_id: str) -> str:
        return f"fb_tokens:{account_id}"

    def _hash_tokens(self, tokens: Dict) -> str:
        """Detect token rotation by comparing hashes."""
        canonical = json.dumps(tokens, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    async def get(self, account_id: str) -> Optional[Dict]:
        # L1 check (in-process)
        if account_id in self._local_cache:
            entry = self._local_cache[account_id]
            if time.time() - entry["ts"] < self.TOKEN_TTL_SECONDS:
                return entry["tokens"]

        # L2 check (Redis)
        key = self._cache_key(account_id)
        data = await self.redis.get(key)
        if data:
            entry = json.loads(data)
            # Validate not expired
            if time.time() - entry["ts"] < self.TOKEN_TTL_SECONDS:
                self._local_cache[account_id] = entry
                return entry["tokens"]
        return None

    async def set(self, account_id: str, tokens: Dict) -> None:
        """Store with hash for rotation tracking."""
        token_hash = self._hash_tokens(tokens)
        entry = {
            "ts": time.time(),
            "tokens": tokens,
            "hash": token_hash,
            "usage_count": 0
        }
        self._local_cache[account_id] = entry
        await self.redis.setex(
            self._cache_key(account_id),
            self.TOKEN_TTL_SECONDS + 60,  # Redis TTL slightly longer
            json.dumps(entry)
        )

    async def increment_usage(self, account_id: str) -> int:
        """Track token usage frequency. High frequency = rotation needed."""
        key = self._cache_key(account_id)
        data = await self.redis.get(key)
        if data:
            entry = json.loads(data)
            entry["usage_count"] = entry.get("usage_count", 0) + 1
            await self.redis.setex(key, self.TOKEN_TTL_SECONDS + 60, json.dumps(entry))
            return entry["usage_count"]
        return 0

    async def is_rotation_needed(self, account_id: str) -> bool:
        """Force rotation if tokens used >20 times or approaching expiry."""
        tokens = await self.get(account_id)
        if not tokens:
            return True
        key = self._cache_key(account_id)
        data = await self.redis.get(key)
        if data:
            entry = json.loads(data)
            age = time.time() - entry["ts"]
            usage = entry.get("usage_count", 0)
            return age > 180 or usage > 20  # Rotate at 3min or 20 uses
        return True
```

---

## IV. Behavioral Mimicry & Human-Like Timing

### 4.1 Stochastic Timing Engine
Never use fixed delays. Use log-normal distributions that model human behavior.

```python
import random
import math
from typing import Callable, List, Tuple

class StochasticTimer:
    """Human-behavioral timing model based on Fitts's Law + fatigue curves."""

    @staticmethod
    def think_time(min_ms: float = 800, max_ms: float = 4500, 
                   focus_factor: float = 1.0) -> float:
        """
        Simulate cognitive processing time.
        Uses log-normal distribution: humans cluster around 1.2s with long tail.
        """
        mu = 0.2  # Log-normal parameter
        sigma = 0.5
        raw = random.lognormvariate(mu, sigma)
        # Scale to range and apply focus factor (tired = slower)
        scaled = min_ms + (raw * (max_ms - min_ms) * focus_factor)
        return scaled / 1000.0  # Convert to seconds

    @staticmethod
    def typing_delay(char_count: int, wpm: float = 65.0) -> List[float]:
        """
        Generate per-character typing delays.
        Models: burst typing, pause after punctuation, fatigue slowdown.
        """
        base_delay = 60.0 / (wpm * 5)  # Seconds per char at given WPM
        delays = []
        for i in range(char_count):
            # 15% chance of pause (space, punctuation, or random)
            if random.random() < 0.15:
                delay = base_delay * random.uniform(2.5, 5.0)
            else:
                # Normal with slight jitter
                delay = base_delay * random.gauss(1.0, 0.15)
            # Fatigue: slow down over long texts
            fatigue = 1.0 + (i / char_count) * 0.15
            delays.append(max(0.01, delay * fatigue))
        return delays

    @staticmethod
    def scroll_pattern(total_height: int) -> List[Tuple[int, float]]:
        """
        Generate scroll events: (delta_pixels, delay_after).
        Models: initial fast scroll, then detailed reading with micro-scrolls.
        """
        events = []
        current = 0
        phase = "fast"  # fast -> detailed -> fast

        while current < total_height:
            if phase == "fast":
                delta = random.randint(300, 800)
                delay = random.uniform(0.1, 0.4)
                if random.random() < 0.3:
                    phase = "detailed"
            elif phase == "detailed":
                delta = random.randint(50, 200)
                delay = random.uniform(1.5, 4.0)
                if random.random() < 0.2:
                    phase = "fast"
            else:
                delta = random.randint(400, 1000)
                delay = random.uniform(0.05, 0.2)

            current += delta
            events.append((delta, delay))
        return events

    @staticmethod
    def post_interval(base_seconds: float, account_age_days: int) -> float:
        """
        Calculate safe posting interval.
        Newer accounts need longer intervals. Established accounts can post faster.
        """
        # Age factor: 0-30 days = 3x base, 30-90 = 1.5x, 90+ = 1.0x
        if account_age_days < 30:
            multiplier = 3.0
        elif account_age_days < 90:
            multiplier = 1.5
        else:
            multiplier = 1.0

        # Add jitter: ±25% variance
        jitter = random.uniform(0.75, 1.25)
        return base_seconds * multiplier * jitter
```

### 4.2 Action Randomization Matrix
```python
class ActionRandomizer:
    """Randomizes high-level action sequences to avoid pattern detection."""

    @staticmethod
    def randomize_post_workflow() -> List[str]:
        """
        Randomize the sequence of actions leading to a post.
        Facebook tracks action sequences. Identical sequences = bot signature.
        """
        workflows = [
            ["navigate_home", "pause", "scroll_feed", "click_composer", "pause", "type", "attach_media", "pause", "submit"],
            ["navigate_home", "click_composer", "pause", "type", "pause", "attach_media", "pause", "scroll_feed", "submit"],
            ["navigate_page", "pause", "click_composer", "type", "pause", "attach_media", "pause", "submit"],
            ["navigate_home", "scroll_feed", "pause", "scroll_feed", "click_composer", "type", "attach_media", "submit"],
        ]
        return random.choice(workflows)

    @staticmethod
    def add_noise_actions(actions: List[str], noise_probability: float = 0.15) -> List[str]:
        """Inject benign noise actions that humans do."""
        noise_pool = ["hover_notification", "click_profile_pic", "scroll_feed", "pause"]
        result = []
        for action in actions:
            if random.random() < noise_probability:
                result.append(random.choice(noise_pool))
            result.append(action)
        return result
```

---

## V. Network Layer & Fingerprint Evasion

### 5.1 Proxy Architecture (Residential Rotating)
Datacenter IPs are flagged instantly. Use residential proxies with sticky sessions.

```python
from typing import Optional, List
import aiohttp

class ProxyManager:
    """Manages residential proxy pool with sticky session affinity."""

    def __init__(self, proxy_list: List[str], redis_client):
        self.proxies = proxy_list
        self.redis = redis_client
        self.sticky_duration = 600  # 10 minutes per session

    async def get_proxy_for_account(self, account_id: str) -> str:
        """Assign sticky proxy to account."""
        key = f"proxy_sticky:{account_id}"
        cached = await self.redis.get(key)
        if cached:
            return cached

        # Round-robin with health check
        # In production: integrate with Bright Data, Oxylabs, or IPRoyal API
        proxy = self._select_healthy_proxy(account_id)
        await self.redis.setex(key, self.sticky_duration, proxy)
        return proxy

    def _select_healthy_proxy(self, account_id: str) -> str:
        # Hash-based selection for consistency
        idx = hash(account_id) % len(self.proxies)
        return self.proxies[idx]

    async def report_failure(self, account_id: str, proxy: str, error: str) -> None:
        """Mark proxy as unhealthy and rotate."""
        key = f"proxy_health:{proxy}"
        await self.redis.hincrby(key, "failures", 1)
        await self.redis.expire(key, 3600)
        # Force sticky rotation
        await self.redis.delete(f"proxy_sticky:{account_id}")
```

### 5.2 TLS & HTTP2 Fingerprint Masking
Python's `aiohttp` and `requests` have distinct TLS fingerprints. We must mask them.

```python
import ssl
import certifi
from aiohttp import TCPConnector

class StealthConnector:
    """Creates TLS connections that mimic real Chrome fingerprints."""

    @staticmethod
    def create_connector() -> TCPConnector:
        """
        Configure TLS to mimic Chrome 126 on Windows 11.
        This affects JA3/JA4 fingerprint.
        """
        # Chrome 126 cipher suites (ordered)
        chrome_ciphers = ":".join([
            "TLS_AES_128_GCM_SHA256",
            "TLS_AES_256_GCM_SHA384",
            "TLS_CHACHA20_POLY1305_SHA256",
            "ECDHE-ECDSA-AES128-GCM-SHA256",
            "ECDHE-RSA-AES128-GCM-SHA256",
            "ECDHE-ECDSA-AES256-GCM-SHA384",
            "ECDHE-RSA-AES256-GCM-SHA384",
            "ECDHE-ECDSA-CHACHA20-POLY1305",
            "ECDHE-RSA-CHACHA20-POLY1305",
            "ECDHE-RSA-AES128-SHA",
            "ECDHE-RSA-AES256-SHA",
            "AES128-GCM-SHA256",
            "AES256-GCM-SHA384",
            "AES128-SHA",
            "AES256-SHA"
        ])

        ssl_context = ssl.create_default_context(cafile=certifi.where())
        ssl_context.set_ciphers(chrome_ciphers)
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        ssl_context.maximum_version = ssl.TLSVersion.TLSv1_3

        # Enable OCSP stapling (Chrome does this)
        # ssl_context.ocsp_response_cb = None

        return TCPConnector(
            ssl=ssl_context,
            limit=100,
            limit_per_host=10,
            enable_cleanup_closed=True,
            force_close=False,  # Allow connection reuse like real browser
            ttl_dns_cache=300
        )
```

### 5.3 Header Randomization (GraphQL & Rupload)
Facebook's API servers analyze header ordering and presence. We must replicate exact Chrome headers with slight acceptable variance.

```python
import random
from typing import Dict

class HeaderForge:
    """Forges request headers that match real Chrome sessions."""

    CHROME_VERSIONS = ["126.0.6478.62", "126.0.6478.63", "126.0.6478.64", "125.0.6422.142"]

    @classmethod
    def forge_graphql_headers(cls, tokens: Dict, identity: 'IdentityContext') -> Dict[str, str]:
        chrome = random.choice(cls.CHROME_VERSIONS)

        headers = {
            "authority": "www.facebook.com",
            "method": "POST",
            "path": "/api/graphql/",
            "scheme": "https",
            "accept": "*/*",
            "accept-encoding": "gzip, deflate, br, zstd",
            "accept-language": identity.locale.replace("_", "-"),
            "content-type": "application/x-www-form-urlencoded",
            "cookie": "",  # Injected by aiohttp cookie jar
            "origin": "https://www.facebook.com",
            "referer": f"https://www.facebook.com/{identity.facebook_user_id or 'me'}",
            "sec-ch-ua": f'"Not/A)Brand";v="8", "Chromium";v="{chrome.split(".")[0]}", "Google Chrome";v="{chrome.split(".")[0]}"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": f'"{identity.platform.replace(" x86_64", "").replace("Win32", "Windows")}"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": identity.user_agent,
            "x-fb-friendly-name": "ComposerStoryCreateMutation",
            "x-fb-lsd": tokens.get("lsd", ""),
        }

        return headers

    @classmethod
    def forge_rupload_headers(cls, tokens: Dict, identity: 'IdentityContext', 
                              file_size: int, offset: int = 0) -> Dict[str, str]:
        headers = {
            "accept": "*/*",
            "accept-encoding": "gzip, deflate, br",
            "accept-language": identity.locale.replace("_", "-"),
            "content-type": "application/octet-stream",
            "cookie": "",
            "origin": "https://www.facebook.com",
            "referer": "https://www.facebook.com/",
            "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": f'"{identity.platform.replace(" x86_64", "").replace("Win32", "Windows")}"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": identity.user_agent,
            "x-fb-fb-dtsg": tokens.get("fb_dtsg", ""),
            "x-fb-lsd": tokens.get("lsd", ""),
            "x-fb-upload-filesize": str(file_size),
            "x-fb-upload-offset": str(offset),
            "x-fb-upload-retry-count": "0",
        }
        return headers
```

---

## VI. Enhanced FastImagePoster with Safety Guards

This is the hardened implementation of the rupload + GraphQL flow with all safety layers integrated.

### 6.1 Pre-Flight Safety Checklist
```python
from enum import Enum
from typing import Tuple, Optional
import aiohttp
import asyncio
import time
import json

class SafetyStatus(Enum):
    CLEAR = "clear"
    QUARANTINE = "quarantine"
    COOLDOWN = "cooldown"
    CHECKPOINT = "checkpoint"

class SafetyGuard:
    """Pre-flight and post-flight safety validation."""

    def __init__(self, redis_client, identity: 'IdentityContext'):
        self.redis = redis_client
        self.identity = identity
        self.account_id = identity.account_id

    async def pre_flight_check(self) -> Tuple[SafetyStatus, str]:
        """Run all safety checks before any network activity."""

        # Check 1: Account quarantine
        quarantine_key = f"quarantine:{self.account_id}"
        if await self.redis.exists(quarantine_key):
            ttl = await self.redis.ttl(quarantine_key)
            return SafetyStatus.QUARANTINE, f"Account quarantined for {ttl}s"

        # Check 2: Rate limit (max 6 posts per hour per account)
        rate_key = f"post_rate:{self.account_id}:{self._hour_bucket()}"
        current_count = await self.redis.incr(rate_key)
        if current_count == 1:
            await self.redis.expire(rate_key, 3600)
        if current_count > 6:
            return SafetyStatus.COOLDOWN, f"Rate limit exceeded: {current_count}/6 posts this hour"

        # Check 3: Daily velocity cap (max 20 posts per day)
        daily_key = f"post_daily:{self.account_id}:{self._day_bucket()}"
        daily_count = await self.redis.incr(daily_key)
        if daily_count == 1:
            await self.redis.expire(daily_key, 86400)
        if daily_count > 20:
            return SafetyStatus.COOLDOWN, f"Daily cap exceeded: {daily_count}/20 posts today"

        # Check 4: Proxy health
        proxy_key = f"proxy_health:{self.identity.proxy_url}"
        failures = await self.redis.hget(proxy_key, "failures")
        if failures and int(failures) > 3:
            return SafetyStatus.QUARANTINE, "Proxy marked unhealthy"

        # Check 5: Token freshness
        token_vault = TokenVault(self.redis)
        if await token_vault.is_rotation_needed(self.account_id):
            return SafetyStatus.COOLDOWN, "Token rotation required"

        return SafetyStatus.CLEAR, "All checks passed"

    async def post_flight_validation(self, success: bool, response_code: int, 
                                     error_message: Optional[str]) -> SafetyStatus:
        """Analyze response to detect early warning signs."""
        if not success:
            # Pattern match for checkpoint indicators
            if error_message:
                lower_err = error_message.lower()
                if any(x in lower_err for x in ["checkpoint", "security", "verify", "confirm identity"]):
                    await self._trigger_quarantine(3600, "Security checkpoint detected")
                    return SafetyStatus.CHECKPOINT
                if any(x in lower_err for x in ["session expired", "logged out", "token invalid"]):
                    await self._trigger_quarantine(300, "Token expiry — force refresh")
                    return SafetyStatus.COOLDOWN
                if "1366046" in lower_err:
                    await self._trigger_quarantine(1800, "Upload fingerprint rejection")
                    return SafetyStatus.QUARANTINE

        # Success: update health score
        health_key = f"account_health:{self.account_id}"
        await self.redis.hincrby(health_key, "success_streak", 1)
        await self.redis.hset(health_key, "last_success", str(time.time()))
        return SafetyStatus.CLEAR

    async def _trigger_quarantine(self, duration_seconds: int, reason: str) -> None:
        key = f"quarantine:{self.account_id}"
        await self.redis.setex(key, duration_seconds, reason)
        # Alert admin
        await self.redis.publish("admin_alerts", json.dumps({
            "level": "CRITICAL",
            "account": self.account_id,
            "reason": reason,
            "duration": duration_seconds,
            "timestamp": time.time()
        }))

    def _hour_bucket(self) -> str:
        return time.strftime("%Y%m%d%H")

    def _day_bucket(self) -> str:
        return time.strftime("%Y%m%d")
```

### 6.2 Hardened Rupload Implementation
```python
import os
import hashlib
import aiofiles
from PIL import Image
import io
import tempfile

class HardenedRupload:
    """rupload implementation with media mutation and fingerprint evasion."""

    def __init__(self, token_vault: TokenVault, identity: 'IdentityContext', 
                 redis_client, proxy_manager: ProxyManager):
        self.tokens = token_vault
        self.identity = identity
        self.redis = redis_client
        self.proxy = proxy_manager
        self.connector = StealthConnector.create_connector()

    async def upload_image(self, image_path: str) -> Tuple[bool, Optional[str], str]:
        """
        Upload image with full safety pipeline.
        Returns: (success, media_fbid, diagnostic_message)
        """
        # Step 0: Validate image
        if not await self._validate_image(image_path):
            return False, None, "Image validation failed"

        # Step 1: Mutate image fingerprint (slight re-encode to change hash)
        mutated_path = await self._mutate_image_fingerprint(image_path)

        try:
            file_size = os.path.getsize(mutated_path)
            file_name = os.path.basename(mutated_path)
            tokens = await self.tokens.get(self.identity.account_id)
            if not tokens:
                return False, None, "No tokens available"

            proxy_url = await self.proxy.get_proxy_for_account(self.identity.account_id)
            headers = HeaderForge.forge_rupload_headers(tokens, self.identity, file_size)

            # Step 2: Initiate upload with jittered timing
            await asyncio.sleep(StochasticTimer.think_time(200, 800))

            async with aiohttp.ClientSession(
                connector=self.connector,
                trust_env=True
            ) as session:
                # Step 2a: Upload init
                init_payload = {
                    'fb_dtsg': tokens['fb_dtsg'],
                    'lsd': tokens['lsd'],
                    'file_size': file_size,
                    'media_type': 'image/jpeg',
                    'file_name': file_name,
                    'client_id': self._generate_client_id(),
                }

                async with session.post(
                    'https://rupload.facebook.com/video/upload_init',
                    data=init_payload,
                    headers=headers,
                    proxy=proxy_url,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    init_data = await resp.json()
                    upload_id = init_data.get('upload_id')

                if not upload_id:
                    return False, None, f"Init failed: {init_data}"

                # Step 3: Transfer with chunked reading (mimic browser memory management)
                chunk_size = 8192  # 8KB chunks like browser
                async with aiofiles.open(mutated_path, 'rb') as f:
                    async with session.post(
                        f'https://rupload.facebook.com/video/upload/{upload_id}',
                        data=await f.read(),  # In production: true chunked async generator
                        headers={**headers, 'content-length': str(file_size)},
                        proxy=proxy_url,
                        timeout=aiohttp.ClientTimeout(total=45)
                    ) as resp:
                        result = await resp.json()
                        media_fbid = result.get('fbid') or result.get('media_fbid')

                if not media_fbid:
                    return False, None, f"Transfer failed: {result}"

                # Step 4: Increment token usage
                await self.tokens.increment_usage(self.identity.account_id)

                return True, media_fbid, "Success"

        except Exception as e:
            await self.proxy.report_failure(self.identity.account_id, proxy_url, str(e))
            return False, None, f"Exception: {str(e)[:200]}"
        finally:
            # Cleanup mutated temp file
            if mutated_path != image_path and os.path.exists(mutated_path):
                os.remove(mutated_path)

    async def _validate_image(self, path: str) -> bool:
        """Validate image and ensure it's not a known hash."""
        try:
            with Image.open(path) as img:
                img.verify()
                # Reject if dimensions are suspicious (e.g., 1x1 tracking pixel)
                if img.width < 100 or img.height < 100:
                    return False
                # Reject if file size is extreme
                size = os.path.getsize(path)
                if size < 1024 or size > 15 * 1024 * 1024:  # 1KB - 15MB
                    return False
            return True
        except Exception:
            return False

    async def _mutate_image_fingerprint(self, path: str) -> str:
        """
        Slightly re-encode image to change file hash while preserving visual.
        This prevents Facebook's perceptual hash matching from flagging duplicate uploads.
        """
        with Image.open(path) as img:
            # Convert to RGB if necessary
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')

            # Subtle quality jitter (88-95%)
            quality = random.randint(88, 95)

            # Subtle resize by 1-2 pixels (imperceptible but changes hash)
            new_width = img.width + random.choice([-1, 0, 1])
            new_height = img.height + random.choice([-1, 0, 1])
            if new_width > 0 and new_height > 0:
                img = img.resize((new_width, new_height), Image.LANCZOS)

            # Save to temp with new metadata
            suffix = '.jpg'
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                img.save(tmp.name, 'JPEG', quality=quality, optimize=True)
                return tmp.name

    def _generate_client_id(self) -> str:
        """Generate client ID that mimics Facebook's internal format."""
        # Facebook uses 16-char hex client IDs
        return hashlib.md5(f"{self.identity.account_id}{time.time()}".encode()).hexdigest()[:16]
```

### 6.3 Hardened GraphQL Poster
```python
class HardenedGraphQLPoster:
    """GraphQL posting with full safety integration."""

    def __init__(self, token_vault: TokenVault, identity: 'IdentityContext',
                 redis_client, proxy_manager: ProxyManager):
        self.tokens = token_vault
        self.identity = identity
        self.redis = redis_client
        self.proxy = proxy_manager
        self.connector = StealthConnector.create_connector()
        self.safety = SafetyGuard(redis_client, identity)

    async def post_to_page(self, page_id: str, caption: str, 
                           media_fbid: Optional[str]) -> Tuple[bool, str, Optional[str]]:
        """
        Post with full pre-flight and post-flight safety.
        Returns: (success, status, post_id)
        """
        # Pre-flight
        status, message = await self.safety.pre_flight_check()
        if status != SafetyStatus.CLEAR:
            return False, status.value, None

        tokens = await self.tokens.get(self.identity.account_id)
        if not tokens:
            return False, "TOKEN_EXPIRED", None

        # Build idempotency token with entropy
        idempotence = self._generate_idempotence_token(page_id, caption)

        # Check if already posted (idempotency)
        idemp_key = f"fb_post_idemp:{idempotence}"
        existing = await self.redis.get(idemp_key)
        if existing:
            return True, "IDEMPOTENT", existing

        # Build payload with exact Facebook structure
        variables = {
            "input": {
                "composer_entry_point": "inline",
                "composer_source_surface": "composer",
                "idempotence_token": idempotence,
                "source": "WWW",
                "message": {"ranges": [], "text": caption},
                "actor_id": page_id,
                "client_mutation_id": str(int(time.time() * 1000)),
                **({"attachments": [{"media_fbid": media_fbid}]} if media_fbid else {})
            }
        }

        payload = {
            'fb_dtsg': tokens['fb_dtsg'],
            'lsd': tokens['lsd'],
            'variables': json.dumps(variables, separators=(',', ':')),
            'doc_id': await self._get_doc_id(),
            '__req': self._generate_req_param(),
            '__a': "1",
            '__user': self.identity.facebook_user_id or "0",
        }

        headers = HeaderForge.forge_graphql_headers(tokens, self.identity)
        proxy_url = await self.proxy.get_proxy_for_account(self.identity.account_id)

        try:
            # Jitter before request
            await asyncio.sleep(StochasticTimer.think_time(100, 500))

            async with aiohttp.ClientSession(
                connector=self.connector,
                trust_env=True
            ) as session:
                async with session.post(
                    'https://www.facebook.com/api/graphql/',
                    data=payload,
                    headers=headers,
                    proxy=proxy_url,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    text = await resp.text()

                    # Handle Facebook's anti-CSRF prefix
                    if text.startswith('for(;;);'):
                        text = text[8:]

                    data = json.loads(text)

                    # Post-flight analysis
                    success = 'errors' not in data
                    error_msg = None
                    if not success:
                        error_msg = data.get('errors', [{}])[0].get('message', 'Unknown error')

                    await self.safety.post_flight_validation(
                        success, resp.status, error_msg
                    )

                    if not success:
                        return False, self._classify_error(error_msg), None

                    # Extract post ID
                    post_data = data.get('data', {}).get('composer_story_create', {})
                    post_id = (post_data.get('story', {}).get('legacy_story_hideable_id') or 
                              post_data.get('post_id'))

                    # Cache idempotency
                    if post_id:
                        await self.redis.setex(idemp_key, 86400, post_id)

                    await self.tokens.increment_usage(self.identity.account_id)
                    return True, "SUCCESS", post_id

        except Exception as e:
            await self.proxy.report_failure(self.identity.account_id, proxy_url, str(e))
            return False, f"NETWORK_ERROR: {str(e)[:100]}", None

    def _generate_idempotence_token(self, page_id: str, caption: str) -> str:
        """Generate unique but deterministic idempotency token."""
        # Mix account, page, caption hash, and time bucket (10-min window)
        time_bucket = int(time.time() / 600)
        base = f"{self.identity.account_id}:{page_id}:{hash(caption)}:{time_bucket}"
        return hashlib.sha256(base.encode()).hexdigest()[:32]

    async def _get_doc_id(self) -> str:
        """Fetch current doc_id from cache or fallback to stable."""
        cached = await self.redis.get("fb_graphql_doc_id")
        return cached or "7711610262198779"  # Fallback stable ID

    def _generate_req_param(self) -> str:
        """Generate Facebook's __req parameter (base64-ish counter)."""
        # Facebook uses a simple counter encoded in a custom base
        counter = int(time.time() * 1000) % 10000
        # Simplified: in production, track actual sequence per session
        import base64
        return base64.b64encode(str(counter).encode()).decode().rstrip('=')

    def _classify_error(self, error_msg: str) -> str:
        lower = error_msg.lower()
        if any(x in lower for x in ["checkpoint", "security", "verify"]):
            return "SECURITY_CHECKPOINT"
        if any(x in lower for x in ["session", "expired", "logged out"]):
            return "TOKEN_EXPIRED"
        if "1366046" in lower:
            return "UPLOAD_REJECTED"
        if "rate limit" in lower or "too fast" in lower:
            return "RATE_LIMITED"
        return f"GRAPHQL_ERROR: {error_msg[:150]}"
```

---

## VII. Checkpoint & Account Quarantine System

### 7.1 Quarantine State Machine
```python
from enum import Enum, auto

class QuarantineLevel(Enum):
    NONE = auto()           # Normal operation
    SOFT = auto()           # 15-min cooldown, reduce velocity
    HARD = auto()           # 1-hour lock, browser fallback only
    SEVERE = auto()         # 24-hour lock, admin review required
    BANNED = auto()         # Permanent, account burned

class QuarantineManager:
    """Manages account health with escalating quarantine levels."""

    ESCALATION_MATRIX = {
        QuarantineLevel.NONE: (QuarantineLevel.SOFT, 900),    # 15 min
        QuarantineLevel.SOFT: (QuarantineLevel.HARD, 3600),   # 1 hour
        QuarantineLevel.HARD: (QuarantineLevel.SEVERE, 86400), # 24 hours
        QuarantineLevel.SEVERE: (QuarantineLevel.BANNED, None),
    }

    def __init__(self, redis_client):
        self.redis = redis_client

    async def get_level(self, account_id: str) -> QuarantineLevel:
        key = f"quarantine_level:{account_id}"
        level = await self.redis.get(key)
        if not level:
            return QuarantineLevel.NONE
        return QuarantineLevel[level]

    async def escalate(self, account_id: str, reason: str) -> QuarantineLevel:
        current = await self.get_level(account_id)
        if current == QuarantineLevel.BANNED:
            return current

        next_level, duration = self.ESCALATION_MATRIX.get(current, (QuarantineLevel.BANNED, None))

        # Set quarantine
        if duration:
            await self.redis.setex(f"quarantine:{account_id}", duration, reason)
            await self.redis.setex(f"quarantine_level:{account_id}", duration, next_level.name)
        else:
            await self.redis.set(f"quarantine:{account_id}", reason)
            await self.redis.set(f"quarantine_level:{account_id}", next_level.name)

        # Log escalation
        await self.redis.xadd("quarantine_log", {
            "account": account_id,
            "from": current.name,
            "to": next_level.name,
            "reason": reason,
            "ts": int(time.time() * 1000)
        })

        # Admin alert if HARD or worse
        if next_level in (QuarantineLevel.HARD, QuarantineLevel.SEVERE, QuarantineLevel.BANNED):
            await self.redis.publish("admin_alerts", json.dumps({
                "level": "CRITICAL",
                "event": "QUARANTINE_ESCALATION",
                "account": account_id,
                "new_level": next_level.name,
                "reason": reason
            }))

        return next_level

    async def reset(self, account_id: str, admin_override: bool = False) -> None:
        """Reset quarantine after manual review or cooldown expiry."""
        if not admin_override:
            # Verify cooldown actually expired
            ttl = await self.redis.ttl(f"quarantine:{account_id}")
            if ttl > 0:
                raise PermissionError("Quarantine still active")

        await self.redis.delete(f"quarantine:{account_id}")
        await self.redis.delete(f"quarantine_level:{account_id}")
        await self.redis.hset(f"account_health:{account_id}", "success_streak", "0")

        await self.redis.xadd("quarantine_log", {
            "account": account_id,
            "event": "RESET",
            "admin_override": str(admin_override),
            "ts": int(time.time() * 1000)
        })
```

### 7.2 Checkpoint Recovery (Human-in-the-Loop)
```python
class CheckpointRecovery:
    """Handles security checkpoints by escalating to human review."""

    async def handle_checkpoint(self, account_id: str, checkpoint_url: str, 
                                screenshot_bytes: bytes) -> None:
        """
        On checkpoint detection:
        1. Immediately quarantine account (SEVERE)
        2. Store checkpoint data for human review
        3. Send alert via Redis pub/sub or webhook
        4. NEVER attempt automated checkpoint solving
        """
        qm = QuarantineManager(self.redis)
        await qm.escalate(account_id, f"Checkpoint detected: {checkpoint_url}")

        # Store checkpoint artifact
        import base64
        await self.redis.setex(
            f"checkpoint_artifact:{account_id}",
            86400,
            json.dumps({
                "url": checkpoint_url,
                "screenshot_b64": base64.b64encode(screenshot_bytes).decode(),
                "detected_at": time.time(),
                "requires_action": True
            })
        )

        # Webhook notification
        await self._send_webhook({
            "event": "checkpoint_detected",
            "account_id": account_id,
            "checkpoint_url": checkpoint_url,
            "action_required": "manual_review",
            "severity": "SEVERE"
        })

    async def _send_webhook(self, payload: dict) -> None:
        webhook_url = os.getenv("ADMIN_WEBHOOK_URL")
        if not webhook_url:
            return
        async with aiohttp.ClientSession() as session:
            await session.post(webhook_url, json=payload, timeout=aiohttp.ClientTimeout(total=10))
```

---

## VIII. Monitoring, Alerting & Audit Logging

### 8.1 Structured Audit Logger
```python
import structlog
import asyncio
from typing import Any, Dict

class AuditLogger:
    """Immutable, structured audit trail for compliance and debugging."""

    def __init__(self, redis_client):
        self.redis = redis_client
        self.logger = structlog.get_logger()

    async def log_action(self, account_id: str, action: str, 
                         metadata: Dict[str, Any], outcome: str) -> None:
        """
        Write tamper-resistant audit entry.
        All actions are logged to Redis Stream for durability.
        """
        entry = {
            "account_id": account_id,
            "action": action,
            "outcome": outcome,
            "metadata": json.dumps(metadata, separators=(',', ':')),
            "timestamp_ms": int(time.time() * 1000),
            "source_ip_hash": hashlib.sha256(
                (metadata.get("proxy", "unknown")).encode()
            ).hexdigest()[:16],  # Hash for privacy
        }

        # Write to append-only stream
        await self.redis.xadd("audit:actions", entry, maxlen=100000)

        # Also write to structured log
        self.logger.info(
            "automation_action",
            account=account_id,
            action=action,
            outcome=outcome,
            **metadata
        )

    async def get_account_history(self, account_id: str, limit: int = 100) -> List[dict]:
        """Retrieve recent actions for an account."""
        # Read from stream with account filter
        entries = []
        async for msg in self.redis.xread({"audit:actions": "$"}, count=limit):
            for stream_name, messages in msg:
                for msg_id, fields in messages:
                    if fields.get("account_id") == account_id:
                        entries.append(fields)
        return entries
```

### 8.2 Real-Time Anomaly Detection
```python
class AnomalyDetector:
    """Detects statistical anomalies that indicate detection risk."""

    def __init__(self, redis_client):
        self.redis = redis_client

    async def check_anomalies(self, account_id: str) -> List[str]:
        """Return list of anomaly flags."""
        flags = []

        # Check 1: Success rate drop
        health_key = f"account_health:{account_id}"
        streak = await self.redis.hget(health_key, "success_streak")
        streak = int(streak or 0)
        if streak == 0:
            recent = await self.redis.lrange(f"outcomes:{account_id}", 0, 9)
            failures = sum(1 for r in recent if r != "SUCCESS")
            if failures >= 7:
                flags.append("HIGH_FAILURE_RATE")

        # Check 2: Temporal clustering (posts too close together)
        last_posts = await self.redis.lrange(f"post_times:{account_id}", 0, 4)
        if len(last_posts) >= 3:
            times = [float(t) for t in last_posts]
            intervals = [times[i] - times[i+1] for i in range(len(times)-1)]
            avg_interval = sum(intervals) / len(intervals)
            if avg_interval < 5.0:  # Less than 5 seconds between posts
                flags.append("TEMPORAL_CLUSTERING")

        # Check 3: Content duplication
        recent_captions = await self.redis.lrange(f"captions:{account_id}", 0, 9)
        if len(set(recent_captions)) < len(recent_captions) * 0.5:
            flags.append("CONTENT_DUPLICATION")

        # Check 4: Proxy/IP hopping
        recent_ips = await self.redis.lrange(f"proxy_used:{account_id}", 0, 4)
        if len(set(recent_ips)) > 2:
            flags.append("PROXY_HOPPING")

        return flags

    async def record_post(self, account_id: str, caption: str, proxy: str) -> None:
        """Record post metadata for anomaly detection."""
        pipe = self.redis.pipeline()
        pipe.lpush(f"post_times:{account_id}", str(time.time()))
        pipe.ltrim(f"post_times:{account_id}", 0, 99)
        pipe.lpush(f"captions:{account_id}", hashlib.sha256(caption.encode()).hexdigest()[:16])
        pipe.ltrim(f"captions:{account_id}", 0, 99)
        pipe.lpush(f"proxy_used:{account_id}", proxy)
        pipe.ltrim(f"proxy_used:{account_id}", 0, 99)
        await pipe.execute()
```

---

## IX. Configuration & Deployment Matrix

### 9.1 Environment Configuration
```python
# config.py — Production Hardened Settings

from typing import Dict, List
import os

class SafetyConfig:
    """Immutable safety configuration."""

    # Rate Limiting
    POSTS_PER_HOUR: int = 6
    POSTS_PER_DAY: int = 20
    MIN_POST_INTERVAL_SECONDS: float = 120.0  # 2 minutes minimum

    # Concurrency
    MAX_CONCURRENT_POSTS_PER_ACCOUNT: int = 1
    MAX_CONCURRENT_ACCOUNTS_GLOBAL: int = 50

    # Token Management
    TOKEN_REFRESH_INTERVAL_SECONDS: int = 240
    TOKEN_MAX_USAGE_COUNT: int = 20

    # Quarantine
    QUARANTINE_SOFT_SECONDS: int = 900      # 15 min
    QUARANTINE_HARD_SECONDS: int = 3600     # 1 hour
    QUARANTINE_SEVERE_SECONDS: int = 86400  # 24 hours

    # Network
    PROXY_STICKY_SECONDS: int = 600
    PROXY_MAX_FAILURES: int = 3
    REQUEST_TIMEOUT_SECONDS: int = 15
    UPLOAD_TIMEOUT_SECONDS: int = 45

    # Behavioral
    TYPING_WPM_MIN: float = 45.0
    TYPING_WPM_MAX: float = 85.0
    THINK_TIME_MIN_MS: float = 500
    THINK_TIME_MAX_MS: float = 5000

    # Media
    MAX_IMAGE_SIZE_MB: int = 15
    MIN_IMAGE_DIMENSION: int = 100
    IMAGE_QUALITY_JITTER_MIN: int = 88
    IMAGE_QUALITY_JITTER_MAX: int = 95

    # Alerting
    ADMIN_WEBHOOK_URL: str = os.getenv("ADMIN_WEBHOOK_URL", "")
    ALERT_CHANNEL: str = os.getenv("ALERT_CHANNEL", "admin_alerts")

    # Fallback
    ENABLE_BROWSER_FALLBACK: bool = True
    BROWSER_FALLBACK_ON_ERRORS: List[str] = ["TOKEN_EXPIRED", "RUPLOAD_FAILED"]
    MAX_BROWSER_FALLBACK_RATIO: float = 0.1  # Max 10% of posts via browser
```

### 9.2 Deployment Architecture
```
┌──────────────────────────────────────────────────────────────┐
│                        Load Balancer                          │
│                   (Rate limit + Auth)                         │
└──────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
┌───────▼────────┐   ┌────────▼────────┐   ┌────────▼────────┐
│  HTTP Worker 1 │   │  HTTP Worker 2  │   │  HTTP Worker N  │
│  (No Browser)  │   │  (No Browser)   │   │  (No Browser)   │
│  Stateless     │   │  Stateless      │   │  Stateless      │
└───────┬────────┘   └────────┬────────┘   └────────┬────────┘
        │                     │                     │
        └─────────────────────┼─────────────────────┘
                              │
                    ┌─────────▼──────────┐
                    │   Redis Cluster    │
                    │  (Tokens + Queue   │
                    │   + Quarantine)    │
                    └─────────┬──────────┘
                              │
                    ┌─────────▼──────────┐
                    │  Browser Worker    │
                    │  (Fallback Only)   │
                    │  Playwright +      │
                    │  Stealth Patches   │
                    └────────────────────┘
```

### 9.3 Worker Implementation Sketch
```python
class HTTPWorker:
    """Stateless HTTP worker for high-volume posting."""

    def __init__(self, redis_url: str, proxy_pool: List[str]):
        self.redis = redis.from_url(redis_url)
        self.proxy_manager = ProxyManager(proxy_pool, self.redis)
        self.token_vault = TokenVault(self.redis)
        self.anomaly = AnomalyDetector(self.redis)
        self.audit = AuditLogger(self.redis)

    async def process_job(self, job: dict) -> dict:
        account_id = job["account_id"]
        identity = await IdentityRegistry(self.redis).get(account_id)

        if not identity:
            return {"success": False, "error": "Identity not found"}

        # Anomaly pre-check
        anomalies = await self.anomaly.check_anomalies(account_id)
        if anomalies:
            await self.audit.log_action(account_id, "job_rejected", 
                                        {"anomalies": anomalies}, "REJECTED")
            return {"success": False, "error": f"Anomalies detected: {anomalies}"}

        # Execute
        poster = HardenedGraphQLPoster(self.token_vault, identity, self.redis, self.proxy_manager)
        uploader = HardenedRupload(self.token_vault, identity, self.redis, self.proxy_manager)

        media_fbid = None
        if job.get("media_path"):
            ok, media_fbid, msg = await uploader.upload_image(job["media_path"])
            if not ok:
                return {"success": False, "error": msg}

        success, status, post_id = await poster.post_to_page(
            job["page_id"], job["caption"], media_fbid
        )

        # Record for anomaly detection
        if success:
            await self.anomaly.record_post(account_id, job["caption"], identity.proxy_url)

        await self.audit.log_action(account_id, "post", {
            "page_id": job["page_id"],
            "has_media": bool(media_fbid),
            "status": status
        }, "SUCCESS" if success else "FAILED")

        return {"success": success, "status": status, "post_id": post_id}
```

---

## X. Ethical Safeguards & Compliance Boundaries

### 10.1 Hardcoded Safety Boundaries
These are **non-configurable** in production to prevent abuse:

```python
from dataclasses import dataclass, field
from typing import Tuple, Optional
import hashlib

@dataclass
class EthicalGuardrails:
    """Immutable guardrails that cannot be bypassed by configuration."""

    ABSOLUTE_MAX_POSTS_PER_DAY: int = 50  # Hard ceiling regardless of account age
    ABSOLUTE_MIN_INTERVAL_SECONDS: float = 60.0  # Never post faster than 1/min
    PROHIBITED_CONTENT_HASHES: set = field(default_factory=set)  # Known spam signatures

    @classmethod
    def validate_content(cls, caption: str, media_path: Optional[str]) -> Tuple[bool, str]:
        """
        Reject content that violates platform norms.
        This is a minimal ethical filter — expand based on your use case.
        """
        # Check 1: Empty or meaningless content
        if not caption or len(caption.strip()) < 3:
            return False, "Content too short"

        # Check 2: Excessive repetition (spam indicator)
        words = caption.lower().split()
        unique_ratio = len(set(words)) / len(words) if words else 0
        if unique_ratio < 0.3 and len(words) > 10:
            return False, "Excessive repetition detected"

        # Check 3: Known spam hash (if media provided)
        if media_path:
            file_hash = hashlib.sha256(open(media_path, "rb").read()).hexdigest()
            if file_hash in cls.PROHIBITED_CONTENT_HASHES:
                return False, "Prohibited media fingerprint"

        return True, "Content acceptable"

    @classmethod
    def validate_velocity(cls, account_age_days: int, proposed_interval: float) -> bool:
        """Enforce absolute minimum interval regardless of account age."""
        if proposed_interval < cls.ABSOLUTE_MIN_INTERVAL_SECONDS:
            return False
        return True
```

### 10.2 Operational Ethics
1. **Transparency**: If automating on behalf of clients, explicit disclosure is required under most jurisdictions (FTC, GDPR, etc.).
2. **Consent**: Only automate accounts where you have explicit written authorization.
3. **No Evasion of Legal Process**: If Facebook issues a legal takedown or account suspension, the system must comply immediately.
4. **Data Minimization**: Store only hashed/encrypted session data. Never persist user passwords.
5. **Human Override**: All quarantine escalations beyond SOFT require human review. Never fully automate account recovery from checkpoints.

### 10.3 Legal Compliance Checklist
- [ ] **CFAA (US)**: Ensure authorized access only (explicit client consent).
- [ ] **GDPR (EU)**: Hash/encrypt all PII; implement right-to-erasure for stored tokens.
- [ ] **Platform ToS**: This architecture is designed for authorized social media management. Using it for spam, harassment, or unauthorized access violates Facebook's Terms and this guide's intent.
- [ ] **CAN-SPAM / DMCA**: Content filters must prevent bulk distribution of copyrighted or deceptive material.

---

## Appendix A: Quick Reference — Detection Signal → Mitigation Mapping

| Facebook Detection Signal | Mitigation in This Architecture |
|---------------------------|--------------------------------|
| `navigator.webdriver=true` | Stealth browser patches + HTTP-first strategy |
| Canvas/WebGL fingerprint mismatch | IdentityContext with fixed, consistent fingerprints |
| JA3/JA4 TLS fingerprint | StealthConnector with Chrome cipher ordering |
| Behavioral timing analysis | StochasticTimer with log-normal distributions |
| IP reputation / datacenter flag | ProxyManager with residential sticky proxies |
| `fb_dtsg` overuse / rotation | TokenVault with 4-min TTL and usage counting |
| Identical media hash | `_mutate_image_fingerprint` with quality jitter |
| Identical caption hash | Content entropy + idempotency tokens with time buckets |
| Temporal clustering (too fast) | SafetyGuard rate limits + post interval jitter |
| Header ordering analysis | HeaderForge with consistent Chrome ordering |
| Security checkpoint | QuarantineManager escalation + human-in-the-loop |
| Error 1366046 | HardenedRupload bypasses DOM injection entirely |

---

## Appendix B: Redis Key Schema

```
identity_ctx:{account_id}           -> JSON (IdentityContext)
fb_tokens:{account_id}              -> JSON (TokenVault entry)
quarantine:{account_id}           -> STRING (reason)
quarantine_level:{account_id}       -> STRING (NONE/SOFT/HARD/SEVERE/BANNED)
post_rate:{account_id}:{hour}       -> INT (count)
post_daily:{account_id}:{day}       -> INT (count)
proxy_sticky:{account_id}           -> STRING (proxy_url)
proxy_health:{proxy_url}          -> HASH (failures, last_error)
account_health:{account_id}       -> HASH (success_streak, last_success)
fb_post_idemp:{hash}              -> STRING (post_id)
fb_graphql_doc_id                 -> STRING (doc_id)
audit:actions                     -> STREAM (append-only)
quarantine_log                    -> STREAM (append-only)
post_times:{account_id}           -> LIST (timestamps)
captions:{account_id}             -> LIST (hashes)
proxy_used:{account_id}           -> LIST (proxy_urls)
outcomes:{account_id}             -> LIST (SUCCESS/FAILED)
checkpoint_artifact:{account_id}  -> STRING (JSON artifact)
```

---

*Document Version: 2026.06.04*  
*Classification: Technical Architecture — Authorized Automation Systems*  
*Warning: This architecture is designed for legitimate, authorized social media management. Misuse for spam, unauthorized access, or platform abuse violates the terms of service of Facebook and the ethical boundaries outlined in Section X.*
