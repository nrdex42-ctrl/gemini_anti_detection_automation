import asyncio

from fb_automation.detection import DetectionEngine
from fb_automation.identity import IdentityRegistry
from fb_automation.models import IdentityContext
from fb_automation.tokens import TokenVault

from .fakes import FakeRedis


def test_detection_engine_flags_runtime_profile_drift_and_stealth_markers():
    async def run():
        redis = FakeRedis()
        identity = IdentityContext(account_id='acct-1', proxy_url='http://proxy-1')
        await IdentityRegistry(redis).register(identity)
        await TokenVault(redis).set('acct-1', {'fb_dtsg': 'd', 'lsd': 'l', 'user_id': '1'})

        engine = DetectionEngine(redis)
        await engine.record_observation('acct-1', 'RUNTIME_PROFILE', {
            'user_agent': identity.user_agent,
            'platform': identity.platform,
            'timezone': identity.timezone,
            'locale': identity.locale,
            'proxy_url': identity.proxy_url,
            'viewport': identity.viewport,
            'screen_resolution': identity.screen_resolution,
            'color_depth': identity.color_depth,
            'chrome_version': identity.chrome_version,
            'webgl_vendor': identity.webgl_vendor,
            'webgl_renderer': identity.webgl_renderer,
            'audio_sample_rate': identity.audio_sample_rate,
            'transport_mode': 'browser_fallback',
            'browser_fallback_enabled': True,
            'private_http_enabled': False,
        })
        await engine.record_observation('acct-1', 'RUNTIME_PROFILE', {
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36',
            'platform': identity.platform,
            'timezone': 'Europe/London',
            'locale': 'en-GB',
            'proxy_url': 'http://proxy-2',
            'viewport': identity.viewport,
            'screen_resolution': identity.screen_resolution,
            'color_depth': identity.color_depth,
            'chrome_version': '127.0.0.0',
            'webgl_vendor': identity.webgl_vendor,
            'webgl_renderer': identity.webgl_renderer,
            'audio_sample_rate': identity.audio_sample_rate,
            'headless': True,
            'webdriver': True,
            'browser_stealth': True,
            'dns_over_https': True,
            'transport_mode': 'private_http',
            'browser_fallback_enabled': False,
            'private_http_enabled': True,
        })

        report = await engine.evaluate_account('acct-1')
        rule_ids = {finding.rule_id for finding in report.findings}
        assert 'RUNTIME_PROFILE_DRIFT' in rule_ids
        assert 'RUNTIME_PROFILE_SUSPICIOUS' in rule_ids
        assert report.blocked
        assert report.risk_score >= 8

    asyncio.run(run())
