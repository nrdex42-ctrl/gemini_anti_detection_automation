import asyncio

from fb_automation.models import IdentityContext
from fb_automation.safety import QuarantineLevel, QuarantineManager, SafetyGuard, SafetyStatus
from fb_automation.tokens import TokenVault

from .fakes import FakeRedis


def test_quarantine_escalation_and_safety_check():
    async def run():
        redis = FakeRedis()
        manager = QuarantineManager(redis)
        level = await manager.escalate('acct-1', 'test')
        assert level == QuarantineLevel.SOFT
        ctx = IdentityContext(account_id='acct-1', proxy_url='http://proxy-1')
        guard = SafetyGuard(redis, ctx, TokenVault(redis))
        status, reason = await guard.pre_flight_check()
        assert status == SafetyStatus.QUARANTINE
        assert 'quarantined' in reason

    asyncio.run(run())


def test_rate_slot_reservation_limits():
    async def run():
        redis = FakeRedis()
        ctx = IdentityContext(account_id='acct-1', proxy_url='http://proxy-1')
        vault = TokenVault(redis)
        await vault.set('acct-1', {'fb_dtsg': 'a', 'lsd': 'b', 'user_id': '1'})
        guard = SafetyGuard(redis, ctx, vault)
        for _ in range(6):
            status, _reason = await guard.reserve_rate_slot()
            assert status == SafetyStatus.CLEAR
        status, reason = await guard.reserve_rate_slot()
        assert status == SafetyStatus.COOLDOWN
        assert 'hourly' in reason

    asyncio.run(run())


def test_account_lock_is_exclusive_and_owner_bound():
    async def run():
        redis = FakeRedis()
        ctx = IdentityContext(account_id='acct-1', proxy_url='http://proxy-1')
        guard = SafetyGuard(redis, ctx, TokenVault(redis))
        assert await guard.acquire_account_lock('worker-a')
        assert not await guard.acquire_account_lock('worker-b')
        await guard.release_account_lock('worker-b')
        assert redis.store['account_lock:acct-1'] == 'worker-a'
        await guard.release_account_lock('worker-a')
        assert 'account_lock:acct-1' not in redis.store
        assert await guard.acquire_account_lock('worker-b')

    asyncio.run(run())


def test_global_interval_uses_persisted_last_post_time():
    async def run():
        redis = FakeRedis()
        ctx = IdentityContext(account_id='acct-1', proxy_url='http://proxy-1')
        guard = SafetyGuard(redis, ctx, TokenVault(redis))
        await guard.record_post_time()
        can_proceed, sleep_seconds = await guard.enforce_global_interval()
        assert not can_proceed
        assert sleep_seconds > 119.0

    asyncio.run(run())


def test_fallback_ratio_tracking_and_rejection():
    async def run():
        redis = FakeRedis()
        ctx = IdentityContext(account_id='acct-1', proxy_url='http://proxy-1')
        guard = SafetyGuard(redis, ctx, TokenVault(redis))
        for _ in range(10):
            await guard.record_post_attempt()
        await guard.record_fallback()
        ok, _reason = await guard.check_fallback_ratio()
        assert ok
        await guard.record_fallback()
        ok, reason = await guard.check_fallback_ratio()
        assert not ok
        assert 'fallback ratio' in reason

    asyncio.run(run())


def test_trigger_quarantine_sets_level_consistently():
    async def run():
        redis = FakeRedis()
        ctx = IdentityContext(account_id='acct-1', proxy_url='http://proxy-1')
        guard = SafetyGuard(redis, ctx, TokenVault(redis))
        status = await guard.post_flight_validation(False, 500, "Your photos couldn't be uploaded")
        assert status == SafetyStatus.QUARANTINE
        assert redis.store['quarantine_level:acct-1'] == QuarantineLevel.HARD.value

    asyncio.run(run())
