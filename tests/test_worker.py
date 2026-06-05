import asyncio

from fb_automation.config import AppConfig
from fb_automation.identity import IdentityRegistry
from fb_automation.models import IdentityContext, PostJob
from fb_automation.worker import HTTPWorker
from fb_automation.tokens import TokenVault

from .fakes import FakeRedis


def test_worker_rejects_content_without_recording_post_activity():
    async def run():
        redis = FakeRedis()
        worker = HTTPWorker(
            redis,
            IdentityContext(account_id='acct-1', proxy_url='http://proxy-1'),
            AppConfig(enable_private_facebook_http=False),
        )
        result = await worker.process_job(PostJob(account_id='acct-1', page_id='123', caption='hi'))
        assert not result.success
        assert result.status == 'CONTENT_REJECTED'
        assert 'post_times:acct-1' not in redis.store
        assert redis.store['outcomes:acct-1'][0] == 'CONTENT_REJECTED'
        assert redis.store['account_health:acct-1']['last_failure_reason'] == 'CONTENT_REJECTED'

    asyncio.run(run())


def test_worker_private_http_disabled_fails_closed():
    async def run():
        redis = FakeRedis()
        worker = HTTPWorker(
            redis,
            IdentityContext(account_id='acct-1', proxy_url='http://proxy-1'),
            AppConfig(enable_private_facebook_http=False),
        )
        result = await worker.process_job(PostJob(account_id='acct-1', page_id='123', caption='valid caption'))
        assert not result.success
        assert result.status == 'PRIVATE_HTTP_DISABLED'
        assert 'post_times:acct-1' not in redis.store

    asyncio.run(run())


def test_worker_records_runtime_profile_observation():
    async def run():
        redis = FakeRedis()
        identity = IdentityContext(account_id='acct-1', proxy_url='http://proxy-1')
        await IdentityRegistry(redis).register(identity)
        await TokenVault(redis).set('acct-1', {'fb_dtsg': 'd', 'lsd': 'l', 'user_id': '1'})
        worker = HTTPWorker(
            redis,
            identity,
            AppConfig(enable_private_facebook_http=False),
        )
        result = await worker.process_job(PostJob(account_id='acct-1', page_id='123', caption='valid caption'))
        assert not result.success
        assert result.status == 'PRIVATE_HTTP_DISABLED'
        runtime_events = [
            event
            for event in redis.streams.get('detection:events', [])
            if event.get('event_type') == 'RUNTIME_PROFILE'
        ]
        assert runtime_events
        assert runtime_events[0]['account_id'] == 'acct-1'
        assert runtime_events[0]['evidence']

    asyncio.run(run())
