"""Human-in-the-loop checkpoint escalation."""

from __future__ import annotations

import base64
import json
from typing import Any

from .config import AppConfig
from .safety import QuarantineManager
from .utils import maybe_await


class CheckpointRecovery:
    def __init__(self, redis_client: Any, config: AppConfig):
        self.redis = redis_client
        self.config = config
        self.quarantine = QuarantineManager(redis_client)

    async def handle_checkpoint(self, account_id: str, checkpoint_url: str, screenshot_bytes: bytes) -> None:
        await self.quarantine.escalate(account_id, f'checkpoint: {checkpoint_url}')
        artifact = {
            'account_id': account_id,
            'checkpoint_url': checkpoint_url,
            'screenshot_b64': base64.b64encode(screenshot_bytes or b'').decode('ascii'),
        }
        if self.redis:
            await maybe_await(self.redis.set(f'checkpoint_artifact:{account_id}', json.dumps(artifact)))
        await self._send_webhook({'type': 'CHECKPOINT', **artifact})

    async def _send_webhook(self, payload: dict) -> None:
        if not self.config.admin_webhook_url:
            return
        try:
            import aiohttp  # type: ignore[reportMissingImports]
        except Exception:
            return
        async with aiohttp.ClientSession() as session:
            async with session.post(self.config.admin_webhook_url, json=payload, timeout=10):
                return
