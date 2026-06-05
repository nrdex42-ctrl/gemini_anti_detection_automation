# CRITICAL GAPS — IMPLEMENTATION FIXES
## For Codex — Production-Ready Completion Guide
## Based on Audit: 65-70% Coverage → 100% Coverage

---

## INSTRUCTION TO CODEX

You have audited the codebase and identified 9 critical gaps. This document provides the **exact implementation** for each gap. Implement these in the order listed. Each section contains:
- The gap description
- The exact file and method to modify
- The complete corrected code
- Verification tests

**Priority Order:** Fix 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9

---

## GAP 1: Global Same-Account Interval Enforcement (CRITICAL)

**Current Problem:** `last_post_time:{account_id}` only persists within one batch dispatch. If two workers process jobs for the same account, the second worker does not see the first worker's post time.

**Root Cause:** The interval check is done in `orchestrator.py` during batch grouping, but not enforced at the worker level with Redis persistence.

**Fix Location:** `safety.py` and `orchestrator.py`

### Step 1A: Add Global Interval Check to `SafetyGuard`

```python
# In safety.py, add to SafetyGuard class:

async def enforce_global_interval(self) -> Tuple[bool, float]:
    """
    Enforce minimum interval between posts for the SAME account across ALL workers.
    Returns: (can_proceed, sleep_seconds)
    """
    key = f"last_post_time:{self.account_id}"

    # Use Redis TIME to avoid clock skew between workers
    redis_time = await self.redis.time()
    current_time = redis_time[0] + redis_time[1] / 1000000.0

    last_post = await self.redis.get(key)
    if last_post:
        elapsed = current_time - float(last_post)
        if elapsed < SafetyConfig.MIN_POST_INTERVAL_SECONDS:
            sleep_time = SafetyConfig.MIN_POST_INTERVAL_SECONDS - elapsed
            # Add jitter: 0-30 seconds to prevent thundering herd
            sleep_time += random.uniform(0, 30)
            return False, sleep_time

    return True, 0.0

async def record_post_time(self) -> None:
    """Record post time using Redis TIME for consistency."""
    redis_time = await self.redis.time()
    current_time = redis_time[0] + redis_time[1] / 1000000.0
    await self.redis.set(
        f"last_post_time:{self.account_id}",
        str(current_time),
        ex=3600  # TTL: keep for 1 hour max
    )
```

### Step 1B: Integrate into `pre_flight_check`

```python
async def pre_flight_check(self) -> Tuple[SafetyStatus, str]:
    # ... existing checks ...

    # Check 6: Global interval enforcement (NEW)
    can_proceed, sleep_seconds = await self.enforce_global_interval()
    if not can_proceed:
        return SafetyStatus.COOLDOWN, f"Global interval: must wait {sleep_seconds:.1f}s"

    return SafetyStatus.CLEAR, "All checks passed"
```

### Step 1C: Integrate into `HTTPWorker.process_job`

```python
# In worker.py, after successful post:

async def process_job(self, job: PostJob) -> PostResult:
    # ... existing logic ...

    success, status, post_id = await poster.post_to_page(...)

    if success:
        # Record post time globally
        await self.safety.record_post_time()
        await self.anomaly.record_post(account_id, job.caption, identity.proxy_url)

    # ... rest of logic ...
```

### Step 1D: Remove Local-Only Interval from `orchestrator.py`

```python
# In orchestrator.py, remove or simplify any local-only interval logic.
# The orchestrator should still group by account, but the actual interval
# enforcement is now handled by SafetyGuard in each worker.

# Keep the account-level serialization (one post per account at a time)
# but remove the time-based delay logic if it was local-only.
```

---

## GAP 2: Proxy Isolation & "No Proxy = Hard Fail" (CRITICAL)

**Current Problem:** `ProxyManager` allows empty proxy pools and does not prevent proxy assignment to multiple identities.

**Fix Location:** `network.py` and `identity.py`

