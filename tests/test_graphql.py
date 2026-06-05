import asyncio
import json
from typing import Any, Dict, List, Tuple
from urllib import parse as urllib_parse

from fb_automation.config import AppConfig
from fb_automation.graphql_poster import HardenedGraphQLPoster
from fb_automation.models import IdentityContext
from fb_automation.network import ProxyManager
from fb_automation.tokens import TokenVault

from .fakes import FakeRedis


class MockGraphQLPoster(HardenedGraphQLPoster):
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


def test_graphql_private_http_disabled():
    async def run():
        redis = FakeRedis()
        identity = IdentityContext(account_id='acct-1', proxy_url='http://proxy-1')
        poster = HardenedGraphQLPoster(
            TokenVault(redis),
            identity,
            redis,
            ProxyManager([], redis),
            AppConfig(enable_private_facebook_http=False),
        )
        ok, status, post_id = await poster.post_to_page('123', 'hello')
        assert not ok
        assert status == 'PRIVATE_HTTP_DISABLED'
        assert post_id is None

    asyncio.run(run())


def test_graphql_doc_id_fallback_and_idempotency_poll():
    async def run():
        redis = FakeRedis()
        identity = IdentityContext(account_id='acct-1', proxy_url='http://proxy-1')
        poster = HardenedGraphQLPoster(
            TokenVault(redis),
            identity,
            redis,
            ProxyManager([], redis),
            AppConfig(enable_private_facebook_http=False),
        )
        assert await poster._get_doc_id() == '7711610262198779'
        await redis.set('fb_post_idemp:test', 'post-123')
        assert await poster._wait_for_idempotent_result('fb_post_idemp:test', attempts=1) == 'post-123'

    asyncio.run(run())


def test_graphql_payload_contract_is_compact_and_typed():
    async def run():
        redis = FakeRedis()
        await redis.set('fb_graphql_doc_id', 'doc-1')
        identity = IdentityContext(account_id='acct-1', proxy_url='http://proxy-1', facebook_user_id='user-1')
        poster = HardenedGraphQLPoster(
            TokenVault(redis),
            identity,
            redis,
            ProxyManager([], redis),
            AppConfig(enable_private_facebook_http=False),
        )
        payload = await poster.build_payload(
            '123',
            'valid caption',
            {'fb_dtsg': 'd', 'lsd': 'l', 'user_id': 'user-1'},
            'media-1',
            'a' * 32,
        )
        variables = json.loads(payload['variables'])
        assert payload['doc_id'] == 'doc-1'
        assert payload['__a'] == '1'
        assert payload['__user'] == 'user-1'
        assert '\n' not in payload['variables']
        assert variables['input']['actor_id'] == '123'
        assert variables['input']['message']['ranges'] == []
        assert variables['input']['attachments'][0]['media_fbid'] == 'media-1'

    asyncio.run(run())


def test_graphql_mocked_success_records_idempotency_and_usage():
    async def run():
        redis = FakeRedis()
        identity = IdentityContext(account_id='acct-1', proxy_url='http://proxy-1', facebook_user_id='user-1')
        vault = TokenVault(redis)
        await vault.set('acct-1', {'fb_dtsg': 'd', 'lsd': 'l', 'user_id': 'user-1'})
        await redis.set('fb_graphql_doc_id', 'doc-1')
        poster = MockGraphQLPoster(
            vault,
            identity,
            redis,
            ProxyManager(['http://proxy-1'], redis),
            AppConfig(enable_private_facebook_http=True),
            responses=[
                (
                    200,
                    json.dumps({
                        'data': {
                            'composer_story_create': {
                                'story': {'legacy_story_hideable_id': 'post-123'}
                            }
                        }
                    }),
                )
            ],
        )
        ok, status, post_id = await poster.post_to_page('page-1', 'valid caption')
        assert ok
        assert status == 'SUCCESS'
        assert post_id == 'post-123'
        assert poster.calls[0]['url'] == 'https://www.facebook.com/api/graphql/'
        assert poster.calls[0]['proxy'] == 'http://proxy-1'
        body = urllib_parse.parse_qs(poster.calls[0]['data'].decode('utf-8'))
        assert body['doc_id'][0] == 'doc-1'
        assert redis.store['fb_tokens:acct-1:usage'] == 1
        assert redis.store['account_health:acct-1']['success_streak'] == 1

    asyncio.run(run())


def test_graphql_mocked_checkpoint_quarantines_and_releases_idempotency():
    async def run():
        redis = FakeRedis()
        identity = IdentityContext(account_id='acct-1', proxy_url='http://proxy-1', facebook_user_id='user-1')
        vault = TokenVault(redis)
        await vault.set('acct-1', {'fb_dtsg': 'd', 'lsd': 'l', 'user_id': 'user-1'})
        await redis.set('fb_graphql_doc_id', 'doc-1')
        poster = MockGraphQLPoster(
            vault,
            identity,
            redis,
            ProxyManager(['http://proxy-1'], redis),
            AppConfig(enable_private_facebook_http=True),
            responses=[(200, json.dumps({'errors': [{'message': 'security checkpoint required'}]}))],
        )
        ok, status, post_id = await poster.post_to_page('page-1', 'valid caption')
        assert not ok
        assert status == 'SECURITY_CHECKPOINT'
        assert post_id is None
        assert redis.store['quarantine_level:acct-1'] == 'SEVERE'
        assert not [key for key, value in redis.store.items() if key.startswith('fb_post_idemp:') and value == 'PENDING']

    asyncio.run(run())
