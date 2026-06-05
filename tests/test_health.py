import asyncio

from fb_automation.health import HealthMonitor
from fb_automation.identity import IdentityRegistry
from fb_automation.models import IdentityContext
from fb_automation.safety import QuarantineManager

from .fakes import FakeRedis


def test_health_monitor_global_snapshot_and_publish():
    async def run():
        redis = FakeRedis()
        registry = IdentityRegistry(redis)
        await registry.register(IdentityContext(
            account_id='acct-1',
            proxy_url='http://proxy-1',
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            chrome_version='126.0.0.0',
        ))
        await registry.register(IdentityContext(
            account_id='acct-2',
            proxy_url='http://proxy-2',
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36',
            chrome_version='127.0.0.0',
        ))
        await redis.hset('account_health:acct-1', 'success_streak', '2')
        await redis.lpush('outcomes:acct-2', 'PRIVATE_HTTP_DISABLED')
        await redis.lpush('outcomes:acct-2', 'CONTENT_REJECTED')
        await redis.lpush('outcomes:acct-2', 'TOKEN_EXPIRED')

        monitor = HealthMonitor(redis)
        snapshot = await monitor.global_snapshot()
        assert snapshot['account_count'] == 2
        assert snapshot['by_status']['CLEAR'] == 1
        assert snapshot['by_status']['DEGRADED'] == 1

        published = await monitor.publish_snapshot('health')
        assert published['account_count'] == 2
        assert redis.published[0][0] == 'health'

    asyncio.run(run())


def test_health_monitor_reports_quarantine():
    async def run():
        redis = FakeRedis()
        await IdentityRegistry(redis).register(IdentityContext(account_id='acct-1', proxy_url='http://proxy-1'))
        await QuarantineManager(redis).escalate('acct-1', 'test quarantine')
        snapshot = await HealthMonitor(redis).account_snapshot('acct-1')
        assert snapshot.status == 'QUARANTINED'
        assert snapshot.quarantine_level == 'SOFT'

    asyncio.run(run())