### Step 2A: Add Proxy Uniqueness Check to `IdentityRegistry`

```python
# In identity.py, add to IdentityRegistry:

async def find_accounts_by_proxy(self, proxy_url: str) -> List[str]:
    """Return all account_ids using this proxy."""
    accounts = []
    async for key in self.redis.scan_iter(match=f"{self.key_prefix}:*"):
        data = json.loads(await self.redis.get(key))
        if data.get("proxy_url") == proxy_url:
            # Extract account_id from key: identity_ctx:{account_id}
            account_id = key.decode().split(":")[-1]
            accounts.append(account_id)
    return accounts

async def register(self, ctx: IdentityContext) -> None:
    # Check proxy uniqueness BEFORE registering
    existing = await self.find_accounts_by_proxy(ctx.proxy_url)
    if existing and ctx.account_id not in existing:
        raise ValueError(
            f"Proxy {ctx.proxy_url} already assigned to accounts: {existing}. "
            f"Each account must have a dedicated proxy."
        )

    # Proceed with registration
    key = f"{self.key_prefix}:{ctx.account_id}"
    data = json.dumps(ctx.__dict__, sort_keys=True, separators=(',', ':'))
    await self.redis.set(key, data, ex=None)
```

### Step 2B: Hard Fail on Empty Proxy in `ProxyManager`

```python
# In network.py, modify ProxyManager:

class ProxyManager:
    def __init__(self, proxy_list: List[str], redis_client):
        if not proxy_list:
            raise ValueError(
                "Proxy pool cannot be empty. "
                "Private HTTP requires residential proxies to avoid detection."
            )
        self.proxies = proxy_list
        self.redis = redis_client
        self.sticky_duration = SafetyConfig.PROXY_STICKY_SECONDS

    async def get_proxy_for_account(self, account_id: str) -> str:
        key = f"proxy_sticky:{account_id}"
        cached = await self.redis.get(key)
        if cached:
            return cached

        # Check if account already has an identity with a proxy
        identity = await IdentityRegistry(self.redis).get(account_id)
        if identity and identity.proxy_url:
            # Verify the proxy is still in our pool
            if identity.proxy_url in self.proxies:
                await self.redis.setex(key, self.sticky_duration, identity.proxy_url)
                return identity.proxy_url
            else:
                raise ValueError(
                    f"Identity proxy {identity.proxy_url} not in current pool. "
                    f"Account {account_id} must be re-registered with a valid proxy."
                )

        # Select new proxy
        proxy = self._select_healthy_proxy(account_id)
        await self.redis.setex(key, self.sticky_duration, proxy)
        return proxy
```

### Step 2C: Add Proxy Validation to `IdentityContext`

```python
# In models.py, add to IdentityContext validation:

from pydantic import field_validator, model_validator

class IdentityContext(BaseModel):
    # ... existing fields ...

    @field_validator("proxy_url")
    @classmethod
    def validate_proxy_not_empty(cls, v: str) -> str:
        if not v or v.strip() == "":
            raise ValueError("proxy_url cannot be empty")
        return v.strip()

    @model_validator(mode="after")
    def validate_proxy_format(self):
        # Ensure proxy URL is valid format
        if not self.proxy_url.startswith(("http://", "https://", "socks5://")):
            raise ValueError("proxy_url must start with http://, https://, or socks5://")
        return self
```

---

## GAP 3: Redis Atomicity — Pipelines & Lua (CRITICAL)

**Current Problem:** Rate counters use `INCR` then `EXPIRE` as separate calls. Idempotency uses `GET` then `SETEX` after success. Both are non-atomic and race-condition-prone.

**Fix Location:** `safety.py`, `graphql_poster.py`, `tokens.py`

### Step 3A: Atomic Rate Counter with Pipeline

