import asyncio

from fb_automation.browser_fallback import BrowserTokenExtractor
from fb_automation.browser_fallback import BrowserVideoUploader
from fb_automation.models import IdentityContext
from fb_automation.tokens import TokenVault

from .fakes import FakeRedis


def test_token_extraction_lock_is_exclusive_and_owner_bound():
    async def run():
        redis = FakeRedis()
        extractor = BrowserTokenExtractor(TokenVault(redis), IdentityContext(account_id='acct-1', proxy_url='http://proxy-1'))
        assert await extractor.acquire_extraction_lock('worker-a')
        assert not await extractor.acquire_extraction_lock('worker-b')
        await extractor.release_extraction_lock('worker-b')
        assert redis.store['token_extract_lock:acct-1'] == 'worker-a'
        await extractor.release_extraction_lock('worker-a')
        assert 'token_extract_lock:acct-1' not in redis.store

    asyncio.run(run())


def test_extract_tokens_fails_fast_when_lock_exists():
    async def run():
        redis = FakeRedis()
        await redis.set('token_extract_lock:acct-1', 'worker-a')
        extractor = BrowserTokenExtractor(TokenVault(redis), IdentityContext(account_id='acct-1', proxy_url='http://proxy-1'))
        try:
            await extractor.extract_tokens('[]')
        except RuntimeError as exc:
            assert 'already running' in str(exc)
        else:
            raise AssertionError('expected token extraction lock failure')

    asyncio.run(run())


def test_extract_with_retry_returns_tokens_from_vault_when_lock_exists():
    async def run():
        redis = FakeRedis()
        vault = TokenVault(redis)
        await vault.set('acct-1', {'fb_dtsg': 'a', 'lsd': 'b', 'user_id': '1'})
        await redis.set('token_extract_lock:acct-1', 'worker-a')
        extractor = BrowserTokenExtractor(vault, IdentityContext(account_id='acct-1', proxy_url='http://proxy-1'))
        tokens = await extractor.extract_with_retry('[]', max_wait_seconds=1, poll_interval_seconds=0.05)
        assert tokens is not None
        assert tokens['fb_dtsg'] == 'a'

    asyncio.run(run())


def test_browser_video_uploader_uses_legacy_adapter(tmp_path):
    async def run():
        video = tmp_path / 'video.mp4'
        video.write_bytes(b'video-bytes')
        captured = {}
        events = []

        async def legacy(cookies_json, posts, progress_callback=None):
            del progress_callback
            captured['cookies'] = cookies_json
            captured['posts'] = posts
            return [{'page': posts[0]['page_name'], 'success': True, 'result': 'legacy-video-post'}]

        async def progress(event):
            events.append(event)

        uploader = BrowserVideoUploader(legacy)
        ok, detail = await uploader.upload_video(
            str(video),
            '[{"name":"c_user","value":"1"}]',
            IdentityContext(account_id='acct-1', proxy_url='http://proxy-1'),
            page_id_or_url='page-1',
            caption='valid video caption',
            page_name='Page 1',
            progress_callback=progress,
        )
        assert ok
        assert detail == 'legacy-video-post'
        assert captured['posts'][0]['post_type'] == 'video'
        assert captured['posts'][0]['media_url'] == str(video)
        assert events[0]['stage'] == 'browser_video_fallback_started'
        assert events[-1]['stage'] == 'browser_video_fallback_completed'

    asyncio.run(run())


def test_browser_video_uploader_normalizes_failures(tmp_path):
    async def run():
        video = tmp_path / 'video.mp4'
        video.write_bytes(b'video-bytes')

        async def legacy(cookies_json, posts, progress_callback=None):
            del cookies_json, posts, progress_callback
            return [{'success': False, 'result': 'composer failed'}]

        uploader = BrowserVideoUploader(legacy)
        result = await uploader.upload_video_post(
            str(video),
            '[{"name":"c_user","value":"1"}]',
            IdentityContext(account_id='acct-1', proxy_url='http://proxy-1'),
            page_id_or_url='page-1',
        )
        assert not result.success
        assert result.status == 'BROWSER_VIDEO_FALLBACK_FAILED'
        assert result.error_message == 'composer failed'

        missing = await uploader.upload_video_post(
            str(tmp_path / 'missing.mp4'),
            '[{"name":"c_user","value":"1"}]',
            IdentityContext(account_id='acct-1', proxy_url='http://proxy-1'),
            page_id_or_url='page-1',
        )
        assert not missing.success
        assert missing.status == 'BROWSER_VIDEO_FILE_MISSING'

    asyncio.run(run())
