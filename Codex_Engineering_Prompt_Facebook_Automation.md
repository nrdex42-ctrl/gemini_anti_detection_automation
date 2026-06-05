# ENGINEERING PROMPT — Facebook Automation System Implementation
## For: AI Coding Assistant (Codex / Claude / o3 / etc.)
## Classification: AUTHORIZED AUTOMATION — User Assumes Full Responsibility

---

## ROLE & CONTEXT
You are a senior backend engineer implementing a **production-grade Facebook automation system** for authorized social media management. The user has provided you with a full architecture document and takes **100% legal, ethical, and operational responsibility** for its deployment and use. Your job is to implement the code exactly as specified, with zero deviation from the safety guardrails.

**CRITICAL RULE:** If any instruction conflicts with platform Terms of Service or safety best practices, implement the **strictest** version of the safety guardrail and flag it in comments. Do not omit safety checks to "simplify" the code.

---

## TECH STACK
- **Language:** Python 3.11+
- **Async Framework:** `asyncio` + `aiohttp` (for HTTP layer)
- **Browser Automation:** `playwright` (fallback ONLY for video uploads and token extraction)
- **Cache & Queue:** `redis` (redis-py with asyncio support)
- **Image Processing:** `Pillow` (PIL)
- **Logging:** `structlog` + standard logging
- **Configuration:** Pydantic `BaseSettings` for env-based config
- **Data Validation:** `pydantic` v2 for all input/output models

---

## PROJECT STRUCTURE
Implement the following file tree exactly:

```
fb_automation/
├── __init__.py
├── config.py                 # Pydantic settings + SafetyConfig + EthicalGuardrails
├── models.py                 # Pydantic models: IdentityContext, PostJob, PostResult, etc.
├── identity.py               # IdentityRegistry + IdentityContext dataclass
├── tokens.py                 # TokenVault (L1/L2 cache, rotation detection)
├── timing.py                 # StochasticTimer + ActionRandomizer
├── network.py                # ProxyManager + StealthConnector + HeaderForge
├── safety.py                 # SafetyGuard + SafetyStatus enum + QuarantineManager
├── rupload.py                # HardenedRupload (image upload via rupload.facebook.com)
├── graphql_poster.py         # HardenedGraphQLPoster (post creation via GraphQL)
├── browser_fallback.py       # Playwright token extraction + video upload fallback
├── anomaly.py                # AnomalyDetector
├── audit.py                  # AuditLogger (Redis Streams)
├── checkpoint.py             # CheckpointRecovery (human-in-the-loop escalation)
├── worker.py                 # HTTPWorker (stateless job processor)
├── orchestrator.py           # Queue manager + batch dispatcher
├── utils.py                  # Helpers: cookie conversion, ID generators, hash utils
├── tests/
│   ├── __init__.py
│   ├── test_identity.py
│   ├── test_tokens.py
│   ├── test_safety.py
│   ├── test_rupload.py
│   └── test_graphql.py
└── main.py                   # Entry point: CLI or FastAPI service wrapper
```

---

## PHASE 1: FOUNDATION (Implement First)

### 1.1 `config.py` — Immutable Configuration
Implement three classes:
- `AppConfig(BaseSettings)`: Redis URL, proxy pool list, admin webhook, log level, worker concurrency
- `SafetyConfig`: Hardcoded rate limits (6 posts/hour, 20/day, 120s min interval), token TTL (240s), quarantine durations (SOFT=900s, HARD=3600s, SEVERE=86400s), max image size (15MB), proxy sticky time (600s)
- `EthicalGuardrails`: ABSOLUTE_MAX_POSTS_PER_DAY=50 (non-configurable), ABSOLUTE_MIN_INTERVAL_SECONDS=60.0 (non-configurable), prohibited content hash set, content validation methods (`validate_content`, `validate_velocity`)

**CRITICAL:** `EthicalGuardrails` values must be **hardcoded constants** — not env vars. They must be impossible to override via configuration.

### 1.2 `models.py` — Data Contracts
Implement Pydantic v2 models:
- `IdentityContext`: frozen dataclass with all identity fields (account_id, proxy_url, user_agent, viewport, timezone, locale, geolocation, screen_resolution, color_depth, platform, chrome_version, webgl_vendor, webgl_renderer, fonts, audio_sample_rate, facebook_user_id). Include validation that viewport <= screen_resolution.
- `PostJob`: account_id, page_id, caption, media_url (optional), post_type (text/image/video), scheduled_at (optional)
- `PostResult`: success, status, post_id, page_id, error_message, execution_time_ms
- `TokenBundle`: fb_dtsg, lsd, user_id, xs, revision, timestamp
- `QuarantineRecord`: account_id, level, reason, expires_at, created_at