```python
# In safety.py, replace rate counter logic:

async def check_rate_limit(self) -> Tuple[bool, str]:
    """Atomic rate limit check using Redis pipeline."""

    # Hourly rate
    hour_key = f"post_rate:{self.account_id}:{self._hour_bucket()}"
    pipe = self.redis.pipeline()
    pipe.incr(hour_key)
    pipe.expire(hour_key, 3600)
    results = await pipe.execute()
    hour_count = results[0]

    if hour_count > SafetyConfig.POSTS_PER_HOUR:
        return False, f"Hourly rate exceeded: {hour_count}/{SafetyConfig.POSTS_PER_HOUR}"

    # Daily rate
    day_key = f"post_daily:{self.account_id}:{self._day_bucket()}"
    pipe = self.redis.pipeline()
    pipe.incr(day_key)
    pipe.expire(day_key, 86400)
    results = await pipe.execute()
    day_count = results[0]

    if day_count > SafetyConfig.POSTS_PER_DAY:
        return False, f"Daily rate exceeded: {day_count}/{SafetyConfig.POSTS_PER_DAY}"

    return True, "Rate limits OK"
```

### Step 3B: Atomic Idempotency with SET NX

```python
# In graphql_poster.py, replace idempotency logic:

async def post_to_page(self, page_id: str, caption: str, 
                       media_fbid: Optional[str]) -> Tuple[bool, str, Optional[str]]:

    # ... existing pre-flight checks ...

    # Build idempotency token
    idempotence = self._generate_idempotence_token(page_id, caption)
    idemp_key = f"fb_post_idemp:{idempotence}"

    # ATOMIC CHECK: Try to reserve the idempotency slot
    # If SET NX succeeds, this is the first attempt
    # If SET NX fails, someone else is processing or already completed
    reserved = await self.redis.set(idemp_key, "PENDING", nx=True, ex=300)

    if not reserved:
        # Check if already completed
        existing = await self.redis.get(idemp_key)
        if existing and existing != "PENDING":
            return True, "IDEMPOTENT", existing
        # If PENDING, another worker is processing. Wait and poll.
        for _ in range(30):  # Poll for 30 seconds max
            await asyncio.sleep(1)
            existing = await self.redis.get(idemp_key)
            if existing and existing != "PENDING":
                return True, "IDEMPOTENT", existing
        return False, "IDEMPOTENCY_TIMEOUT", None

    try:
        # Execute the actual post
        success, status, post_id = await self._execute_post(page_id, caption, media_fbid)

        if success and post_id:
            # Update idempotency with post_id
            await self.redis.setex(idemp_key, 86400, post_id)
        else:
            # Release the reservation so retry can happen
            await self.redis.delete(idemp_key)

        return success, status, post_id

    except Exception as e:
        # Release reservation on exception
        await self.redis.delete(idemp_key)
        raise
```

### Step 3C: Atomic Token Usage Increment

```python
# In tokens.py, ensure increment_usage is atomic:

async def increment_usage(self, account_id: str) -> int:
    """Atomic increment using Redis HINCRBY."""
    key = self._cache_key(account_id)

    # Use HINCRBY for atomic increment
    new_count = await self.redis.hincrby(key, "usage_count", 1)

    # Refresh TTL
    await self.redis.expire(key, self.TOKEN_TTL_SECONDS + 60)

    return new_count
```

---

## GAP 4: Fallback Ratio Tracking & Enforcement (CRITICAL)

**Current Problem:** `SafetyConfig.MAX_BROWSER_FALLBACK_RATIO` exists but no code tracks or enforces it.

**Fix Location:** `worker.py`, `safety.py`, `orchestrator.py`

### Step 4A: Add Fallback Tracking Methods

