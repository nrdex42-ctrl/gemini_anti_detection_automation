import asyncio

from fb_automation.identity import IdentityRegistry
from fb_automation.models import IdentityContext
from fb_automation.network import ProxyManager

from .fakes import FakeRedis


def test_proxy_manager_hard_fails_when_proxy_required_and_pool_empty():
    try:
        ProxyManager([], FakeRedis(), require_proxy=True)
    except ValueError as exc:
        assert 'proxy pool' in str(exc)
    else:
        raise AssertionError('expected empty proxy pool rejection')


def test_proxy_manager_uses_registered_identity_proxy():
    async def run():
        redis = FakeRedis()
        await IdentityRegistry(redis).register(IdentityContext(account_id='acct-1', proxy_url='http://proxy-1'))
        manager = ProxyManager(['http://proxy-1'], redis, require_proxy=True)
        assert await manager.get_proxy_for_account('acct-1') == 'http://proxy-1'
        assert redis.store['proxy_sticky:acct-1'] == 'http://proxy-1'

    asyncio.run(run())


def test_proxy_manager_rejects_identity_proxy_outside_pool():
    async def run():
        redis = FakeRedis()
        await IdentityRegistry(redis).register(IdentityContext(account_id='acct-1', proxy_url='http://proxy-1'))
        manager = ProxyManager(['http://proxy-2'], redis, require_proxy=True)
        try:
            await manager.get_proxy_for_account('acct-1')
        except ValueError as exc:
            assert 'not in the configured proxy pool' in str(exc)
        else:
            raise AssertionError('expected identity proxy pool mismatch')

    asyncio.run(run())
