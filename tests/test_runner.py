import asyncio

from fb_automation.config import AppConfig
from fb_automation.models import IdentityContext, PostJob
from fb_automation.runner import WorkerLoop

from .fakes import FakeRedis


def test_worker_loop_run_once_processes_batch():
    async def run():
        redis = FakeRedis()
        loop = WorkerLoop(
            redis,
            AppConfig(enable_private_facebook_http=False, require_proxy=False, worker_concurrency=2),
        )
        await loop.orchestrator.identity_registry.register(IdentityContext(account_id='acct-1', proxy_url='http://proxy-1'))
        await loop.orchestrator.enqueue(PostJob(account_id='acct-1', page_id='1', caption='valid caption one'))
        await loop.orchestrator.enqueue(PostJob(account_id='acct-1', page_id='2', caption='valid caption two'))
        results = await loop.run_once()
        assert len(results) == 2
        assert [result.status for result in results] == ['PRIVATE_HTTP_DISABLED', 'PRIVATE_HTTP_DISABLED']

    asyncio.run(run())


def test_worker_loop_run_forever_can_be_bounded():
    async def run():
        redis = FakeRedis()
        loop = WorkerLoop(
            redis,
            AppConfig(
                enable_private_facebook_http=False,
                require_proxy=False,
                worker_concurrency=1,
                worker_poll_interval_seconds=0.05,
            ),
        )
        results = await loop.run_forever(max_iterations=1)
        assert results == []

    asyncio.run(run())