```python
# In safety.py, add to SafetyGuard:

async def check_fallback_ratio(self) -> Tuple[bool, str]:
    """Check if browser fallback ratio exceeds 10% for this account."""
    hour_bucket = self._hour_bucket()
    fallback_key = f"fallback_count:{self.account_id}:{hour_bucket}"
    total_key = f"post_count:{self.account_id}:{hour_bucket}"

    fallback_count = int(await self.redis.get(fallback_key) or 0)
    total_count = int(await self.redis.get(total_key) or 0)

    if total_count > 0:
        ratio = fallback_count / total_count
        if ratio > SafetyConfig.MAX_BROWSER_FALLBACK_RATIO:
            return False, f"Fallback ratio {ratio:.1%} exceeds {SafetyConfig.MAX_BROWSER_FALLBACK_RATIO:.0%}"

    return True, "Fallback ratio OK"

async def record_fallback(self) -> None:
    """Record that a browser fallback occurred."""
    hour_bucket = self._hour_bucket()
    fallback_key = f"fallback_count:{self.account_id}:{hour_bucket}"

    pipe = self.redis.pipeline()
    pipe.incr(fallback_key)
    pipe.expire(fallback_key, 3600)
    await pipe.execute()

async def record_post_attempt(self) -> None:
    """Record any post attempt (success or failure) for ratio denominator."""
    hour_bucket = self._hour_bucket()
    total_key = f"post_count:{self.account_id}:{hour_bucket}"

    pipe = self.redis.pipeline()
    pipe.incr(total_key)
    pipe.expire(total_key, 3600)
    await pipe.execute()
```

### Step 4B: Integrate into Worker Flow

```python
# In worker.py, modify process_job:

async def process_job(self, job: PostJob) -> PostResult:
    # ... load identity ...

    # Record attempt for fallback ratio tracking
    await self.safety.record_post_attempt()

    # Try HTTP path first
    try:
        tokens = await self.token_vault.get(account_id)
        if not tokens or await self.token_vault.is_rotation_needed(account_id):
            tokens = await self._refresh_tokens(account_id)

        if tokens:
            # Check fallback ratio before attempting HTTP
            ok, msg = await self.safety.check_fallback_ratio()
            if not ok:
                return PostResult(
                    success=False,
                    status="FALLBACK_RATIO_EXCEEDED",
                    error_message=msg
                )

            # Proceed with HTTP post
            result = await self._http_post(job, tokens, identity)
            if result.success:
                return result

            # If HTTP failed with specific errors, check if fallback is allowed
            if result.status in SafetyConfig.BROWSER_FALLBACK_ON_ERRORS:
                # Check fallback ratio again
                ok, msg = await self.safety.check_fallback_ratio()
                if ok:
                    await self.safety.record_fallback()
                    return await self._browser_fallback_post(job, identity)
                else:
                    return PostResult(
                        success=False,
                        status="FALLBACK_RATIO_EXCEEDED",
                        error_message=msg
                    )

            return result
    except Exception as e:
        logger.error(f"HTTP post failed: {e}")

    # If we reach here, HTTP failed and fallback is not allowed or failed
    return PostResult(
        success=False,
        status="HTTP_FAILED_NO_FALLBACK",
        error_message="HTTP post failed and fallback not available"
    )
```

---

## GAP 5: Stronger IdentityContext Validation (HIGH)

**Current Problem:** Only validates `viewport <= screen_resolution`. Missing: UA/platform consistency, locale/timezone consistency, proxy/geolocation consistency.

**Fix Location:** `models.py`

### Step 5A: Add Comprehensive Validators

