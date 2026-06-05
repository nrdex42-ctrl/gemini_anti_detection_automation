"""Queue manager and batch dispatcher."""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

from .anomaly import AnomalyDetector
from .audit import AuditLogger
from .config import AppConfig, SafetyConfig
from .identity import IdentityRegistry
from .models import IdentityContext, PostJob, PostResult
from .utils import maybe_await
from .worker import HTTPWorker


WorkerFactory = Callable[[Any, IdentityContext, Optional[AppConfig]], Any]
SleepFunc = Callable[[float], Awaitable[None]]


class QueueOrchestrator:
    def __init__(
        self,
        redis_client: Any,
        queue_name: str = 'fb_automation:jobs',
        worker_factory: Optional[WorkerFactory] = None,
        sleep_func: Optional[SleepFunc] = None,
    ):
        self.redis = redis_client
        self.queue_name = queue_name
        self.identity_registry = IdentityRegistry(redis_client)
        self.safety = SafetyConfig()
        self.worker_factory = worker_factory or HTTPWorker
        self.sleep = sleep_func or asyncio.sleep

    async def enqueue(self, job: PostJob) -> None:
        await self.redis.rpush(self.queue_name, json.dumps(job.to_dict(), ensure_ascii=False))

    async def dequeue(self) -> Optional[PostJob]:
        raw = await maybe_await(self.redis.lpop(self.queue_name))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8', errors='ignore')
        payload = json.loads(str(raw))
        return PostJob.from_dict(payload)

    async def dequeue_many(self, limit: int) -> List[PostJob]:
        jobs: List[PostJob] = []
        for _ in range(max(0, limit)):
            job = await self.dequeue()
            if job is None:
                break
            jobs.append(job)
        return jobs

    async def process_next(self, config: Optional[AppConfig] = None) -> Optional[PostResult]:
        job = await self.dequeue()
        if job is None:
            return None

        return (await self.dispatch_jobs([job], config=config))[0]

    async def process_available(
        self,
        limit: int = 1,
        config: Optional[AppConfig] = None,
    ) -> List[PostResult]:
        jobs = await self.dequeue_many(limit)
        if not jobs:
            return []
        return await self.dispatch_jobs(jobs, config=config)

    async def dispatch_jobs(
        self,
        jobs: Iterable[PostJob],
        config: Optional[AppConfig] = None,
        concurrency: Optional[int] = None,
    ) -> List[PostResult]:
        """Run account groups concurrently while serializing jobs per account."""
        job_list = list(jobs)
        if not job_list:
            return []

        runtime_config = config or AppConfig()
        account_limit = min(
            self.safety.max_concurrent_accounts_global,
            max(1, int(concurrency or runtime_config.worker_concurrency)),
        )
        semaphore = asyncio.Semaphore(account_limit)
        grouped: 'OrderedDict[str, List[Tuple[int, PostJob]]]' = OrderedDict()
        for index, job in enumerate(job_list):
            grouped.setdefault(job.account_id, []).append((index, job))

        results: List[Optional[PostResult]] = [None] * len(job_list)

        async def run_account_group(account_id: str, indexed_jobs: List[Tuple[int, PostJob]]) -> None:
            async with semaphore:
                identity = await self.identity_registry.get(account_id)
                if identity is None:
                    for index, job in indexed_jobs:
                        results[index] = await self._missing_identity_result(job)
                    return

                worker = self.worker_factory(self.redis, identity, runtime_config)
                for index, job in indexed_jobs:
                    results[index] = await worker.process_job(job)

        await asyncio.gather(*(run_account_group(account_id, account_jobs) for account_id, account_jobs in grouped.items()))
        return [
            result if result is not None else PostResult(False, 'INTERNAL_RESULT_MISSING', job.page_id)
            for result, job in zip(results, job_list)
        ]

    async def _missing_identity_result(self, job: PostJob) -> PostResult:
        result = PostResult(
            success=False,
            status='IDENTITY_NOT_FOUND',
            page_id=job.page_id,
            error_message=f'no identity registered for account {job.account_id}',
        )
        await AuditLogger(self.redis).log_action(
            job.account_id,
            f'post_{job.post_type}',
            {'page_id': job.page_id, 'status': result.status},
            'failed',
        )
        await AnomalyDetector(self.redis).record_outcome(job.account_id, result.status)
        return result

    async def dispatch_batch(
        self,
        identity: IdentityContext,
        jobs: Iterable[PostJob],
        concurrency: int = 2,
    ) -> List[PostResult]:
        del concurrency
        worker = self.worker_factory(self.redis, identity, None)
        results: List[PostResult] = []
        for job in jobs:
            results.append(await worker.process_job(job))
        return results