### 1.3 `utils.py` — Utilities
Implement:
- `cookies_json_to_header(cookies_json: str) -> str`: Convert stored cookie JSON to "name=value; name2=value2" format
- `generate_client_id(account_id: str) -> str`: 16-char hex hash mimicking Facebook internal format
- `generate_idempotence_token(account_id, page_id, caption, time_bucket_minutes=10) -> str`: Deterministic SHA256 hash with time bucket
- `extract_page_id(page_url_or_id: str) -> str`: Handle `profile.php?id=...` and vanity URLs
- `classify_error(error_msg: str) -> str`: Map to `SECURITY_CHECKPOINT`, `TOKEN_EXPIRED`, `UPLOAD_REJECTED`, `RATE_LIMITED`, `GRAPHQL_ERROR`
- `sanitize_caption(caption: str) -> str`: Strip excessive whitespace, enforce max length (5000 chars)

---

## PHASE 2: IDENTITY & SESSION (Implement Second)

### 2.1 `identity.py` — Identity Registry
Implement:
- `IdentityContext` dataclass (frozen=True) with `to_browser_args()` method returning Playwright context options
- `IdentityRegistry` class with async methods:
  - `register(ctx: IdentityContext) -> None`: Store in Redis key `identity_ctx:{account_id}` as persistent JSON
  - `get(account_id: str) -> Optional[IdentityContext]`
  - `is_proxy_unique(proxy_url: str, exclude_account: str) -> bool`: Scan all identities to prevent proxy sharing
  - `list_all() -> List[IdentityContext]`

### 2.2 `tokens.py` — Token Lifecycle Management
Implement `TokenVault` class:
- `TOKEN_TTL_SECONDS = 240`
- L1 cache: in-process dict (`self._local_cache`)
- L2 cache: Redis with key `fb_tokens:{account_id}`
- Methods:
  - `get(account_id) -> Optional[Dict]`: Check L1 then L2, validate expiry
  - `set(account_id, tokens) -> None`: Store with hash, usage_count=0, timestamp
  - `increment_usage(account_id) -> int`: Atomic increment in Redis
  - `is_rotation_needed(account_id) -> bool`: True if age > 180s OR usage > 20
  - `_hash_tokens(tokens) -> str`: SHA256 of canonical JSON for rotation detection

---

## PHASE 3: BEHAVIORAL & NETWORK (Implement Third)

### 3.1 `timing.py` — Stochastic Timing Engine
Implement `StochasticTimer` with static methods:
- `think_time(min_ms=800, max_ms=4500, focus_factor=1.0) -> float`: Log-normal distribution, convert to seconds
- `typing_delay(char_count: int, wpm=65.0) -> List[float]`: Per-character delays with 15% pause probability, fatigue slowdown over long text
- `scroll_pattern(total_height: int) -> List[Tuple[int, float]]`: Phase-based scroll (fast/detailed/fast) with realistic pixel deltas and delays
- `post_interval(base_seconds: float, account_age_days: int) -> float`: Age multiplier (0-30 days=3x, 30-90=1.5x, 90+=1x) + ±25% jitter

Implement `ActionRandomizer`:
- `randomize_post_workflow() -> List[str]`: Return one of 4+ randomized action sequences
- `add_noise_actions(actions, noise_probability=0.15) -> List[str]`: Inject benign noise actions (hover_notification, click_profile_pic, scroll_feed, pause)

### 3.2 `network.py` — Network Fingerprint Evasion
Implement three classes:

**`ProxyManager`**:
- `__init__(proxy_list: List[str], redis_client)`
- `get_proxy_for_account(account_id) -> str`: Sticky assignment via `proxy_sticky:{account_id}` with 600s TTL
- `report_failure(account_id, proxy, error) -> None`: Increment `proxy_health:{proxy}` failures, force rotation on >3 failures
- `_select_healthy_proxy(account_id) -> str`: Hash-based selection from pool

**`StealthConnector`**:
- `create_connector() -> TCPConnector`: Chrome 126 cipher suite ordering, TLS 1.2-1.3, OCSP stapling, connection reuse enabled, DNS cache TTL=300

