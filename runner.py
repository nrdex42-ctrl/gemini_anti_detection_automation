"""Worker loop utilities for queue-backed deployments."""

from __future__ import annotations

import asyncio
from typing import Any, List, Optional

from .config import AppConfig
from .models import PostResult
from .orchestrator import QueueOrchestrator


class WorkerLoop:
    def __init__(
        self,
        redis_client: Any,
        config: Optional[AppConfig] = None,
        queue_name: str = 'fb_automation:jobs',
    ):
        self.redis = redis_client
        self.config = config or AppConfig()
        self.orchestrator = QueueOrchestrator(redis_client, queue_name=queue_name)

    async def run_once(self, limit: Optional[int] = None) -> List[PostResult]:
        batch_limit = limit if limit is not None else self.config.worker_concurrency
        return await self.orchestrator.process_available(
            limit=max(1, int(batch_limit)),
            config=self.config,
        )

    async def run_forever(
        self,
        stop_event: Optional[asyncio.Event] = None,
        max_iterations: Optional[int] = None,
    ) -> List[PostResult]:
        """Poll the queue until stopped. `max_iterations` keeps tests bounded."""
        results: List[PostResult] = []
        iterations = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            if max_iterations is not None and iterations >= max_iterations:
                break

            batch = await self.run_once()
            results.extend(batch)
            iterations += 1
            if not batch:
                await asyncio.sleep(max(0.05, float(self.config.worker_poll_interval_seconds)))
        return results
