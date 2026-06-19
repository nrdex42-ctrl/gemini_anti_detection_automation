"""Comprehensive tests for the TokenVault."""

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from fb_automation.tokens import TokenVault


@pytest.fixture
def mock_redis():
    mock = AsyncMock()
    # Mock specific redis commands to return None or specific values by default
    mock.get.return_value = None
    mock.setex = AsyncMock()
    mock.incr = AsyncMock(return_value=1)
    mock.expire = AsyncMock()
    return mock


@pytest.mark.asyncio
async def test_token_vault_set_and_get(mock_redis):
    """Test setting tokens and getting them from L1 cache."""
    vault = TokenVault(mock_redis)
    tokens = {
        'fb_dtsg': 'mock_dtsg',
        'lsd': 'mock_lsd',
        'user_id': '12345'
    }
    
    # Set tokens (stores in L1 and L2)
    await vault.set('account1', tokens)
    
    # Verify Redis setex was called
    mock_redis.setex.assert_called_once()
    args, kwargs = mock_redis.setex.call_args
    assert args[0] == 'fb_tokens:account1'
    assert args[1] == vault.TOKEN_TTL_SECONDS + 60
    stored_data = json.loads(args[2])
    assert stored_data['fb_dtsg'] == 'mock_dtsg'
    assert stored_data['token_hash'] is not None
    assert stored_data['usage_count'] == 0
    
    # Reset mock and get tokens (should come from L1)
    mock_redis.get.reset_mock()
    retrieved = await vault.get('account1')
    
    assert retrieved is not None
    assert retrieved['fb_dtsg'] == 'mock_dtsg'
    # L1 cache should prevent Redis call
    mock_redis.get.assert_not_called()


@pytest.mark.asyncio
async def test_token_vault_get_from_l2(mock_redis):
    """Test getting tokens from L2 cache when L1 is empty."""
    vault = TokenVault(mock_redis)
    tokens = {
        'fb_dtsg': 'redis_dtsg',
        'timestamp': time.time(),
        'usage_count': 0
    }
    
    # Mock Redis returning the token payload
    mock_redis.get.return_value = json.dumps(tokens)
    
    retrieved = await vault.get('account2')
    
    assert retrieved is not None
    assert retrieved['fb_dtsg'] == 'redis_dtsg'
    mock_redis.get.assert_called_once_with('fb_tokens:account2')
    
    # Second get should hit L1 cache
    mock_redis.get.reset_mock()
    retrieved2 = await vault.get('account2')
    assert retrieved2['fb_dtsg'] == 'redis_dtsg'
    mock_redis.get.assert_not_called()


@pytest.mark.asyncio
async def test_token_vault_expiry(mock_redis):
    """Test token TTL expiry."""
    vault = TokenVault(mock_redis)
    
    # Store old tokens in Redis
    old_time = time.time() - vault.TOKEN_TTL_SECONDS - 10
    tokens = {
        'fb_dtsg': 'old_dtsg',
        'timestamp': old_time,
        'usage_count': 0
    }
    mock_redis.get.return_value = json.dumps(tokens)
    
    # Should return None because tokens are expired
    retrieved = await vault.get('account3')
    assert retrieved is None


@pytest.mark.asyncio
async def test_token_vault_usage_counting(mock_redis):
    """Test increment_usage counts correctly in L1 and L2."""
    vault = TokenVault(mock_redis)
    
    # Set initial tokens
    await vault.set('account4', {'fb_dtsg': 'dtsg1'})
    
    # Increment usage
    mock_redis.incr.return_value = 1
    count = await vault.increment_usage('account4')
    
    assert count == 1
    assert vault._local_cache['account4']['usage_count'] == 1
    
    mock_redis.incr.assert_called_once_with('fb_tokens:account4:usage')
    mock_redis.expire.assert_called_once_with('fb_tokens:account4:usage', vault.TOKEN_TTL_SECONDS)


@pytest.mark.asyncio
async def test_token_vault_rotation_needed(mock_redis):
    """Test rotation detection based on age and usage."""
    vault = TokenVault(mock_redis)
    
    # No tokens = rotation needed
    assert await vault.is_rotation_needed('account5') is True
    
    # Fresh tokens = no rotation needed
    await vault.set('account5', {'fb_dtsg': 'dtsg1'})
    assert await vault.is_rotation_needed('account5') is False
    
    # Old tokens (> 300s) = rotation needed
    vault._local_cache['account5']['timestamp'] = time.time() - 301
    assert await vault.is_rotation_needed('account5') is True
    
    # High usage (> 50) = rotation needed
    vault._local_cache['account5']['timestamp'] = time.time()
    mock_redis.get.return_value = '51'  # Return high usage from redis
    assert await vault.is_rotation_needed('account5') is True