**`HeaderForge`**:
- `forge_graphql_headers(tokens, identity) -> Dict[str, str]`: Exact Chrome headers including `sec-ch-ua`, `sec-fetch-*`, `x-fb-lsd`, `x-fb-friendly-name`
- `forge_rupload_headers(tokens, identity, file_size, offset=0) -> Dict[str, str]`: rupload-specific headers including `x-fb-upload-filesize`, `x-fb-upload-offset`

---

## PHASE 4: SAFETY & QUARANTINE (Implement Fourth)

### 4.1 `safety.py` — Safety Guard + Quarantine
Implement `SafetyStatus` enum: `CLEAR`, `QUARANTINE`, `COOLDOWN`, `CHECKPOINT`

Implement `SafetyGuard`:
- `__init__(redis_client, identity)`
- `pre_flight_check() -> Tuple[SafetyStatus, str]`:
  1. Check `quarantine:{account_id}` exists → QUARANTINE
  2. Check `post_rate:{account_id}:{hour}` > 6 → COOLDOWN
  3. Check `post_daily:{account_id}:{day}` > 20 → COOLDOWN
  4. Check `proxy_health:{proxy}` failures > 3 → QUARANTINE
  5. Check `TokenVault.is_rotation_needed()` → COOLDOWN
- `post_flight_validation(success, response_code, error_message) -> SafetyStatus`: Pattern-match errors for checkpoint/security/1366046 and trigger quarantine via `_trigger_quarantine()`
- `_trigger_quarantine(duration_seconds, reason) -> None`: Set Redis quarantine key, publish to `admin_alerts` channel

Implement `QuarantineManager`:
- `QuarantineLevel` enum: `NONE`, `SOFT`, `HARD`, `SEVERE`, `BANNED`
- `ESCALATION_MATRIX`: NONE→SOFT(900s), SOFT→HARD(3600s), HARD→SEVERE(86400s), SEVERE→BANNED(permanent)
- `get_level(account_id) -> QuarantineLevel`
- `escalate(account_id, reason) -> QuarantineLevel`: Move to next level, set Redis keys, log to `quarantine_log` stream, publish CRITICAL admin alert for HARD+
- `reset(account_id, admin_override=False) -> None`: Verify TTL expired unless admin_override, delete quarantine keys, reset health streak

### 4.2 `checkpoint.py` — Human Escalation
Implement `CheckpointRecovery`:
- `handle_checkpoint(account_id, checkpoint_url, screenshot_bytes) -> None`: 
  1. Quarantine to SEVERE via QuarantineManager
  2. Store artifact in `checkpoint_artifact:{account_id}` with base64 screenshot
  3. Send webhook to `ADMIN_WEBHOOK_URL`
  4. **NEVER** attempt to solve the checkpoint automatically — always escalate to human
- `_send_webhook(payload) -> None`: POST to admin webhook with 10s timeout

### 4.3 `anomaly.py` — Anomaly Detection
Implement `AnomalyDetector`:
- `check_anomalies(account_id) -> List[str]`: Check for `HIGH_FAILURE_RATE` (7+ failures in last 10), `TEMPORAL_CLUSTERING` (avg interval < 5s), `CONTENT_DUPLICATION` (<50% unique caption hashes), `PROXY_HOPPING` (>2 unique proxies in last 5)
- `record_post(account_id, caption, proxy) -> None`: Pipeline LPUSH to `post_times`, `captions`, `proxy_used` lists with LTRIM to 100 items

### 4.4 `audit.py` — Audit Logging
Implement `AuditLogger`:
- `log_action(account_id, action, metadata, outcome) -> None`: Write to Redis Stream `audit:actions` with maxlen=100000, also log via structlog
- `get_account_history(account_id, limit=100) -> List[dict]`

---

## PHASE 5: CORE AUTOMATION (Implement Fifth)

### 5.1 `rupload.py` — Hardened Image Upload
Implement `HardenedRupload`:
- `__init__(token_vault, identity, redis_client, proxy_manager)`
- `upload_image(image_path: str) -> Tuple[bool, Optional[str], str]`:
  1. Validate image: min 100x100, size 1KB-15MB, valid format via PIL
  2. Mutate fingerprint: `_mutate_image_fingerprint()` → re-encode JPEG with quality 88-95%, ±1px resize, save to temp
  3. Extract tokens from TokenVault
  4. Jitter sleep 200-800ms before init
  5. POST to `rupload.facebook.com/video/upload_init` with forged headers + proxy
  6. POST file bytes to `rupload.facebook.com/video/upload/{upload_id}` with `content-length`
  7. Return `(True, media_fbid, "Success")` or `(False, None, error)`
  8. Cleanup temp file in `finally` block
  9. On exception: report proxy failure
