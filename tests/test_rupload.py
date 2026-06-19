import asyncio
from pathlib import Path
from typing import Any, Dict, List, Tuple

from PIL import Image

from fb_automation.config import AppConfig
from fb_automation.models import IdentityContext
from fb_automation.network import ProxyManager
from fb_automation.rupload import HardenedRupload
from fb_automation.tokens import TokenVault

from .fakes import FakeRedis


class MockRupload(HardenedRupload):
    def __init__(self, *args: Any, responses: List[Tuple[int, str]], **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    async def _post_form(
        self,
        url: str,
        data: Any,
        headers: Dict[str, str],
        proxy: str,
        timeout_seconds: int,
    ) -> Tuple[int, str]:
        self.calls.append({
            'url': url,
            'data': data,
            'headers': headers,
            'proxy': proxy,
            'timeout_seconds': timeout_seconds,
        })
        return self.responses.pop(0)

    @staticmethod
    async def _sleep(seconds: float) -> None:
        del seconds


def test_rupload_validates_and_stays_disabled(tmp_path: Path):
    async def run():
        image_path = tmp_path / 'image.jpg'
        image_module = Image  # type: Any
        image_module.new('RGB', (200, 200), (255, 255, 255)).save(image_path, quality=90)
        redis = FakeRedis()
        identity = IdentityContext(account_id='acct-1', proxy_url='http://proxy-1')
        uploader = HardenedRupload(
            TokenVault(redis),
            identity,
            redis,
            ProxyManager([], redis),
            AppConfig(enable_private_facebook_http=False),
        )
        assert uploader._validate_image(str(image_path))
        ok, media_id, detail = await uploader.upload_image(str(image_path))
        assert not ok
        assert media_id is None
        assert 'disabled' in detail.lower()

    asyncio.run(run())


def test_rupload_init_payload_contract():
    redis = FakeRedis()
    identity = IdentityContext(account_id='acct-1', proxy_url='http://proxy-1')
    uploader = HardenedRupload(
        TokenVault(redis),
        identity,
        redis,
        ProxyManager([], redis),
        AppConfig(enable_private_facebook_http=False),
    )
    payload = uploader.build_upload_init_payload(
        {'fb_dtsg': 'd', 'lsd': 'l'},
        2048,
        'mutated.jpg',
    )
    assert payload['media_type'] == 'image/jpeg'
    assert payload['file_size'] == '2048'
    assert payload['file_name'] == 'mutated.jpg'


def test_rupload_json_loader_handles_facebook_prefix():
    assert HardenedRupload._loads_json('for(;;);{"upload_session_id":"1"}')['upload_session_id'] == '1'


def test_rupload_mutates_grayscale_image(tmp_path: Path):
    image_path = tmp_path / 'gray.png'
    image_module = Image  # type: Any
    image_module.new('L', (180, 180), 128).save(image_path)
    redis = FakeRedis()
    identity = IdentityContext(account_id='acct-1', proxy_url='http://proxy-1')
    uploader = HardenedRupload(
        TokenVault(redis),
        identity,
        redis,
        ProxyManager([], redis),
        AppConfig(enable_private_facebook_http=False),
    )

    mutated_path, image_bytes = uploader._mutate_and_encode(str(image_path))
    try:
        assert mutated_path != str(image_path)
        assert image_bytes.startswith(b'\xff\xd8')
        assert len(image_bytes) > 0
        with image_module.open(mutated_path) as mutated:
            assert mutated.mode == 'RGB'
            assert mutated.format == 'JPEG'
    finally:
        Path(mutated_path).unlink(missing_ok=True)


def test_rupload_mocked_success_uploads_mutated_image(tmp_path: Path):
    async def run():
        image_path = tmp_path / 'image.jpg'
        image_module = Image  # type: Any
        image_module.new('RGB', (240, 240), (255, 255, 255)).save(image_path, quality=90)
        redis = FakeRedis()
        identity = IdentityContext(account_id='acct-1', proxy_url='http://proxy-1')
        vault = TokenVault(redis)
        await vault.set('acct-1', {'fb_dtsg': 'd', 'lsd': 'l', 'user_id': 'user-1'})
        uploader = MockRupload(
            vault,
            identity,
            redis,
            ProxyManager(['http://proxy-1'], redis),
            AppConfig(enable_private_facebook_http=True),
            responses=[(200, '{"upload_session_id":"session-1"}'), (200, '{"fbid":"media-1"}')],
        )
        ok, media_id, detail = await uploader.upload_image(str(image_path))
        assert ok
        assert media_id == 'media-1'
        assert detail == 'Success'
        assert len(uploader.calls) == 2
        assert uploader.calls[0]['url'] == 'https://rupload.facebook.com/photo-upload/v1'
        assert uploader.calls[0]['data'] == b''
        h0 = {k.lower(): v for k, v in uploader.calls[0]['headers'].items()}
        h1 = {k.lower(): v for k, v in uploader.calls[1]['headers'].items()}
        assert h0.get('content-type') == 'application/x-www-form-urlencoded'
        assert h0.get('content-length') == '0'
        assert h0.get('x-entity-type') == 'image/jpeg'
        assert h0.get('sec-fetch-site') == 'same-site'
        assert h0.get('host') == 'rupload.facebook.com'
        assert h0.get('x-fb-lsd') == 'l'
        assert h0.get('x-fb-fb-dtsg') == 'd' or h0.get('x-fb-dtsg') == 'd'
        assert h0.get('x-fb-upload-retry-count') == '0' or h0.get('x-fb-retry-count') == '0'
        assert 'x-fb-friendly-name' not in h0
        assert uploader.calls[0]['proxy'] == 'http://proxy-1'
        assert uploader.calls[1]['url'] == 'https://rupload.facebook.com/photo-upload/v1/session-1'
        assert isinstance(uploader.calls[1]['data'], bytes)
        assert h1.get('content-type') == 'image/jpeg'
        assert h1.get('content-length') == str(len(uploader.calls[1]['data']))
        assert h1.get('x-entity-length') == str(len(uploader.calls[1]['data']))
        assert h1.get('x-start-offset') == '0'
        assert redis.store['fb_tokens:acct-1:usage'] == 1

    asyncio.run(run())


def test_rupload_mocked_transfer_failure_reports_error(tmp_path: Path):
    async def run():
        image_path = tmp_path / 'image.jpg'
        image_module = Image  # type: Any
        image_module.new('RGB', (240, 240), (255, 255, 255)).save(image_path, quality=90)
        redis = FakeRedis()
        identity = IdentityContext(account_id='acct-1', proxy_url='http://proxy-1')
        vault = TokenVault(redis)
        await vault.set('acct-1', {'fb_dtsg': 'd', 'lsd': 'l', 'user_id': 'user-1'})
        uploader = MockRupload(
            vault,
            identity,
            redis,
            ProxyManager(['http://proxy-1'], redis),
            AppConfig(enable_private_facebook_http=True),
            responses=[(200, '{"upload_session_id":"session-1"}'), (500, '{"error":"upload rejected"}')],
        )
        ok, media_id, detail = await uploader.upload_image(str(image_path))
        assert not ok
        assert media_id is None
        assert 'RUPLOAD_TRANSFER_HTTP_500' in detail

    asyncio.run(run())


# ── Video upload tests ──

def test_rupload_video_validates(tmp_path: Path):
    uploader = HardenedRupload(
        TokenVault(FakeRedis()),
        IdentityContext(account_id='acct-1', proxy_url='http://proxy-1'),
        FakeRedis(),
        ProxyManager([], FakeRedis()),
        AppConfig(enable_private_facebook_http=False),
    )
    video = tmp_path / 'test.mp4'
    video.write_bytes(b'\x00\x00\x00\x00' * 256)
    assert uploader._validate_video(str(video))
    bad = tmp_path / 'test.txt'
    bad.write_text('not a video')
    assert not uploader._validate_video(str(bad))


def test_rupload_video_disabled(tmp_path: Path):
    async def run():
        video = tmp_path / 'test.mp4'
        video.write_bytes(b'\x00\x00\x00\x00' * 256)
        uploader = HardenedRupload(
            TokenVault(FakeRedis()),
            IdentityContext(account_id='acct-1', proxy_url='http://proxy-1'),
            FakeRedis(),
            ProxyManager([], FakeRedis()),
            AppConfig(enable_private_facebook_http=False),
        )
        ok, media_id, detail = await uploader.upload_video(str(video))
        assert not ok
        assert media_id is None
        assert 'disabled' in detail.lower()

    asyncio.run(run())


def test_rupload_video_mocked_success(tmp_path: Path):
    async def run():
        video = tmp_path / 'test.mp4'
        video.write_bytes(b'\x00\x00\x00\x00' * 256)
        redis = FakeRedis()
        identity = IdentityContext(account_id='acct-1', proxy_url='http://proxy-1')
        vault = TokenVault(redis)
        await vault.set('acct-1', {'fb_dtsg': 'd', 'lsd': 'l', 'user_id': 'user-1'})

        uploader = MockRupload(
            vault,
            identity,
            redis,
            ProxyManager(['http://proxy-1'], redis),
            AppConfig(enable_private_facebook_http=True),
            responses=[
                (200, '{"payload":{"upload_url":"https://rupload-test.up.facebook.com/fb_video/hash-0-1024","upload_session_id":"vsess-1"}}'),
                (200, '{"fbid":"vmedia-1"}'),
                (200, '{"fbid":"vmedia-1"}'),
            ],
        )
        ok, media_id, detail = await uploader.upload_video(str(video))
        assert ok
        assert media_id == 'vmedia-1'
        assert detail == 'Success'
        assert len(uploader.calls) == 3
        assert 'vupload2.facebook.com/ajax/video/upload/requests/start/' in uploader.calls[0]['url']
        assert 'rupload-test.up.facebook.com' in uploader.calls[1]['url']
        assert 'vupload2.facebook.com/ajax/video/upload/requests/receive/' in uploader.calls[2]['url']
        assert uploader.calls[1]['headers']['X-Entity-Type'] == 'video/mp4'
        assert uploader.calls[1]['headers']['X-Start-Offset'] == '0'
        assert isinstance(uploader.calls[1]['data'], bytes)
        assert redis.store['fb_tokens:acct-1:usage'] == 1

    asyncio.run(run())


def test_rupload_video_transfer_no_fbid_relies_on_receive(tmp_path: Path):
    async def run():
        video = tmp_path / 'test.mp4'
        video.write_bytes(b'\x00\x00\x00\x00' * 256)
        redis = FakeRedis()
        identity = IdentityContext(account_id='acct-1', proxy_url='http://proxy-1')
        vault = TokenVault(redis)
        await vault.set('acct-1', {'fb_dtsg': 'd', 'lsd': 'l', 'user_id': 'user-1'})

        uploader = MockRupload(
            vault,
            identity,
            redis,
            ProxyManager(['http://proxy-1'], redis),
            AppConfig(enable_private_facebook_http=True),
            responses=[
                (200, '{"payload":{"upload_url":"https://rupload-test.up.facebook.com/fb_video/hash-0-1024","upload_session_id":"vsess-1"}}'),
                (200, '{"status":"success"}'),
                (200, '{"payload":{"fbid":"vmedia-2"}}'),
            ],
        )
        ok, media_id, detail = await uploader.upload_video(str(video))
        assert ok
        assert media_id == 'vmedia-2'
        assert detail == 'Success'
        assert len(uploader.calls) == 3

    asyncio.run(run())


def test_rupload_video_init_failure(tmp_path: Path):
    async def run():
        video = tmp_path / 'test.mp4'
        video.write_bytes(b'\x00\x00\x00\x00' * 256)
        redis = FakeRedis()
        identity = IdentityContext(account_id='acct-1', proxy_url='http://proxy-1')
        vault = TokenVault(redis)
        await vault.set('acct-1', {'fb_dtsg': 'd', 'lsd': 'l', 'user_id': 'user-1'})

        uploader = MockRupload(
            vault,
            identity,
            redis,
            ProxyManager(['http://proxy-1'], redis),
            AppConfig(enable_private_facebook_http=True),
            responses=[(403, '{"error":"forbidden"}')],
        )
        ok, media_id, detail = await uploader.upload_video(str(video))
        assert not ok
        assert media_id is None
        assert 'VUPLOAD_INIT_HTTP_403' in detail

    asyncio.run(run())


def test_rupload_video_receive_failure(tmp_path: Path):
    async def run():
        video = tmp_path / 'test.mp4'
        video.write_bytes(b'\x00\x00\x00\x00' * 256)
        redis = FakeRedis()
        identity = IdentityContext(account_id='acct-1', proxy_url='http://proxy-1')
        vault = TokenVault(redis)
        await vault.set('acct-1', {'fb_dtsg': 'd', 'lsd': 'l', 'user_id': 'user-1'})

        uploader = MockRupload(
            vault,
            identity,
            redis,
            ProxyManager(['http://proxy-1'], redis),
            AppConfig(enable_private_facebook_http=True),
            responses=[
                (200, '{"payload":{"upload_url":"https://rupload-test.up.facebook.com/fb_video/hash-0-1024","upload_session_id":"vsess-1"}}'),
                (200, '{"status":"success"}'),
                (500, '{"error":"confirm failed"}'),
            ],
        )
        ok, media_id, detail = await uploader.upload_video(str(video))
        assert not ok
        assert media_id is None
        assert 'VUPLOAD_RECEIVE_HTTP_500' in detail

    asyncio.run(run())