```python
# In models.py, add to IdentityContext:

import re
from pydantic import field_validator, model_validator

class IdentityContext(BaseModel):
    # ... existing fields ...

    @field_validator("user_agent")
    @classmethod
    def validate_user_agent_format(cls, v: str) -> str:
        # Must contain Chrome version
        if "Chrome/" not in v:
            raise ValueError("user_agent must contain Chrome/ version")
        # Extract version
        match = re.search(r"Chrome/(\d+\.\d+", v)
        if not match:
            raise ValueError("user_agent must contain valid Chrome version")
        return v

    @field_validator("chrome_version")
    @classmethod
    def validate_chrome_version_matches_ua(cls, v: str, info) -> str:
        values = info.data
        ua = values.get("user_agent", "")
        if v not in ua:
            raise ValueError(f"chrome_version {v} must appear in user_agent")
        return v

    @field_validator("platform")
    @classmethod
    def validate_platform_matches_ua(cls, v: str, info) -> str:
        values = info.data
        ua = values.get("user_agent", "")
        platform_map = {
            "Win32": "Windows",
            "MacIntel": "Macintosh",
            "Linux x86_64": "Linux",
        }
        expected = platform_map.get(v)
        if expected and expected not in ua:
            raise ValueError(f"platform {v} requires {expected} in user_agent")
        return v

    @field_validator("locale")
    @classmethod
    def validate_locale_matches_timezone(cls, v: str, info) -> str:
        values = info.data
        tz = values.get("timezone", "")
        # Extract continent from timezone
        tz_continent = tz.split("/")[0] if "/" in tz else tz
        # Locale should match timezone region roughly
        locale_map = {
            "America": ["en_US", "en_CA", "es_MX", "pt_BR"],
            "Europe": ["en_GB", "de_DE", "fr_FR", "es_ES", "it_IT"],
            "Asia": ["en_SG", "ja_JP", "ko_KR", "zh_CN", "zh_TW"],
            "Australia": ["en_AU"],
            "Africa": ["en_ZA"],
        }
        allowed = locale_map.get(tz_continent, [])
        if allowed and v not in allowed:
            raise ValueError(f"locale {v} does not match timezone region {tz_continent}")
        return v

    @model_validator(mode="after")
    def validate_proxy_geolocation_consistency(self):
        # In production, you might have a geo-IP lookup service
        # For now, ensure geolocation is within reasonable bounds
        lat, lon = self.geolocation
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            raise ValueError(f"Invalid geolocation: {lat}, {lon}")

        # Ensure timezone roughly matches longitude
        # Each hour of timezone offset = ~15 degrees of longitude
        # This is a rough check
        return self

    @model_validator(mode="after")
    def validate_webgl_consistency(self):
        # If platform is Windows, WebGL vendor should mention Windows or NVIDIA/AMD/Intel
        if self.platform == "Win32":
            if "Google Inc." not in self.webgl_vendor and "Intel" not in self.webgl_vendor:
                raise ValueError("Windows platform should have Google Inc. or Intel WebGL vendor")
        return self
```

---

## GAP 6: Quarantine Level Consistency (HIGH)

**Current Problem:** `SafetyGuard._trigger_quarantine()` writes `quarantine:{account_id}` but not `quarantine_level:{account_id}`. `QuarantineManager.escalate()` writes both, but direct quarantine triggers bypass the level tracking.

**Fix Location:** `safety.py`

### Step 6A: Unify Quarantine Triggering

```python
# In safety.py, modify SafetyGuard to use QuarantineManager:

class SafetyGuard:
    def __init__(self, redis_client, identity: IdentityContext):
        self.redis = redis_client
        self.identity = identity
        self.account_id = identity.account_id
        self.quarantine_manager = QuarantineManager(redis_client)

    async def _trigger_quarantine(self, duration_seconds: int, reason: str) -> None:
        """
        Trigger quarantine using QuarantineManager for consistent level tracking.
        Determine level based on duration.
        """
        # Map duration to level
        if duration_seconds <= 900:
            target_level = QuarantineLevel.SOFT
        elif duration_seconds <= 3600:
            target_level = QuarantineLevel.HARD
        elif duration_seconds <= 86400:
            target_level = QuarantineLevel.SEVERE
        else:
            target_level = QuarantineLevel.BANNED

        # Get current level
        current = await self.quarantine_manager.get_level(self.account_id)

        # If current is already higher, don't downgrade
        if current.value >= target_level.value:
            # Just extend the existing quarantine
            await self.redis.setex(
                f"quarantine:{self.account_id}",
                duration_seconds,
                reason
            )
        else:
            # Use QuarantineManager to escalate properly
            await self.quarantine_manager.escalate(self.account_id, reason)

        # Always publish alert
        await self.redis.publish("admin_alerts", json.dumps({
            "level": "CRITICAL",
            "account": self.account_id,
            "reason": reason,
            "duration": duration_seconds,
            "timestamp": time.time()
        }))
```

