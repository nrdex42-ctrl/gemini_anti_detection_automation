import asyncio
from pathlib import Path
from typing import Any, Dict, List

from PIL import Image

from fb_automation.identity import IdentityContext
from fb_automation.live_emulation import attempt_private_http_image_post


def test_attempt_private_http_image_post_success(tmp_path: Path):
    async def run():
        image_path = tmp_path / 'image.jpg'
        Image.new('RGB', (160, 160), (20, 40, 60)).save(image_path, 'JPEG')
        calls: Dict[str, Any] = {}

        class FakeUploader:
            def __init__(self, token_vault, identity, redis_client, proxy_manager, config):
                calls['uploader_identity'] = identity
                calls['uploader_tokens'] = token_vault
                calls['uploader_config'] = config

            async def upload_image(self, path: str):
                calls['upload_path'] = path
                return True, 'media-1', 'Success'

        class FakePoster:
            def __init__(self, token_vault, identity, redis_client, proxy_manager, config):
                calls['poster_identity'] = identity

            async def post_to_page(self, page_id: str, caption: str, media_fbid: str, cookie_header: str = ''):
                calls['post_to_page'] = (page_id, caption, media_fbid, cookie_header)
                return True, 'SUCCESS', 'post-1'

        post = {
            'page_id_or_url': 'https://www.facebook.com/profile.php?id=12345',
            'page_name': 'Page',
            'post_type': 'image',
            'caption': 'valid image caption',
            'media_url': str(image_path),
        }
        result = await attempt_private_http_image_post(
            post,
            {'fb_dtsg': 'd', 'lsd': 'l', 'user_id': 'user-1'},
            '[{"name":"c_user","value":"user-1"},{"name":"xs","value":"xs-1"}]',
            IdentityContext(account_id='acct-1', proxy_url=''),
            uploader_factory=FakeUploader,
            poster_factory=FakePoster,
        )

        assert result['success'] is True
        assert result['transport'] == 'private_http_image'
        assert result['post_id'] == 'post-1'
        assert calls['upload_path'] == str(image_path)
        assert calls['post_to_page'] == ('12345', 'valid image caption', 'media-1', 'c_user=user-1; xs=xs-1')
        assert calls['poster_identity'].facebook_user_id == 'user-1'

    asyncio.run(run())


def test_attempt_private_http_image_post_upload_failure(tmp_path: Path):
    async def run():
        image_path = tmp_path / 'image.jpg'
        Image.new('RGB', (160, 160), (20, 40, 60)).save(image_path, 'JPEG')
        poster_calls: List[str] = []

        class FakeUploader:
            def __init__(self, *args: Any):
                pass

            async def upload_image(self, path: str):
                return False, None, 'RUPLOAD_INIT_HTTP_400'

        class FakePoster:
            def __init__(self, *args: Any):
                poster_calls.append('created')

        post = {
            'page_id_or_url': '12345',
            'page_name': 'Page',
            'post_type': 'image',
            'caption': 'valid image caption',
            'media_url': str(image_path),
        }
        result = await attempt_private_http_image_post(
            post,
            {'fb_dtsg': 'd', 'lsd': 'l', 'user_id': 'user-1'},
            '[{"name":"c_user","value":"user-1"}]',
            IdentityContext(account_id='acct-1', proxy_url=''),
            uploader_factory=FakeUploader,
            poster_factory=FakePoster,
        )

        assert result['success'] is False
        assert result['transport'] == 'private_http_image'
        assert 'HTTP_IMAGE_UPLOAD_FAILED' in result['status']
        assert poster_calls == []

    asyncio.run(run())
