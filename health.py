"""Account health snapshots for worker monitoring."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List

from .anomaly import AnomalyDetector
from .safety import QuarantineLevel, QuarantineManager
from .utils import maybe_await, redis_lrange


@dataclass(frozen=True)
class AccountHealthSnapshot:
    account_id: str
    status: str
    quarantine_level: str
    risk_score: int = 0
    success_streak: int = 0
    failure_count_10: int = 0
    recent_outcomes: List[str] = field(default_factory=list)
    anomaly_flags: List[str] = field(default_factory=list)
    detection_findings: List[Dict[str, Any]] = field(default_factory=list)
    detection_summary: str = ''
    last_success: str = ''
    last_failure: str = ''
    last_failure_reason: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class HealthMonitor:
    def __init__(self, redis_client: Any):
        self.redis = redis_client
        self.anomaly = AnomalyDetector(redis_client)
        self.quarantine = QuarantineManager(redis_client)

    async def account_snapshot(self, account_id: str) -> AccountHealthSnapshot:
        health = await self._hgetall(f'account_health:{account_id}')
        outcomes = [str(value) for value in await redis_lrange(self.redis, f'outcomes:{account_id}', 0, 9)]
        failures = [value for value in outcomes if value.upper() != 'SUCCESS']
        anomalies = await self.anomaly.check_anomalies(account_id)
        report = await self.anomaly.evaluate(account_id)
        quarantine_level = await self.quarantine.get_level(account_id)
        status = self._status_for(quarantine_level, failures, anomalies, report.blocked)
        return AccountHealthSnapshot(
            account_id=account_id,
            status=status,
            quarantine_level=quarantine_level.value,
            risk_score=report.risk_score,
            success_streak=int(health.get('success_streak') or 0),
            failure_count_10=len(failures),
            recent_outcomes=outcomes,
            anomaly_flags=anomalies,
            detection_findings=[finding.to_dict() for finding in report.findings],
            detection_summary=report.summary,
            last_success=str(health.get('last_success') or ''),
            last_failure=str(health.get('last_failure') or ''),
            last_failure_reason=str(health.get('last_failure_reason') or ''),
        )

    async def registered_account_ids(self) -> List[str]:
        if self.redis is None:
            return []
        keys = await maybe_await(self.redis.keys('identity_ctx:*'))
        account_ids: List[str] = []
        for key in keys or []:
            text = key.decode('utf-8', errors='ignore') if isinstance(key, bytes) else str(key)
            account_ids.append(text.split(':', 1)[1])
        return sorted(set(account_ids))

    async def global_snapshot(self) -> Dict[str, Any]:
        account_ids = await self.registered_account_ids()
        accounts = [await self.account_snapshot(account_id) for account_id in account_ids]
        by_status: Dict[str, int] = {}
        for snapshot in accounts:
            by_status[snapshot.status] = by_status.get(snapshot.status, 0) + 1
        return {
            'account_count': len(accounts),
            'by_status': by_status,
            'accounts': [snapshot.to_dict() for snapshot in accounts],
        }

    async def publish_snapshot(self, channel: str = 'admin_alerts') -> Dict[str, Any]:
        snapshot = await self.global_snapshot()
        if self.redis is not None:
            await maybe_await(self.redis.publish(channel, json.dumps({'type': 'HEALTH_SNAPSHOT', **snapshot})))
        return snapshot

    async def _hgetall(self, key: str) -> Dict[str, Any]:
        if self.redis is None:
            return {}
        if hasattr(self.redis, 'hgetall'):
            raw = await maybe_await(self.redis.hgetall(key))
            return {
                self._decode(field): self._decode(value)
                for field, value in dict(raw or {}).items()
            }
        fields = ('success_streak', 'last_success', 'last_failure', 'last_failure_reason')
        output: Dict[str, Any] = {}
        for field in fields:
            value = await maybe_await(self.redis.hget(key, field))
            if value is not None:
                output[field] = self._decode(value)
        return output

    @staticmethod
    def _decode(value: Any) -> str:
        return value.decode('utf-8', errors='ignore') if isinstance(value, bytes) else str(value)

    @staticmethod
    def _status_for(level: QuarantineLevel, failures: List[str], anomalies: List[str], blocked: bool = False) -> str:
        if level != QuarantineLevel.NONE or blocked:
            return 'QUARANTINED'
        if anomalies:
            return 'WATCH'
        if len(failures) >= 3:
            return 'DEGRADED'
        return 'CLEAR'
