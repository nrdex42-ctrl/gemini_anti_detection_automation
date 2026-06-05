"""Stateless job processor."""

from __future__ import annotations

import time
from typing import Any, Optional

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
from .utils import classify_error, extract_page_id, sanitize_caption, stable_hash


class HTTPWorker:
    def __init__(
        self,
        redis_client: Any,
        identity: IdentityContext,
        config: Optional[AppConfig] = None,
    ):
        self.redis = redis_client
        self.identity = identity
        self.config = config or AppConfig()
        self.token_vault = TokenVault(redis_client)
        self.proxy_manager = ProxyManager(
            self.config.proxy_pool,
            redis_client,
            require_proxy=self.config.enable_private_facebook_http,
        )
        self.audit = AuditLogger(redis_client)
        self.anomaly = AnomalyDetector(redis_client)

    async def process_job(self, job: PostJob, lifecycle: Optional[Any] = None) -> PostResult:
        started = time.time()
        job_id = stable_hash(job.account_id, job.page_id, started, length=32)
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
        safety = SafetyGuard(self.redis, self.identity, self.token_vault)
        try:
            if job.account_id != self.identity.account_id:
                status = 'ACCOUNT_MISMATCH'
                raise RuntimeError('job account_id does not match worker identity')

            await safety.record_post_attempt()

            content_ok, content_reason = EthicalGuardrails.validate_content(
                caption_hash=stable_hash(caption, length=64),
                caption=caption,
                media_path=job.media_url if job.post_type == 'image' else None,
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

            media_fbid: Optional[str] = None
            if job.post_type == 'image':
                uploader = HardenedRupload(
                    self.token_vault,
                    self.identity,
                    self.redis,
                    self.proxy_manager,
                    self.config,
                )
                ok, media_fbid, detail = await uploader.upload_image(str(job.media_url or ''))
                if not ok:
                    if looks_like_upload_block(str(detail)):
                        mark_upload_blocked(self.identity.account_id)
                        status = 'UPLOAD_BLOCKED'
                    raise RuntimeError(detail)
            elif job.post_type == 'video':
                fallback_ok, fallback_reason = await safety.check_fallback_ratio()
                if not fallback_ok:
                    status = 'FALLBACK_RATIO_EXCEEDED'
                    await safety._trigger_quarantine(safety.config.quarantine_soft_seconds, fallback_reason)
                    raise RuntimeError(fallback_reason)
                await safety.record_fallback()
                raise RuntimeError('video jobs must be routed to browser fallback')

            poster = HardenedGraphQLPoster(
                self.token_vault,
                self.identity,
                self.redis,
                self.proxy_manager,
                self.config,
            )
            success, status, post_id = await poster.post_to_page(extract_page_id(job.page_id), caption, media_fbid)
            if not success:
                error = status
        except Exception as exc:
            error = str(exc)
            if status == 'UNHANDLED':
                status = classify_error(error)
            success = False
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
                    await safety.record_success()
                    if status == 'SUCCESS':
                        await safety.record_post_time()
                else:
                    await safety.record_failure(status)
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
        if lifecycle is not None and hasattr(lifecycle, 'unregister_in_flight'):
            lifecycle.unregister_in_flight(job_id)
        return result
