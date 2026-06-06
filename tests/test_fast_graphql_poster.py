import os
import asyncio
import json
from typing import Any, Dict, List, Tuple

from fb_automation.config import AppConfig
from fb_automation.models import IdentityContext
from fb_automation.tokens import TokenVault
from fb_automation.identity import IdentityRegistry
from fb_automation.fast_graphql_poster import (
    is_fast_graphql_enabled,
    create_facebook_posts_fast,
    create_facebook_posts_fast_sync
)

from .fakes import FakeRedis


def test_is_fast_graphql_enabled():
    # Verify it aligns with AppConfig settings
    original = os.environ.get('FB_AUTOMATION_ENABLE_PRIVATE_HTTP')
    try:
        os.environ['FB_AUTOMATION_ENABLE_PRIVATE_HTTP'] = 'true'
        assert is_fast_graphql_enabled() is True
        os.environ['FB_AUTOMATION_ENABLE_PRIVATE_HTTP'] = 'false'
        assert is_fast_graphql_enabled() is False
    finally:
        if original is not None:
            os.environ['FB_AUTOMATION_ENABLE_PRIVATE_HTTP'] = original
        else:
            os.environ.pop('FB_AUTOMATION_ENABLE_PRIVATE_HTTP', None)


def test_fast_graphql_disabled_fallback():
    async def run():
        # Setup configuration
        original = os.environ.get('FB_AUTOMATION_ENABLE_PRIVATE_HTTP')
        os.environ['FB_AUTOMATION_ENABLE_PRIVATE_HTTP'] = 'false'
        
        fallback_called = []
        async def mock_fallback(posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            fallback_called.append(posts)
            return [{'page': 'test_page', 'success': True, 'result': 'fallback_success'}]

        try:
            posts = [{'page_id_or_url': '123', 'post_type': 'text', 'caption': 'hello'}]
            cookies = json.dumps([{'name': 'c_user', 'value': '10001234'}])
            
            results = await create_facebook_posts_fast(
                cookies_json=cookies,
                posts=posts,
                browser_fallback=mock_fallback
            )
            
            assert len(fallback_called) == 1
            assert fallback_called[0] == posts
            assert results == [{'page': 'test_page', 'success': True, 'result': 'fallback_success'}]
        finally:
            if original is not None:
                os.environ['FB_AUTOMATION_ENABLE_PRIVATE_HTTP'] = original
            else:
                os.environ.pop('FB_AUTOMATION_ENABLE_PRIVATE_HTTP', None)

    asyncio.run(run())


def test_fast_graphql_missing_identity_fallback():
    async def run():
        original_http = os.environ.get('FB_AUTOMATION_ENABLE_PRIVATE_HTTP')
        original_redis = os.environ.get('REDIS_URL')
        
        os.environ['FB_AUTOMATION_ENABLE_PRIVATE_HTTP'] = 'true'
        os.environ['REDIS_URL'] = 'redis://localhost:6379'
        
        fallback_called = []
        async def mock_fallback(posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            fallback_called.append(posts)
            return [{'page': 'test_page', 'success': True, 'result': 'fallback_success'}]

        try:
            # We mock the _get_redis_client to return a FakeRedis
            import fb_automation.fast_graphql_poster as fgp
            original_get_redis = fgp._get_redis_client
            
            redis = FakeRedis()
            async def mock_get_redis():
                return redis
            fgp._get_redis_client = mock_get_redis

            posts = [{'page_id_or_url': '123', 'post_type': 'text', 'caption': 'hello'}]
            cookies = json.dumps([
                {'name': 'c_user', 'value': '10001234'},
                {'name': 'xs', 'value': 'session_secret'}
            ])
            
            # Since no identity registry entry exists, it must fallback to browser
            results = await create_facebook_posts_fast(
                cookies_json=cookies,
                posts=posts,
                browser_fallback=mock_fallback
            )
            
            assert len(fallback_called) == 1
            assert results == [{'page': 'test_page', 'success': True, 'result': 'fallback_success'}]
            
            fgp._get_redis_client = original_get_redis
        finally:
            if original_http is not None:
                os.environ['FB_AUTOMATION_ENABLE_PRIVATE_HTTP'] = original_http
            else:
                os.environ.pop('FB_AUTOMATION_ENABLE_PRIVATE_HTTP', None)
            if original_redis is not None:
                os.environ['REDIS_URL'] = original_redis
            else:
                os.environ.pop('REDIS_URL', None)

    asyncio.run(run())


def test_fast_graphql_sync_wrapper():
    # Setup configuration
    original = os.environ.get('FB_AUTOMATION_ENABLE_PRIVATE_HTTP')
    os.environ['FB_AUTOMATION_ENABLE_PRIVATE_HTTP'] = 'false'
    
    fallback_called = []
    def mock_fallback_sync(posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        fallback_called.append(posts)
        return [{'page': 'test_page', 'success': True, 'result': 'fallback_success_sync'}]

    try:
        posts = [{'page_id_or_url': '123', 'post_type': 'text', 'caption': 'hello'}]
        cookies = json.dumps([{'name': 'c_user', 'value': '10001234'}])
        
        results = create_facebook_posts_fast_sync(
            cookies_json=cookies,
            posts=posts,
            browser_fallback=mock_fallback_sync
        )
        
        assert len(fallback_called) == 1
        assert results == [{'page': 'test_page', 'success': True, 'result': 'fallback_success_sync'}]
    finally:
        if original is not None:
            os.environ['FB_AUTOMATION_ENABLE_PRIVATE_HTTP'] = original
        else:
            os.environ.pop('FB_AUTOMATION_ENABLE_PRIVATE_HTTP', None)


def test_fast_graphql_auto_healing_retry():
    async def run():
        original_http = os.environ.get('FB_AUTOMATION_ENABLE_PRIVATE_HTTP')
        original_redis = os.environ.get('REDIS_URL')
        original_get_redis = None
        original_post = None
        original_extract = None
        
        os.environ['FB_AUTOMATION_ENABLE_PRIVATE_HTTP'] = 'true'
        os.environ['REDIS_URL'] = 'redis://localhost:6379'
        
        try:
            import fb_automation.fast_graphql_poster as fgp
            from fb_automation.graphql_poster import HardenedGraphQLPoster
            from fb_automation.browser_fallback import BrowserTokenExtractor

            original_get_redis = fgp._get_redis_client
            original_post = HardenedGraphQLPoster.post_to_page
            original_extract = BrowserTokenExtractor.extract_with_retry
            
            redis = FakeRedis()
            # Register identity so we don't fall back immediately
            registry = IdentityRegistry(redis)
            await registry.register(IdentityContext(account_id='10001234', proxy_url=''))
            
            # Setup tokens in vault
            vault = TokenVault(redis)
            await vault.set('10001234', {'fb_dtsg': 'old_dtsg', 'lsd': 'old_lsd', 'cookie_header': 'c_user=10001234'})
            
            async def mock_get_redis():
                return redis
            fgp._get_redis_client = mock_get_redis

            # Mock Poster calls
            post_calls = []
            async def mock_post_to_page(self, page_id, caption, media_fbid=None, cookie_header=None):
                post_calls.append({
                    'page_id': page_id,
                    'caption': caption,
                    'media_fbid': media_fbid,
                    'cookie_header': cookie_header
                })
                if len(post_calls) == 1:
                    return False, 'TOKEN_EXPIRED', None
                return True, 'SUCCESS', 'post-healing-123'

            HardenedGraphQLPoster.post_to_page = mock_post_to_page

            # Mock Extractor calls
            extract_calls = []
            async def mock_extract_with_retry(self, cookies_json):
                extract_calls.append(cookies_json)
                return {'fb_dtsg': 'new_dtsg', 'lsd': 'new_lsd', 'cookie_header': 'c_user=10001234; xs=new'}

            BrowserTokenExtractor.extract_with_retry = mock_extract_with_retry

            posts = [{'page_id_or_url': '123', 'post_type': 'text', 'caption': 'hello healing'}]
            cookies = json.dumps([
                {'name': 'c_user', 'value': '10001234'},
                {'name': 'xs', 'value': 'session_secret'}
            ])
            
            results = await create_facebook_posts_fast(
                cookies_json=cookies,
                posts=posts,
                browser_fallback=None
            )
            
            # Verify results
            assert len(post_calls) == 2
            assert len(extract_calls) == 1
            assert results == [{
                'page': '123',
                'success': True,
                'result': 'Post accepted by Facebook via direct GraphQL. ID: post-healing-123'
            }]
        finally:
            import fb_automation.fast_graphql_poster as fgp
            from fb_automation.graphql_poster import HardenedGraphQLPoster
            from fb_automation.browser_fallback import BrowserTokenExtractor

            if original_get_redis is not None:
                fgp._get_redis_client = original_get_redis
            if original_post is not None:
                HardenedGraphQLPoster.post_to_page = original_post
            if original_extract is not None:
                BrowserTokenExtractor.extract_with_retry = original_extract

            if original_http is not None:
                os.environ['FB_AUTOMATION_ENABLE_PRIVATE_HTTP'] = original_http
            else:
                os.environ.pop('FB_AUTOMATION_ENABLE_PRIVATE_HTTP', None)
            if original_redis is not None:
                os.environ['REDIS_URL'] = original_redis
            else:
                os.environ.pop('REDIS_URL', None)

    asyncio.run(run())