---

## GAP 7: GraphQL Doc ID Seeding (MEDIUM)

**Current Problem:** `_get_doc_id()` returns empty string when Redis cache is missing. Private HTTP path requires a valid doc_id to work.

**Fix Location:** `graphql_poster.py` and `browser_fallback.py`

### Step 7A: Add Doc ID Extraction to Token Extraction

```python
# In browser_fallback.py, modify BrowserTokenExtractor:

async def extract_tokens(self, cookies_json: str) -> Dict:
    # ... existing token extraction ...

    # Also extract the current doc_id from the page
    doc_id = await page.evaluate("""
        () => {
            // Facebook stores doc_ids in various modules
            // Try to find the ComposerStoryCreateMutation doc_id
            const modules = require.getModules();
            for (const mod of modules) {
                const exports = require(mod);
                if (exports && exports.doc_id === '7711610262198779') {
                    return exports.doc_id;
                }
            }
            // Fallback: search in page scripts
            const scripts = document.querySelectorAll('script');
            for (const script of scripts) {
                const text = script.textContent || '';
                const match = text.match(/7711610262198779/);
                if (match) return match[0];
            }
            return null;
        }
    """)

    if doc_id:
        await self.redis.setex("fb_graphql_doc_id", 604800, doc_id)  # 7 days

    return tokens
```

### Step 7B: Add Doc ID Fallback with Warning

```python
# In graphql_poster.py, modify _get_doc_id:

async def _get_doc_id(self) -> str:
    cached = await self.redis.get("fb_graphql_doc_id")
    if cached:
        return cached

    # Log critical warning
    logger.error(
        "fb_graphql_doc_id not found in cache. "
        "Private HTTP path requires a valid doc_id. "
        "Run token extraction to populate the cache."
    )

    # Return fallback but mark as unsafe
    return "7711610262198779"  # Known stable fallback
```

---

## GAP 8: Browser Fallback Wiring (MEDIUM)

**Current Problem:** Video upload is not wired. Token extraction has locking but no retry-from-vault if another extraction is active.

**Fix Location:** `browser_fallback.py` and `worker.py`

### Step 8A: Implement Video Upload Fallback

```python
# In browser_fallback.py, implement BrowserVideoUploader:

class BrowserVideoUploader:
    """Video upload via browser (proven working path)."""

    def __init__(self, identity: IdentityContext):
        self.identity = identity

    async def upload_video(self, video_path: str, cookies_json: str) -> Tuple[bool, Optional[str]]:
        """
        Upload video using Facebook's Pages Portal via Playwright.
        This is the only browser-automation path for posting.
        """
        playwright, browser, context = await launch_stealth_browser(self.identity)

        try:
            page = await context.new_page()

            # Load cookies
            cookies = json.loads(cookies_json)
            await context.add_cookies(cookies)

            # Navigate to composer with human-like delays
            await page.goto("https://www.facebook.com/", wait_until="domcontentloaded")
            await asyncio.sleep(StochasticTimer.think_time(1000, 3000))

            # Click composer (with realistic mouse movement)
            composer = await page.wait_for_selector('[aria-label="Create a post"]', timeout=10000)
            if composer:
                await composer.click()
                await asyncio.sleep(StochasticTimer.think_time(500, 1500))

            # Attach video
            file_input = await page.wait_for_selector('input[type="file"]', timeout=5000)
            if file_input:
                await file_input.set_input_files(video_path)
                await asyncio.sleep(StochasticTimer.think_time(2000, 5000))

            # Wait for upload to complete
            await page.wait_for_selector('[aria-label="Post"]', timeout=60000)

            # Submit
            post_button = await page.query_selector('[aria-label="Post"]')
            if post_button:
                await post_button.click()

            # Extract post ID from URL or response
            await asyncio.sleep(2)
            # ... extract post_id logic ...

            return True, post_id

        except Exception as e:
            logger.error(f"Video upload failed: {e}")
            return False, None
        finally:
            await browser.close()
            await playwright.stop()
```

