import asyncio

from fb_automation.identity import IdentityRegistry
from fb_automation.models import IdentityContext

from .fakes import FakeRedis


def test_identity_roundtrip_and_proxy_uniqueness():
    async def run():
        redis = FakeRedis()
        registry = IdentityRegistry(redis)
        ctx = IdentityContext(account_id='acct-1', proxy_url='http://proxy-1')
        await registry.register(ctx)
        loaded = await registry.get('acct-1')
        assert loaded == ctx
        assert await registry.is_proxy_unique('http://proxy-1', 'acct-1')
        assert not await registry.is_proxy_unique('http://proxy-1', 'acct-2')

    asyncio.run(run())


def test_identity_register_rejects_duplicate_proxy():
    async def run():
        redis = FakeRedis()
        registry = IdentityRegistry(redis)
        await registry.register(IdentityContext(account_id='acct-1', proxy_url='http://proxy-1'))
        try:
            await registry.register(IdentityContext(account_id='acct-2', proxy_url='http://proxy-1'))
        except ValueError as exc:
            assert 'already assigned' in str(exc)
        else:
            raise AssertionError('expected duplicate proxy rejection')

    asyncio.run(run())


def test_identity_viewport_validation():
    try:
        IdentityContext(
            account_id='acct-1',
            proxy_url='http://proxy-1',
            viewport=(2000, 1200),
            screen_resolution=(1920, 1080),
        )
    except ValueError as exc:
        assert 'viewport' in str(exc)
    else:
        raise AssertionError('expected validation error')


def test_identity_allows_direct_mode_and_rejects_invalid_proxy_and_platform_mismatch():
    def assert_invalid(kwargs, expected):
        try:
            IdentityContext(**kwargs)
        except ValueError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError('expected identity validation error')

    direct = IdentityContext(account_id='acct-1', proxy_url='')
    assert 'proxy' not in direct.to_browser_args()
    assert_invalid({'account_id': 'acct-1', 'proxy_url': 'proxy-1'}, 'proxy_url')
    assert_invalid({
        'account_id': 'acct-1',
        'proxy_url': 'http://proxy-1',
        'platform': 'MacIntel',
    }, 'platform')
