"""Stateless job processor with result caching, deduplication, circuit breaker,
rate limiting, and optional curl_cffi / FBClient transport.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from enum import Enum
from typing import Any, Dict, Optional

from .anomaly import AnomalyDetector
from .audit import AuditLogger
from .config import AppConfig, EthicalGuardrails
from .graphql_poster import HardenedGraphQLPoster
from .models import IdentityContext, PostJob, PostResult
from .network import ProxyManager
from .rupload import HardenedRupload
from .safety import SafetyGuard, SafetyStatus
from .smart_poster import looks_like_upload_block, mark_upload_blocked
from .tokens import TokenVault
from .utils import classify_error, extract_page_id, sanitize_caption, stable_hash, maybe_await
from .token_rotation import TokenRotationPolicy, TokenFreshnessValidator
from .fb_client import FBClient, FBClientPool
from .circuit_breaker import CircuitBreaker, CircuitBreakerOpen
from .rate_limiter import RateLimiter
from .behavior_simulator import TelemetryFlusher, BehaviorSimulator
from .session_heartbeat import SessionHeartbeatManager

logger = logging.getLogger(__name__)


class AccountState(Enum):
    """Per-account state machine from the reference (Chapter 21).

    State             Allowed Actions           Budget Multiplier
    ────────────────  ────────────────────────  ────────────────
    COLD              Login, browse only         0.1x
    WARMING           Per WarmupPlanner          Per warmup schedule
    ACTIVE            All, per rate budget       1.0x
    DEGRADED          All, at reduced rate       0.5x
    COOLED            Browse + telemetry only    0.0x (no posts)
    QUARANTINED       None                       0.0x
    RETIRED           None                       0.0x
    """

    COLD = "cold"
    WARMING = "warming"
    ACTIVE = "active"
    DEGRADED = "degraded"
    COOLED = "cooled"
    QUARANTINED = "quarantined"
    RETIRED = "retired"


STATE_BUDGET_MULTIPLIER: Dict[AccountState, float] = {
    AccountState.COLD: 0.1,
    AccountState.WARMING: 0.3,
    AccountState.ACTIVE: 1.0,
    AccountState.DEGRADED: 0.5,
    AccountState.COOLED: 0.0,
    AccountState.QUARANTINED: 0.0,
    AccountState.RETIRED: 0.0,
}


class ResultCache:
    """Cache for deduplicating job results across worker retries."""

    def __init__(self, redis_client: Any):
        self.redis = redis_client
        self.result_ttl = 3600

    async def get(self, job_id: str) -> Optional[PostResult]:
        if not self.redis:
            return None
        try:
            cached_json = await maybe_await(self.redis.get(f'job_result:{job_id}'))
            if cached_json:
                data = json.loads(cached_json.decode() if isinstance(cached_json, bytes) else cached_json)
                logger.info(f"Cache hit for job {job_id}: returning cached result")
                return PostResult.from_dict(data)
        except Exception as e:
            logger.warning(f"Error retrieving cached result for {job_id}: {e}")
        return None

    async def set(self, job_id: str, result: PostResult) -> None:
        if not self.redis:
            return
        try:
            result_json = json.dumps(result.to_dict(), separators=(',', ':'))
            await maybe_await(self.redis.setex(f'job_result:{job_id}', self.result_ttl, result_json))
            logger.debug(f"Cached result for job {job_id}")
        except Exception as e:
            logger.warning(f"Error caching result for {job_id}: {e}")


class DeadLetterQueue:
    """Queue for capturing permanently failed jobs."""

    def __init__(self, redis_client: Any):
        self.redis = redis_client
        self.dlq_key = 'dlq:failed_jobs'
        self.dlq_metadata_prefix = 'dlq:metadata:'

    async def enqueue(self, job: PostJob, result: PostResult, reason: str = None) -> None:
        if not self.redis:
            return
        try:
            job_id = f"{job.account_id}:{job.page_id}:{int(time.time()*1000)}"
            failure_record = {
                'job_id': job_id,
                'job': job.to_dict(),
                'result': result.to_dict(),
                'reason': reason or result.status,
                'timestamp': time.time(),
                'retry_count': 0,
            }
            await maybe_await(self.redis.xadd(
                self.dlq_key, failure_record, maxlen=10000, approximate=True
            ))
            logger.warning(f"Enqueued failed job to DLQ: {job_id} ({reason or result.status})")
        except Exception as e:
            logger.error(f"Error enqueuing job to DLQ: {e}")

    async def get_failures(self, account_id: str = None, limit: int = 100) -> list:
        if not self.redis:
            return []
        try:
            entries = await maybe_await(self.redis.xrange(self.dlq_key, count=limit))
            if not entries:
                return []
            failures = []
            for entry_id, data in entries:
                try:
                    if account_id and data.get('job', {}).get('account_id') != account_id:
                        continue
                    failures.append(data)
                except Exception:
                    continue
            return failures
        except Exception as e:
            logger.error(f"Error retrieving DLQ entries: {e}")
            return []


PERMANENT_FAILURES = {
    'CONTENT_REJECTED',
    'ACCOUNT_MISMATCH',
    'TOKEN_EXPIRED',
    'DETECTED',
    'ACCOUNT_BANNED',
    'RATE_LIMITED',
}


class HTTPWorker:
    def __init__(
        self,
        redis_client: Any,
        identity: IdentityContext,
        config: Optional[AppConfig] = None,
        fb_client_pool: Optional[FBClientPool] = None,
    ):
        self.redis = redis_client
        self.identity = identity
        self.config = config or AppConfig()
        self.account_id = identity.account_id
        self.token_vault = TokenVault(redis_client)
        self.proxy_manager = ProxyManager(
            self.config.proxy_pool,
            redis_client,
            require_proxy=self.config.require_proxy,
        )
        self.audit = AuditLogger(redis_client)
        self.anomaly = AnomalyDetector(redis_client)
        self.result_cache = ResultCache(redis_client)
        self.dlq = DeadLetterQueue(redis_client)
        self.token_rotation = TokenRotationPolicy(redis_client) if redis_client else None
        self.freshness_validator = TokenFreshnessValidator(self.token_rotation) if self.token_rotation else None

        # ── Per-account state machine ──
        self.account_state: AccountState = AccountState.ACTIVE
        self.state_changed_at: float = time.time()
        self.consecutive_failures: int = 0

        # ── New components ──
        self.fb_client_pool = fb_client_pool or FBClientPool()
        self.fb_client: Optional[FBClient] = None
        self.circuit_breaker = CircuitBreaker(account_id=self.account_id)
        self.rate_limiter = RateLimiter(redis_client)
        self.telemetry_flusher: Optional[TelemetryFlusher] = None
        self._heartbeat_mgr: Optional[SessionHeartbeatManager] = None

    async def ensure_fb_client(self):
        """Lazy-init the FBClient for this account."""
        if self.fb_client is None:
            fp = {
                "impersonate": "chrome120",
                "user_agent": self.identity.user_agent or "",
                "sec_ch_ua": (
                    f'"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"'
                ),
                "sec_ch_ua_platform": f'"{self.identity.platform or "Windows"}"',
                "locale": self.identity.locale or "en-US",
                "platform": self.identity.platform or "Windows",
            }
            self.fb_client = await self.fb_client_pool.get_or_create(
                account_id=self.account_id,
                fingerprint_profile=fp,
                proxy_url=self.identity.proxy_url or "",
            )
        return self.fb_client

    def get_budget_multiplier(self) -> float:
        """Return the budget multiplier for the current account state."""
        return STATE_BUDGET_MULTIPLIER.get(self.account_state, 0.0)

    def transition_state(self, new_state: AccountState):
        """Transition the account to *new_state* and log the change."""
        old = self.account_state
        if old == new_state:
            return
        self.account_state = new_state
        self.state_changed_at = time.time()
        logger.info(
            "Account %s state: %s -> %s",
            self.account_id, old.value, new_state.value,
        )

    async def process_job(self, job: PostJob, lifecycle: Optional[Any] = None) -> PostResult:
        started = time.time()
        job_id = stable_hash(job.account_id, job.page_id, started, length=32)

        cached = await self.result_cache.get(job_id)
        if cached:
            logger.info(f"Deduplication hit: returning cached result for {job_id}")
            return cached

        if lifecycle is not None and hasattr(lifecycle, 'register_in_flight'):
            lifecycle.register_in_flight(job_id)

        caption = sanitize_caption(job.caption)
        post_id: Optional[str] = None
        status = 'UNHANDLED'
        success = False
        error = ''
        attempted_post = False
        lock_owner = stable_hash(self.identity.account_id, job.page_id, started, length=32)
        lock_acquired = False
        safety = SafetyGuard(
            self.redis, self.identity, self.token_vault,
            circuit_breaker=self.circuit_breaker,
        )

        try:
            if job.account_id != self.identity.account_id:
                status = 'ACCOUNT_MISMATCH'
                raise RuntimeError('job account_id does not match worker identity')

            await safety.record_post_attempt()

            # Check token freshness before mutation
            if self.config.enable_private_facebook_http and self.freshness_validator:
                is_fresh, freshness_status = await self.freshness_validator.validate_before_graphql_mutation(
                    job.account_id, mutation_type='post'
                )
                if not is_fresh:
                    status = 'TOKEN_ROTATION_DUE'
                    raise RuntimeError(f'Token not fresh: {freshness_status}')

            # Account state check — gate by state machine
            if self.account_state in (AccountState.COOLED, AccountState.QUARANTINED, AccountState.RETIRED):
                status = f"STATE_BLOCKED:{self.account_state.value}"
                raise RuntimeError(
                    f"Account {self.account_id} is {self.account_state.value} — "
                    "no actions permitted"
                )
            budget_mult = self.get_budget_multiplier()
            if budget_mult <= 0:
                status = f"STATE_BLOCKED:{self.account_state.value}"
                raise RuntimeError(f"Account budget multiplier is {budget_mult}")

            # Rate limiter check (cost scaled by budget multiplier for DEGRADED)
            action = f"post:{job.post_type or 'text'}"
            rate_cost = max(1, int(round(1.0 / budget_mult))) if budget_mult < 1.0 else 1
            rate_allowed, rate_info = await self.rate_limiter.consume(self.account_id, action, cost=rate_cost)
            if not rate_allowed:
                wait_s = self.rate_limiter.jittered_wait(float(rate_info))
                logger.info(
                    "Rate limited for account %s (action=%s): waiting %.1fs",
                    self.account_id, action, wait_s,
                )
                await asyncio.sleep(min(wait_s, 300))

            content_ok, content_reason = EthicalGuardrails.validate_content(
                caption_hash=stable_hash(caption, length=64),
                caption=caption,
                media_path=job.media_url if job.post_type in ('image', 'video') else None,
            )
            if not content_ok:
                status = 'CONTENT_REJECTED'
                raise RuntimeError(content_reason)

            await self.anomaly.record_runtime_profile(job.account_id, {
                'user_agent': self.identity.user_agent,
                'platform': self.identity.platform,
                'locale': self.identity.locale,
                'timezone': self.identity.timezone,
                'proxy_url': self.identity.proxy_url,
                'viewport': self.identity.viewport,
                'screen_resolution': self.identity.screen_resolution,
                'color_depth': self.identity.color_depth,
                'chrome_version': self.identity.chrome_version,
                'webgl_vendor': self.identity.webgl_vendor,
                'webgl_renderer': self.identity.webgl_renderer,
                'audio_sample_rate': self.identity.audio_sample_rate,
                'transport_mode': 'private_http' if self.config.enable_private_facebook_http else 'browser_fallback',
                'browser_fallback_enabled': self.config.enable_browser_fallback,
                'private_http_enabled': self.config.enable_private_facebook_http,
            })

            report = await self.anomaly.evaluate(job.account_id)
            if report.findings:
                await self.anomaly.engine.record_observation(job.account_id, 'EVALUATE', report.to_dict())
            if report.blocked or report.risk_score >= 5:
                status = 'DETECTED'
                await self.anomaly.engine.publish_report(report)
                await safety._trigger_quarantine(safety.config.quarantine_hard_seconds, report.summary)
                raise RuntimeError(report.summary)

            preflight_status, preflight_reason = await safety.pre_flight_check()
            if preflight_status != SafetyStatus.CLEAR and self.config.enable_private_facebook_http:
                status = preflight_status.value
                raise RuntimeError(preflight_reason)

            if self.config.enable_private_facebook_http:
                lock_acquired = await safety.acquire_account_lock(lock_owner)
                if not lock_acquired:
                    status = 'ACCOUNT_BUSY'
                    raise RuntimeError('another worker is already posting for this account')
                reserve_status, reserve_reason = await safety.reserve_rate_slot()
                if reserve_status != SafetyStatus.CLEAR:
                    status = reserve_status.value
                    raise RuntimeError(reserve_reason)
                attempted_post = True

            # Ensure FBClient is initialized for this account
            fb_client = await self.ensure_fb_client()

            # Send pre-action telemetry burst
            if self._heartbeat_mgr is not None:
                try:
                    await self._heartbeat_mgr.flush_telemetry_pre_action()
                except Exception as tel_exc:
                    logger.debug("Pre-action telemetry failed: %s", tel_exc)

            media_fbid: Optional[str] = None
            if job.post_type == 'image':
                uploader = HardenedRupload(
                    self.token_vault,
                    self.identity,
                    self.redis,
                    self.proxy_manager,
                    self.config,
                    fb_client=fb_client,
                )
                ok, media_fbid, detail = await uploader.upload_image(str(job.media_url or ''))
                if not ok:
                    if looks_like_upload_block(str(detail)):
                        mark_upload_blocked(self.identity.account_id)
                        status = 'UPLOAD_BLOCKED'
                    raise RuntimeError(detail)
            elif job.post_type == 'video':
                uploader = HardenedRupload(
                    self.token_vault,
                    self.identity,
                    self.redis,
                    self.proxy_manager,
                    self.config,
                    fb_client=fb_client,
                )
                ok, media_fbid, detail = await uploader.upload_video(str(job.media_url or ''))
                if not ok:
                    if looks_like_upload_block(str(detail)):
                        mark_upload_blocked(self.identity.account_id)
                        status = 'UPLOAD_BLOCKED'
                    raise RuntimeError(detail)

            poster = HardenedGraphQLPoster(
                self.token_vault,
                self.identity,
                self.redis,
                self.proxy_manager,
                self.config,
                fb_client=fb_client,
                rate_limiter=self.rate_limiter,
            )
            success, status, post_id = await poster.post_to_page(extract_page_id(job.page_id), caption, media_fbid)
            if not success:
                error = status

        except CircuitBreakerOpen as cbe:
            error = str(cbe)
            status = 'CIRCUIT_BREAKER_OPEN'
            success = False
            self.consecutive_failures += 1
            if self.consecutive_failures >= 3:
                self.transition_state(AccountState.DEGRADED)
        except Exception as exc:
            error = str(exc)
            if status == 'UNHANDLED':
                status = classify_error(error)
            success = False
            self.consecutive_failures += 1
        finally:
            await self.audit.log_action(
                job.account_id,
                f'post_{job.post_type}',
                {'page_id': job.page_id, 'status': status},
                'success' if success else 'failed',
            )
            await self.anomaly.record_outcome(job.account_id, 'SUCCESS' if success else status)
            if job.account_id == self.identity.account_id:
                if success:
                    self.consecutive_failures = 0
                    if self.account_state == AccountState.DEGRADED:
                        self.transition_state(AccountState.ACTIVE)
                    await safety.record_success()
                    if status == 'SUCCESS':
                        await safety.record_post_time()
                    if self.config.enable_private_facebook_http and self.freshness_validator:
                        await self.freshness_validator.track_mutation_usage(job.account_id)
                else:
                    await safety.record_failure(status)
                    if self.consecutive_failures >= 5:
                        self.transition_state(AccountState.DEGRADED)
                if lock_acquired:
                    await safety.release_account_lock(lock_owner)
            if success or attempted_post:
                proxy = await self.proxy_manager.get_proxy_for_account(self.identity.account_id)
                await self.anomaly.record_post(job.account_id, caption, proxy)

        result = PostResult(
            success=success,
            status=status,
            post_id=post_id,
            page_id=job.page_id,
            error_message=error or None,
            execution_time_ms=int((time.time() - started) * 1000),
        )

        if success or status in PERMANENT_FAILURES:
            await self.result_cache.set(job_id, result)

        if not success and status in PERMANENT_FAILURES:
            await self.dlq.enqueue(job, result, reason=f"Permanent failure: {status}")

        if lifecycle is not None and hasattr(lifecycle, 'unregister_in_flight'):
            lifecycle.unregister_in_flight(job_id)
        return result

    async def start_heartbeat(
        self,
        cookies_json: str,
        identity: Any,
        on_session_expired: Optional[Any] = None,
    ):
        """Start the background heartbeat + telemetry manager."""
        self._heartbeat_mgr = SessionHeartbeatManager(
            cookies_json=cookies_json,
            account_id=self.account_id,
            token_vault=self.token_vault,
            identity=identity,
            redis_client=self.redis,
            on_session_expired=on_session_expired,
            fb_client=self.fb_client,
        )
        asyncio.create_task(self._heartbeat_mgr.run_forever())

    async def stop(self):
        """Clean up FBClient and heartbeats."""
        if self.fb_client_pool is not None:
            await self.fb_client_pool.remove(self.account_id)