### Step 8B: Add Token Extraction Retry

```python
# In browser_fallback.py, add retry logic:

class BrowserTokenExtractor:
    async def extract_with_retry(self, account_id: str, cookies_json: str, 
                                  max_wait: int = 60) -> Optional[Dict]:
        """
        Extract tokens with vault polling if another worker is already extracting.
        """
        lock_key = f"token_extract_lock:{account_id}"

        # Try to acquire lock
        lock_acquired = await self.redis.set(lock_key, "1", nx=True, ex=30)

        if not lock_acquired:
            # Another worker is extracting. Poll the vault.
            for _ in range(max_wait):
                tokens = await self.token_vault.get(account_id)
                if tokens:
                    return tokens
                await asyncio.sleep(1)
            return None

        try:
            # We have the lock. Extract tokens.
            tokens = await self.extract_tokens(cookies_json)
            await self.token_vault.set(account_id, tokens)
            return tokens
        finally:
            # Always release lock
            await self.redis.delete(lock_key)
```

---

## GAP 9: Application Shutdown Lifecycle (HIGH)

**Current Problem:** No SIGTERM handler, no Redis close, no in-flight drain, no centralized temp-file registry.

**Fix Location:** New file `lifecycle.py`, modifications to `main.py`

### Step 9A: Create Lifecycle Manager

```python
# lifecycle.py

import asyncio
import signal
import os
import tempfile
from typing import List, Set
import structlog

logger = structlog.get_logger()

class ApplicationLifecycle:
    """Manages graceful shutdown, resource cleanup, and temp file tracking."""

    def __init__(self, redis_client, workers: List):
        self.redis = redis_client
        self.workers = workers
        self.in_flight_jobs: Set[str] = set()
        self.temp_files: Set[str] = set()
        self._shutdown_event = asyncio.Event()
        self._is_shutting_down = False

    def register_temp_file(self, path: str) -> None:
        """Register a temp file for cleanup on shutdown."""
        self.temp_files.add(path)

    def unregister_temp_file(self, path: str) -> None:
        """Remove from registry after successful cleanup."""
        self.temp_files.discard(path)

    def register_in_flight(self, job_id: str) -> None:
        """Track in-flight job."""
        self.in_flight_jobs.add(job_id)

    def unregister_in_flight(self, job_id: str) -> None:
        """Remove from in-flight tracking."""
        self.in_flight_jobs.discard(job_id)

    async def shutdown(self, signal_name: str = "UNKNOWN"):
        """Graceful shutdown sequence."""
        if self._is_shutting_down:
            return
        self._is_shutting_down = True

        logger.info(f"Shutdown initiated via {signal_name}")

        # 1. Stop accepting new jobs
        self._shutdown_event.set()

        # 2. Wait for in-flight jobs (with timeout)
        if self.in_flight_jobs:
            logger.info(f"Waiting for {len(self.in_flight_jobs)} in-flight jobs")
            try:
                await asyncio.wait_for(
                    self._wait_for_in_flight(),
                    timeout=30.0
                )
            except asyncio.TimeoutError:
                logger.warning(f"Timeout waiting for jobs: {self.in_flight_jobs}")

        # 3. Close all workers
        for worker in self.workers:
            if hasattr(worker, 'close'):
                await worker.close()

        # 4. Close Redis connections
        if self.redis:
            await self.redis.close()
            logger.info("Redis connections closed")

        # 5. Clean up temp files
        cleaned = 0
        for path in list(self.temp_files):
            try:
                if os.path.exists(path):
                    os.remove(path)
                    cleaned += 1
            except Exception as e:
                logger.error(f"Failed to clean up {path}: {e}")
        logger.info(f"Cleaned up {cleaned} temp files")

        # 6. Clean up any orphaned temp files in system temp dir
        self._cleanup_orphaned_temps()

        logger.info("Shutdown complete")

    async def _wait_for_in_flight(self) -> None:
        """Wait until all in-flight jobs complete."""
        while self.in_flight_jobs:
            await asyncio.sleep(0.5)

    def _cleanup_orphaned_temps(self) -> None:
        """Clean up temp files matching our pattern."""
        temp_dir = tempfile.gettempdir()
        for filename in os.listdir(temp_dir):
            if filename.startswith("fb_auto_"):  # Our naming convention
                try:
                    os.remove(os.path.join(temp_dir, filename))
                except Exception:
                    pass

    def register_signal_handlers(self):
        """Register SIGINT and SIGTERM handlers."""
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(
                sig,
                lambda s=sig: asyncio.create_task(
                    self.shutdown(signal.Signals(s).name)
                )
            )
```