- `_mutate_image_fingerprint(path) -> str`: PIL re-encode with jitter
- `_validate_image(path) -> bool`: PIL verify + dimension/size checks
- `_generate_client_id() -> str`: 16-char hex

### 5.2 `graphql_poster.py` — Hardened GraphQL Posting
Implement `HardenedGraphQLPoster`:
- `__init__(token_vault, identity, redis_client, proxy_manager)`
- `post_to_page(page_id, caption, media_fbid) -> Tuple[bool, str, Optional[str]]`:
  1. Run `SafetyGuard.pre_flight_check()` → fail fast if not CLEAR
  2. Check idempotency: `fb_post_idemp:{hash}` in Redis → return IDEMPOTENT
  3. Build GraphQL payload with exact structure:
     - `input.composer_entry_point`: "inline"
     - `input.composer_source_surface`: "composer"
     - `input.idempotence_token`: generated hash
     - `input.message.ranges`: []
     - `input.actor_id`: page_id
     - `input.client_mutation_id`: millisecond timestamp
     - `input.attachments`: `[{"media_fbid": media_fbid}]` if present
     - `doc_id`: from Redis cache or fallback "7711610262198779"
     - `__req`: base64-ish counter
     - `__a`: "1"
     - `__user`: facebook_user_id or "0"
  4. Jitter sleep 100-500ms
  5. POST to `https://www.facebook.com/api/graphql/` with forged headers + proxy
  6. Strip `for(;;);` prefix if present, parse JSON
  7. Run `post_flight_validation()`
  8. Extract `post_id` from `data.composer_story_create.story.legacy_story_hideable_id` or `post_id`
  9. Cache idempotency for 24h
  10. Return `(True, "SUCCESS", post_id)` or `(False, classified_error, None)`
- `_generate_idempotence_token(page_id, caption) -> str`: SHA256 of account+page+caption_hash+10-min time bucket
- `_get_doc_id() -> str`: Redis `fb_graphql_doc_id` or fallback
- `_generate_req_param() -> str`: base64 encoded counter
- `_classify_error(error_msg) -> str`: Map to canonical error codes

### 5.3 `browser_fallback.py` — Token Extraction & Video Fallback
Implement browser fallback for **two purposes only**:
1. **Token extraction** (when TokenVault is empty or rotation needed)
2. **Video upload** (when post_type == "video")

Implement `BrowserTokenExtractor`:
- `extract_tokens(cookies_json: str) -> Dict`: Launch Playwright with stealth patches (navigator.webdriver=false, plugins mocked, WebGL consistent with IdentityContext), navigate to `https://www.facebook.com/me`, execute JS to extract `fb_dtsg`, `lsd`, `user_id`, `xs`, `revision` from `DTSGInitialData`, `LSD`, `CurrentUserInitialData` modules or DOM inputs. Cache in TokenVault. Close browser immediately.

Implement `BrowserVideoUploader`:
- `upload_video(video_path, cookies_json, identity) -> Tuple[bool, Optional[str]]`: Use existing working Pages Portal flow via Playwright. Do NOT use rupload for video (proven 4/4 success in logs). Add human-like delays between actions.

**CRITICAL:** Browser instances must be launched with the exact `IdentityContext.to_browser_args()` and additional stealth args:
- `--disable-blink-features=AutomationControlled`
- `--disable-dev-shm-usage`
- `--no-sandbox` (only if containerized)
- User Data Dir isolation per account

---

## PHASE 6: WORKER & ORCHESTRATION (Implement Sixth)

### 6.1 `worker.py` — Stateless HTTP Worker
Implement `HTTPWorker`:
- `__init__(redis_url, proxy_pool)`
- `process_job(job: PostJob) -> PostResult`:
  1. Load identity from IdentityRegistry
  2. Run AnomalyDetector pre-check → reject if anomalies found
  3. If media_path: HardenedRupload.upload_image() → get media_fbid
  4. HardenedGraphQLPoster.post_to_page() → get result
  5. If success: AnomalyDetector.record_post()
  6. AuditLogger.log_action()
  7. Return PostResult

