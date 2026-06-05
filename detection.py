"""Structured detection and risk scoring for account-level automation behavior."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from .identity import IdentityRegistry
from .safety import QuarantineManager
from .tokens import TokenVault
from .utils import maybe_await, redis_lrange, stable_hash


class DetectionSeverity(str, Enum):
    INFO = 'INFO'
    LOW = 'LOW'
    MEDIUM = 'MEDIUM'
    HIGH = 'HIGH'
    CRITICAL = 'CRITICAL'


@dataclass(frozen=True)
class DetectionFinding:
    rule_id: str
    severity: DetectionSeverity
    score: int
    title: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    recommendation: str = ''

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data['severity'] = self.severity.value
        return data


@dataclass(frozen=True)
class DetectionReport:
    account_id: str
    risk_score: int
    blocked: bool
    findings: List[DetectionFinding] = field(default_factory=list)
    checked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    summary: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'account_id': self.account_id,
            'risk_score': self.risk_score,
            'blocked': self.blocked,
            'findings': [finding.to_dict() for finding in self.findings],
            'checked_at': self.checked_at,
            'summary': self.summary,
        }


class DetectionEngine:
    """Aggregates multiple low-level signals into a single risk report."""

    def __init__(self, redis_client: Any, token_vault: Optional[TokenVault] = None):
        self.redis = redis_client
        self.token_vault = token_vault or TokenVault(redis_client)
        self.identity_registry = IdentityRegistry(redis_client)
        self.quarantine = QuarantineManager(redis_client)

    async def evaluate_account(self, account_id: str) -> DetectionReport:
        findings: List[DetectionFinding] = []

        identity = await self.identity_registry.get(account_id)
        if identity is not None:
            findings.extend(await self._identity_findings(identity))

        findings.extend(await self._behavior_findings(account_id))
        findings.extend(await self._state_findings(account_id))
        findings.extend(await self._runtime_profile_findings(account_id))

        risk_score = sum(finding.score for finding in findings)
        blocked = any(finding.severity == DetectionSeverity.CRITICAL for finding in findings) or risk_score >= 7
        summary = self._summarize(findings, risk_score, blocked)
        return DetectionReport(
            account_id=account_id,
            risk_score=risk_score,
            blocked=blocked,
            findings=findings,
            summary=summary,
        )

    async def publish_report(self, report: DetectionReport, channel: str = 'admin_alerts') -> None:
        if self.redis is None:
            return
        payload = {'type': 'DETECTION_REPORT', **report.to_dict()}
        await maybe_await(self.redis.xadd('detection:reports', payload, maxlen=10000, approximate=True))
        await maybe_await(self.redis.publish(channel, json.dumps(payload, ensure_ascii=False)))

    async def record_observation(self, account_id: str, event_type: str, evidence: Dict[str, Any]) -> None:
        if self.redis is None:
            return
        payload = {
            'account_id': account_id,
            'event_type': event_type,
            'evidence': json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
            'ts': datetime.now(timezone.utc).isoformat(),
        }
        await maybe_await(self.redis.xadd('detection:events', payload, maxlen=100000, approximate=True))

    async def _identity_findings(self, identity: Any) -> List[DetectionFinding]:
        findings: List[DetectionFinding] = []
        fingerprint = self.identity_registry.identity_fingerprint(identity)
        accounts = await self.identity_registry.find_accounts_by_fingerprint(fingerprint)
        if len(accounts) > 1:
            findings.append(
                DetectionFinding(
                    rule_id='IDENTITY_FINGERPRINT_REUSE',
                    severity=DetectionSeverity.CRITICAL if len(accounts) > 3 else DetectionSeverity.HIGH,
                    score=5 if len(accounts) > 3 else 3,
                    title='same browser fingerprint reused across multiple accounts',
                    evidence={
                        'fingerprint': fingerprint,
                        'account_count': len(accounts),
                        'accounts': accounts[:10],
                    },
                    recommendation='deduplicate browser profiles and isolate identity contexts per account',
                )
            )

        proxy_accounts = await self.identity_registry.find_accounts_by_proxy(identity.proxy_url)
        if len(proxy_accounts) > 1:
            findings.append(
                DetectionFinding(
                    rule_id='PROXY_REUSE',
                    severity=DetectionSeverity.HIGH,
                    score=3,
                    title='same proxy is assigned to multiple accounts',
                    evidence={
                        'proxy_url': identity.proxy_url,
                        'account_count': len(proxy_accounts),
                        'accounts': proxy_accounts[:10],
                    },
                    recommendation='assign a dedicated proxy per account or quarantine the shared proxy',
                )
            )

        if identity.chrome_version not in identity.user_agent:
            findings.append(
                DetectionFinding(
                    rule_id='UA_CHROME_VERSION_MISMATCH',
                    severity=DetectionSeverity.MEDIUM,
                    score=2,
                    title='chrome version is not reflected in the user agent',
                    evidence={'user_agent': identity.user_agent, 'chrome_version': identity.chrome_version},
                    recommendation='keep user-agent and reported chrome version aligned',
                )
            )

        normalized_locale = identity.locale.replace('-', '_')
        if identity.timezone.startswith('America/') and normalized_locale not in {'en_US', 'en_CA', 'es_MX', 'pt_BR'}:
            findings.append(
                DetectionFinding(
                    rule_id='LOCALE_TIMEZONE_MISMATCH',
                    severity=DetectionSeverity.MEDIUM,
                    score=2,
                    title='locale does not fit the timezone region',
                    evidence={'timezone': identity.timezone, 'locale': identity.locale},
                    recommendation='match locale and timezone to the same human locale profile',
                )
            )

        return findings

    async def _runtime_profile_findings(self, account_id: str) -> List[DetectionFinding]:
        findings: List[DetectionFinding] = []
        if self.redis is None or not hasattr(self.redis, 'xrevrange'):
            return findings

        raw_events = await maybe_await(self.redis.xrevrange('detection:events', count=100))
        profiles: List[Dict[str, Any]] = []
        for _entry_id, payload in raw_events or []:
            event = self._decode_event(payload)
            if str(event.get('account_id') or '') != str(account_id):
                continue
            if str(event.get('event_type') or '').upper() != 'RUNTIME_PROFILE':
                continue
            profiles.append(event)

        if not profiles:
            return findings

        latest = profiles[0].get('evidence') or {}
        suspicious = self._runtime_profile_suspicious_fields(latest)
        if suspicious:
            findings.append(
                DetectionFinding(
                    rule_id='RUNTIME_PROFILE_SUSPICIOUS',
                    severity=DetectionSeverity.HIGH if len(suspicious) < 4 else DetectionSeverity.CRITICAL,
                    score=3 if len(suspicious) < 4 else 5,
                    title='runtime profile contains stealth-related markers',
                    evidence={
                        'matched_fields': suspicious,
                        'runtime_profile': latest,
                    },
                    recommendation='treat the account as high risk and route it through quarantine or human review',
                )
            )

        if len(profiles) >= 2:
            baseline = profiles[-1].get('evidence') or {}
            drift_keys = self._runtime_profile_drift_keys(baseline, latest)
            if drift_keys:
                findings.append(
                    DetectionFinding(
                        rule_id='RUNTIME_PROFILE_DRIFT',
                        severity=DetectionSeverity.MEDIUM if len(drift_keys) < 4 else DetectionSeverity.HIGH,
                        score=2 if len(drift_keys) < 4 else 3,
                        title='runtime profile changed across recent observations',
                        evidence={
                            'changed_fields': drift_keys,
                            'baseline_profile': baseline,
                            'latest_profile': latest,
                        },
                        recommendation='keep runtime profile stable and verify the account is not being rotated across environments',
                    )
                )

        return findings

    async def _behavior_findings(self, account_id: str) -> List[DetectionFinding]:
        findings: List[DetectionFinding] = []
        outcomes = [str(value) for value in await redis_lrange(self.redis, f'outcomes:{account_id}', 0, 9)]
        failures = [value for value in outcomes if value.upper() != 'SUCCESS']
        if len(outcomes) >= 10 and len(failures) >= 7:
            findings.append(
                DetectionFinding(
                    rule_id='HIGH_FAILURE_RATE',
                    severity=DetectionSeverity.HIGH,
                    score=3,
                    title='recent outcomes show sustained failure rate',
                    evidence={'outcomes': outcomes, 'failure_count': len(failures)},
                    recommendation='pause the account and inspect failure classes before retrying',
                )
            )

        times = [float(value) for value in await redis_lrange(self.redis, f'post_times:{account_id}', 0, 9)]
        if len(times) >= 2:
            intervals = [abs(times[i] - times[i + 1]) for i in range(len(times) - 1)]
            average_interval = sum(intervals) / max(1, len(intervals))
            if average_interval < 5:
                findings.append(
                    DetectionFinding(
                        rule_id='TEMPORAL_CLUSTERING',
                        severity=DetectionSeverity.HIGH,
                        score=3,
                        title='posts are clustered too tightly in time',
                        evidence={'average_interval_seconds': round(average_interval, 3), 'intervals': intervals[:10]},
                        recommendation='increase jitter and rate-limit the account more aggressively',
                    )
                )

        captions = await redis_lrange(self.redis, f'captions:{account_id}', 0, 19)
        if captions:
            uniqueness_ratio = len(set(captions)) / max(1, len(captions))
            if uniqueness_ratio < 0.5:
                findings.append(
                    DetectionFinding(
                        rule_id='CONTENT_DUPLICATION',
                        severity=DetectionSeverity.MEDIUM,
                        score=2,
                        title='caption history is highly repetitive',
                        evidence={'uniqueness_ratio': round(uniqueness_ratio, 3), 'sample_size': len(captions)},
                        recommendation='rotate content templates and avoid repeated captions',
                    )
                )

        proxies = await redis_lrange(self.redis, f'proxy_used:{account_id}', 0, 9)
        if len(set(proxies)) > 2:
            findings.append(
                DetectionFinding(
                    rule_id='PROXY_HOPPING',
                    severity=DetectionSeverity.MEDIUM,
                    score=2,
                    title='account is moving across many proxies',
                    evidence={'proxy_count': len(set(proxies)), 'proxies': proxies[:10]},
                    recommendation='keep a stable proxy per account session',
                )
            )

        fallback_count = int(await maybe_await(self.redis.get(f'fallback_count:{account_id}:{datetime.now(timezone.utc):%Y%m%d%H}')) or 0)
        total_count = int(await maybe_await(self.redis.get(f'post_count:{account_id}:{datetime.now(timezone.utc):%Y%m%d%H}')) or 0)
        if total_count > 0 and fallback_count / total_count > 0.1:
            findings.append(
                DetectionFinding(
                    rule_id='FALLBACK_PRESSURE',
                    severity=DetectionSeverity.HIGH if fallback_count / total_count > 0.25 else DetectionSeverity.MEDIUM,
                    score=3 if fallback_count / total_count > 0.25 else 2,
                    title='browser fallback usage is over the safe ratio',
                    evidence={
                        'fallback_count': fallback_count,
                        'total_count': total_count,
                        'ratio': round(fallback_count / total_count, 3),
                    },
                    recommendation='reduce browser fallback usage and fix the primary HTTP path',
                )
            )

        return findings

    async def _state_findings(self, account_id: str) -> List[DetectionFinding]:
        findings: List[DetectionFinding] = []

        if self.redis is not None and await maybe_await(self.redis.exists(f'quarantine:{account_id}')):
            level = await self.quarantine.get_level(account_id)
            findings.append(
                DetectionFinding(
                    rule_id='QUARANTINE_PRESENT',
                    severity=DetectionSeverity.CRITICAL,
                    score=5,
                    title='account is already quarantined',
                    evidence={'level': level.value},
                    recommendation='stop posting and resolve the quarantine reason first',
                )
            )

        if self.redis is not None and await maybe_await(self.redis.exists(f'checkpoint_artifact:{account_id}')):
            findings.append(
                DetectionFinding(
                    rule_id='CHECKPOINT_ARTIFACT',
                    severity=DetectionSeverity.CRITICAL,
                    score=5,
                    title='checkpoint evidence exists for this account',
                    evidence={'checkpoint_artifact_key': f'checkpoint_artifact:{account_id}'},
                    recommendation='route to human review and clear the checkpoint before retrying',
                )
            )

        tokens = await self.token_vault.get(account_id)
        if not tokens:
            findings.append(
                DetectionFinding(
                    rule_id='TOKEN_MISSING',
                    severity=DetectionSeverity.HIGH,
                    score=3,
                    title='token cache is empty or expired',
                    evidence={'account_id': account_id},
                    recommendation='refresh session tokens before allowing further posts',
                )
            )
        elif await self.token_vault.is_rotation_needed(account_id):
            findings.append(
                DetectionFinding(
                    rule_id='TOKEN_ROTATION_NEEDED',
                    severity=DetectionSeverity.MEDIUM,
                    score=2,
                    title='token rotation is due soon',
                    evidence={
                        'timestamp': tokens.get('timestamp', 0),
                        'usage_count': tokens.get('usage_count', 0),
                    },
                    recommendation='refresh the token bundle and avoid repeated use of stale tokens',
                )
            )

        return findings

    @staticmethod
    def _runtime_profile_suspicious_fields(profile: Dict[str, Any]) -> List[str]:
        suspicious_keys = (
            'headless',
            'webdriver',
            'navigator_webdriver',
            'canvas_noise',
            'canvas_spoof',
            'webgl_spoof',
            'webgl_override',
            'audio_context_spoof',
            'browser_stealth',
            'tls_client',
            'dns_over_https',
            'doh',
            'activity_simulation',
            'timing_jitter',
            'image_mutation',
            'fingerprint_rotation',
            'proxy_rotation',
        )
        matches: List[str] = []
        for key in suspicious_keys:
            value = profile.get(key)
            if isinstance(value, bool) and value:
                matches.append(key)
            elif isinstance(value, (int, float)) and bool(value):
                matches.append(key)
            elif isinstance(value, str) and value.strip() and value.strip().lower() not in {'0', 'false', 'none', 'off', 'no'}:
                matches.append(key)
        return matches

    @staticmethod
    def _runtime_profile_drift_keys(baseline: Dict[str, Any], latest: Dict[str, Any]) -> List[str]:
        tracked = (
            'user_agent',
            'platform',
            'timezone',
            'locale',
            'proxy_url',
            'viewport',
            'screen_resolution',
            'color_depth',
            'chrome_version',
            'webgl_vendor',
            'webgl_renderer',
            'audio_sample_rate',
            'transport_mode',
            'browser_fallback_enabled',
            'private_http_enabled',
        )
        return [key for key in tracked if baseline.get(key) != latest.get(key)]

    @staticmethod
    def _decode_event(payload: Any) -> Dict[str, Any]:
        if isinstance(payload, dict):
            event = dict(payload)
        else:
            try:
                event = json.loads(str(payload))
            except Exception:
                event = {}
        evidence = event.get('evidence')
        if isinstance(evidence, str):
            try:
                event['evidence'] = json.loads(evidence)
            except Exception:
                event['evidence'] = {'raw': evidence}
        elif evidence is None:
            event['evidence'] = {}
        return event

    @staticmethod
    def _summarize(findings: List[DetectionFinding], risk_score: int, blocked: bool) -> str:
        if not findings:
            return 'no notable risk signals'
        top = ', '.join(finding.rule_id for finding in findings[:4])
        prefix = 'blocked' if blocked else 'watch'
        return f'{prefix}: score={risk_score}; signals={top}'