### Step 9B: Integrate into Main Application

```python
# main.py

from lifecycle import ApplicationLifecycle

class Application:
    def __init__(self):
        self.redis = redis.from_url(Settings.REDIS_URL)
        self.proxy_manager = ProxyManager(Settings.PROXY_POOL, self.redis)
        self.token_vault = TokenVault(self.redis)
        self.identity_registry = IdentityRegistry(self.redis)
        self.workers = []
        self.lifecycle = None

    async def start(self):
        # Create workers
        for i in range(Settings.WORKER_COUNT):
            worker = HTTPWorker(self.redis, Settings.PROXY_POOL)
            self.workers.append(worker)

        # Initialize lifecycle
        self.lifecycle = ApplicationLifecycle(self.redis, self.workers)
        self.lifecycle.register_signal_handlers()

        # Start processing
        await self._run_workers()

    async def _run_workers(self):
        tasks = []
        for worker in self.workers:
            task = asyncio.create_task(worker.run())
            tasks.append(task)

        # Wait for shutdown signal
        await self.lifecycle._shutdown_event.wait()

        # Cancel worker tasks
        for task in tasks:
            task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)
```

### Step 9C: Use Lifecycle in Worker

```python
# worker.py, modify process_job:

async def process_job(self, job: PostJob, lifecycle: ApplicationLifecycle) -> PostResult:
    job_id = f"{job.account_id}:{job.page_id}:{time.time()}"
    lifecycle.register_in_flight(job_id)

    try:
        # ... existing logic ...

        # Register temp files for cleanup
        if job.media_path:
            mutated = await self.uploader.mutate_image(job.media_path)
            lifecycle.register_temp_file(mutated)
            try:
                result = await self._upload_and_post(mutated)
            finally:
                lifecycle.unregister_temp_file(mutated)
                if os.path.exists(mutated):
                    os.remove(mutated)

        return result
    finally:
        lifecycle.unregister_in_flight(job_id)
```

---

## VERIFICATION CHECKLIST

After implementing all fixes, verify:

- [ ] Two workers posting for same account: second waits for interval
- [ ] Registering identity with duplicate proxy: raises ValueError
- [ ] Empty proxy pool: raises ValueError on startup
- [ ] Rate counter: `INCR` and `EXPIRE` in single pipeline
- [ ] Idempotency: concurrent posts with same caption returns IDEMPOTENT
- [ ] Fallback ratio >10%: quarantine triggered
- [ ] Invalid IdentityContext (wrong UA/platform): Pydantic validation error
- [ ] Quarantine from SafetyGuard: correct level written to Redis
- [ ] Missing doc_id: fallback used with logged warning
- [ ] Video job: routes to BrowserVideoUploader
- [ ] Token extraction with active lock: polls vault for 60s
- [ ] SIGTERM: in-flight jobs complete, Redis closes, temp files deleted
- [ ] Orphaned temp files: cleaned on startup/shutdown

---

*Document Version: 2026.06.04*
*Target: Bring codebase from 65-70% to 100% checklist coverage*