### 6.2 `orchestrator.py` — Batch Dispatcher
Implement `BatchOrchestrator`:
- `dispatch_batch(posts: List[PostJob]) -> List[PostResult]`:
  - Group by account_id (never mix accounts in a single proxy/session)
  - For each account: enforce `MIN_POST_INTERVAL_SECONDS` between posts
  - For text/image: use HTTPWorker (stateless, concurrent across accounts)
  - For video: route to BrowserVideoUploader (sequential, limited concurrency)
  - Apply `StochasticTimer.post_interval()` delays between posts for same account
  - Collect results, log failures, trigger quarantine on checkpoint errors
- `create_facebook_posts_fast(cookies_json, posts, progress_callback) -> List[dict]`: Main entry point matching original API signature but routing through new architecture

### 6.3 `main.py` — Entry Point
Implement either:
- **Option A:** CLI using `typer` or `click` with commands: `extract-tokens`, `post`, `post-batch`, `check-health`
- **Option B:** FastAPI service with endpoints: `POST /jobs`, `GET /health/{account_id}`, `POST /admin/quarantine/reset`

Include graceful shutdown handling (close Redis connections, stop Playwright gracefully).

---

## PHASE 7: TESTING (Implement Seventh)

Write comprehensive tests in `tests/`:
- `test_identity.py`: Test registry CRUD, proxy uniqueness enforcement, frozen dataclass behavior
- `test_tokens.py`: Test TTL expiry, rotation detection, usage counting, L1/L2 cache consistency
- `test_safety.py`: Test rate limiting (6/hour, 20/day), quarantine escalation matrix, proxy health checks
- `test_rupload.py`: Mock aiohttp responses for rupload init/transfer, test image mutation changes hash, test validation rejects small/large images
- `test_graphql.py`: Mock GraphQL responses, test idempotency caching, test error classification, test payload structure exactness

Use `pytest-asyncio` for all async tests. Use `fakeredis` or mocked Redis for unit tests. Use `aioresponses` or `respx` for HTTP mocking.

---

## CRITICAL IMPLEMENTATION RULES

1. **NEVER disable safety checks.** All rate limits, quarantine logic, and ethical guardrails are mandatory.
2. **NEVER automate checkpoint solving.** On checkpoint detection, immediately quarantine and escalate to human.
3. **NEVER share proxies between accounts.** Each account gets a dedicated sticky proxy.
4. **NEVER reuse tokens beyond 20 requests or 3 minutes.** TokenVault must enforce rotation.
5. **NEVER post faster than 60 seconds interval** (absolute minimum) or 120 seconds (default minimum).
6. **NEVER upload identical media hashes.** All images must be mutated before upload.
7. **NEVER use datacenter proxies.** The system assumes residential proxy pool input.
8. **NEVER log raw cookies or tokens.** Hash or mask sensitive values in logs.
9. **ALWAYS use HTTP-first for text/image.** Browser is fallback only for video or token extraction.
10. **ALWAYS validate content before posting.** EthicalGuardrails.validate_content() must pass.

---

## DELIVERABLE CHECKLIST

- [ ] All files in the specified tree exist and are importable
- [ ] `config.py` contains hardcoded `EthicalGuardrails` that cannot be overridden by env vars
- [ ] `IdentityContext` is frozen and validates viewport <= screen_resolution
- [ ] `TokenVault` has working L1/L2 cache with 240s TTL and usage-based rotation
- [ ] `SafetyGuard.pre_flight_check()` blocks posts when rate limits exceeded
- [ ] `QuarantineManager.escalate()` moves through SOFT → HARD → SEVERE → BANNED
- [ ] `HardenedRupload.upload_image()` mutates image fingerprint and uses rupload.facebook.com
- [ ] `HardenedGraphQLPoster.post_to_page()` builds exact Facebook GraphQL payload structure
- [ ] `BrowserTokenExtractor` uses Playwright with stealth patches and extracts all 5 tokens
- [ ] `HTTPWorker.process_job()` runs full pipeline: anomaly check → upload → post → audit log
- [ ] `BatchOrchestrator` enforces per-account intervals and never mixes accounts in sessions
- [ ] All tests pass (pytest with asyncio support)
- [ ] `README.md` included with setup instructions, env var template, and safety warnings

---

## FINAL INSTRUCTION

Implement the entire system in the order specified (Phase 1 → Phase 7). Write clean, typed, documented Python code. Every public method must have a docstring. Every complex logic block must have inline comments. The code must be production-ready: handle exceptions, close resources properly, use context managers, and never leak sensitive data in logs.

**The user assumes full responsibility for deployment, compliance, and ethical use. Your responsibility is to implement the architecture exactly as specified, with maximum safety and robustness.**
