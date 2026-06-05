import asyncio
from typing import Any, Dict, List, Optional

from fb_automation.config import AppConfig
from fb_automation.models import IdentityContext, PostJob, PostResult
from fb_automation.orchestrator import QueueOrchestrator

from .fakes import FakeRedis


class RecordingWorker:
    events: List[tuple] = []
    active: Dict[str, int] = {}
    violations: List[str] = []

    def __init__(self, redis_client: Any, identity: IdentityContext, config: Optional[AppConfig]):
        del redis_client, config
        self.identity = identity

    async def process_job(self, job: PostJob) -> PostResult:
        account_id = self.identity.account_id
        self.active[account_id] = self.active.get(account_id, 0) + 1
        if self.active[account_id] > 1:
            self.violations.append(account_id)
        self.events.append(('start', account_id, job.page_id))
        await asyncio.sleep(0.01)
        self.events.append(('end', account_id, job.page_id))
        self.active[account_id] -= 1
        return PostResult(success=True, status='FAKE_SUCCESS', page_id=job.page_id, post_id=f'post-{job.page_id}')


def reset_recording_worker():
    RecordingWorker.events = []
    RecordingWorker.active = {}
    RecordingWorker.violations = []


def test_process_next_handles_missing_identity():
    async def run():
        redis = FakeRedis()
        orchestrator = QueueOrchestrator(redis)
        await orchestrator.enqueue(PostJob(account_id='acct-1', page_id='123', caption='valid caption'))
        result = await orchestrator.process_next(AppConfig(enable_private_facebook_http=False))
        assert result is not None
        assert not result.success
        assert result.status == 'IDENTITY_NOT_FOUND'
        assert redis.store['outcomes:acct-1'][0] == 'IDENTITY_NOT_FOUND'
        assert redis.streams['audit:actions'][0]['outcome'] == 'failed'

    asyncio.run(run())


def test_process_next_dispatches_registered_identity_fail_closed():
    async def run():
        redis = FakeRedis()
        orchestrator = QueueOrchestrator(redis)
        await orchestrator.identity_registry.register(IdentityContext(account_id='acct-1', proxy_url='http://proxy-1'))
        await orchestrator.enqueue(PostJob(account_id='acct-1', page_id='123', caption='valid caption'))
        result = await orchestrator.process_next(AppConfig(enable_private_facebook_http=False))
        assert result is not None
        assert not result.success
        assert result.status == 'PRIVATE_HTTP_DISABLED'
        assert redis.store['outcomes:acct-1'][0] == 'PRIVATE_HTTP_DISABLED'
        assert redis.store['account_health:acct-1']['last_failure_reason'] == 'PRIVATE_HTTP_DISABLED'

    asyncio.run(run())


def test_process_available_stops_on_empty_queue():
    async def run():
        redis = FakeRedis()
        orchestrator = QueueOrchestrator(redis)
        await orchestrator.identity_registry.register(IdentityContext(account_id='acct-1', proxy_url='http://proxy-1'))
        await orchestrator.enqueue(PostJob(account_id='acct-1', page_id='1', caption='valid caption one'))
        await orchestrator.enqueue(PostJob(account_id='acct-1', page_id='2', caption='valid caption two'))
        results = await orchestrator.process_available(limit=5, config=AppConfig(enable_private_facebook_http=False))
        assert len(results) == 2
        assert [result.page_id for result in results] == ['1', '2']
        assert await redis.lrange(orchestrator.queue_name, 0, -1) == []

    asyncio.run(run())


def test_dispatch_jobs_parallelizes_across_accounts_but_serializes_each_account():
    async def run():
        reset_recording_worker()
        redis = FakeRedis()
        orchestrator = QueueOrchestrator(redis, worker_factory=RecordingWorker)
        await orchestrator.identity_registry.register(IdentityContext(account_id='acct-1', proxy_url='http://proxy-1'))
        await orchestrator.identity_registry.register(IdentityContext(account_id='acct-2', proxy_url='http://proxy-2'))
        jobs = [
            PostJob(account_id='acct-1', page_id='a', caption='valid caption a'),
            PostJob(account_id='acct-1', page_id='b', caption='valid caption b'),
            PostJob(account_id='acct-2', page_id='c', caption='valid caption c'),
            PostJob(account_id='acct-2', page_id='d', caption='valid caption d'),
        ]
        results = await orchestrator.dispatch_jobs(
            jobs,
            config=AppConfig(enable_private_facebook_http=False, worker_concurrency=2),
        )
        assert [result.page_id for result in results] == ['a', 'b', 'c', 'd']
        assert RecordingWorker.violations == []

        first_end_index = next(index for index, event in enumerate(RecordingWorker.events) if event[0] == 'end')
        starts_before_first_end = [event for event in RecordingWorker.events[:first_end_index] if event[0] == 'start']
        assert {event[1] for event in starts_before_first_end} == {'acct-1', 'acct-2'}
        acct_1_events = [event for event in RecordingWorker.events if event[1] == 'acct-1']
        assert acct_1_events == [
            ('start', 'acct-1', 'a'),
            ('end', 'acct-1', 'a'),
            ('start', 'acct-1', 'b'),
            ('end', 'acct-1', 'b'),
        ]

    asyncio.run(run())


def test_dispatch_jobs_serializes_same_account_without_local_interval_sleep():
    async def run():
        reset_recording_worker()
        delays: List[float] = []

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)

        redis = FakeRedis()
        orchestrator = QueueOrchestrator(redis, worker_factory=RecordingWorker, sleep_func=fake_sleep)
        await orchestrator.identity_registry.register(IdentityContext(account_id='acct-1', proxy_url='http://proxy-1'))
        jobs = [
            PostJob(account_id='acct-1', page_id='a', caption='valid caption a'),
            PostJob(account_id='acct-1', page_id='b', caption='valid caption b'),
        ]
        await orchestrator.dispatch_jobs(
            jobs,
            config=AppConfig(enable_private_facebook_http=True, worker_concurrency=2),
        )
        assert delays == []
        assert RecordingWorker.violations == []

    asyncio.run(run())
