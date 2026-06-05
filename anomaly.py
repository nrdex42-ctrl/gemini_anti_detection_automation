"""Compatibility wrapper around the structured detection engine."""

from __future__ import annotations

from typing import Any, Dict, List

from .detection import DetectionEngine, DetectionSeverity
from .utils import maybe_await


class AnomalyDetector:
    def __init__(self, redis_client: Any):
        self.redis = redis_client
        self.engine = DetectionEngine(redis_client)

    async def check_anomalies(self, account_id: str) -> List[str]:
        report = await self.engine.evaluate_account(account_id)
        ignored = {
            'TOKEN_MISSING',
            'TOKEN_ROTATION_NEEDED',
        }
        return [
            finding.rule_id
            for finding in report.findings
            if finding.severity in {DetectionSeverity.MEDIUM, DetectionSeverity.HIGH, DetectionSeverity.CRITICAL}
            and finding.rule_id not in ignored
        ]

    async def evaluate(self, account_id: str):
        return await self.engine.evaluate_account(account_id)

    async def record_outcome(self, account_id: str, outcome: str) -> None:
        if self.redis is None:
            return
        await maybe_await(self.redis.lpush(f'outcomes:{account_id}', outcome))
        await maybe_await(self.redis.ltrim(f'outcomes:{account_id}', 0, 99))

    async def record_post(self, account_id: str, caption: str, proxy: str) -> None:
        if self.redis is None:
            return
        await self.engine.record_observation(
            account_id,
            'POST',
            {'caption': caption, 'proxy': proxy},
        )
        import hashlib
        import time

        caption_hash = hashlib.sha256(str(caption or '').encode('utf-8')).hexdigest()
        pipe = self.redis.pipeline()
        pipe.lpush(f'post_times:{account_id}', str(time.time()))
        pipe.ltrim(f'post_times:{account_id}', 0, 99)
        pipe.lpush(f'captions:{account_id}', caption_hash)
        pipe.ltrim(f'captions:{account_id}', 0, 99)
        pipe.lpush(f'proxy_used:{account_id}', proxy or '')
        pipe.ltrim(f'proxy_used:{account_id}', 0, 99)
        await maybe_await(pipe.execute())

    async def record_runtime_profile(self, account_id: str, profile: Dict[str, Any]) -> None:
        if self.redis is None:
            return
        await self.engine.record_observation(
            account_id,
            'RUNTIME_PROFILE',
            profile,
        )
