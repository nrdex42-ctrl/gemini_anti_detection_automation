"""Audit logging."""

from __future__ import annotations

import json
import logging
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .utils import maybe_await

try:
    import structlog  # type: ignore[reportMissingImports]
except Exception:  # pragma: no cover - optional in local workspace.
    structlog = None


class AuditLogger:
    def __init__(self, redis_client: Any):
        self.redis = redis_client
        self.logger = logging.getLogger(__name__)
        self.struct_logger: Optional[Any] = structlog.get_logger(__name__) if structlog else None

    async def log_action(self, account_id: str, action: str, metadata: Dict[str, Any], outcome: str) -> None:
        payload = {
            'account_id': account_id,
            'action': action,
            'metadata': json.dumps(metadata or {}, ensure_ascii=False),
            'outcome': outcome,
            'ts': datetime.now(timezone.utc).isoformat(),
            'source_ip_hash': hashlib.sha256(
                str((metadata or {}).get('proxy') or 'unknown').encode('utf-8')
            ).hexdigest()[:16],
        }
        if self.redis:
            await maybe_await(self.redis.xadd('audit:actions', payload, maxlen=100000, approximate=True))
        if self.struct_logger is not None:
            self.struct_logger.info('audit_action', **payload)
        else:
            self.logger.info('audit_action %s', payload)

    async def get_account_history(self, account_id: str, limit: int = 100) -> List[dict]:
        if not self.redis:
            return []
        rows = await maybe_await(self.redis.xrevrange('audit:actions', count=limit))
        output: List[dict] = []
        for _entry_id, fields in rows or []:
            decoded = {
                (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
                for k, v in dict(fields).items()
            }
            if decoded.get('account_id') == account_id:
                output.append(decoded)
        return output
