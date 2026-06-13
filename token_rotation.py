"""Token rotation and freshness management for GraphQL mutations."""

from __future__ import annotations

import logging
import time
from typing import Any, Optional, Tuple

from .utils import maybe_await

logger = logging.getLogger(__name__)


class TokenRotationPolicy:
    """Manages token rotation based on mutations and age."""
    
    # Rotate after this many mutations per token — raised to 50 since the
    # heartbeat refresh keeps tokens much fresher than the old 15-use limit assumed.
    MUTATION_COUNT_LIMIT = 50
    
    # Rotate token if older than this (in seconds) — aligned with TokenVault TTL (1500s)
    # so the rotation policy and vault expiry agree on what "stale" means.
    TOKEN_AGE_LIMIT = 1500  # 25 minutes
    
    # Maximum time a token can be held before rotation (hard limit)
    MAX_TOKEN_LIFETIME = 3600  # 1 hour
    
    # Minimum freshness required before allowing mutation (in seconds)
    MIN_FRESHNESS_REQUIRED = 60  # Token must be < 1 hour old
    
    # IMPORTANT: This key MUST match TokenVault._key() in tokens.py
    # TokenVault stores under 'fb_tokens:{account_id}'
    _TOKEN_REDIS_KEY_PREFIX = 'fb_tokens:'
    
    def __init__(self, redis_client: Any):
        self.redis = redis_client
    
    async def should_rotate_before_mutation(self, account_id: str) -> Tuple[bool, Optional[str]]:
        """
        Check if token should be rotated BEFORE next mutation.
        
        Returns:
            (should_rotate: bool, reason: Optional[str])
        """
        if not self.redis:
            return False, None
        
        try:
            # Get token info — must use the same key prefix as TokenVault._key()
            token_data = await maybe_await(self.redis.get(f'{self._TOKEN_REDIS_KEY_PREFIX}{account_id}'))
            if not token_data:
                return True, 'NO_TOKEN'
            
            # Get mutation count
            usage_key = f'token_mutations:{account_id}'
            mutation_count = int(await maybe_await(self.redis.get(usage_key)) or 0)
            
            # Get token timestamp
            import json
            token_obj = json.loads(token_data.decode() if isinstance(token_data, bytes) else token_data)
            token_timestamp = float(token_obj.get('timestamp', 0))
            
            if token_timestamp == 0:
                return True, 'MISSING_TIMESTAMP'
            
            token_age = time.time() - token_timestamp
            
            # Check mutation count limit
            if mutation_count >= self.MUTATION_COUNT_LIMIT:
                logger.warning(
                    f"Token for {account_id} hit mutation limit ({mutation_count}/{self.MUTATION_COUNT_LIMIT})"
                )
                return True, f'MUTATION_LIMIT_EXCEEDED:{mutation_count}'
            
            # Check token age
            if token_age > self.TOKEN_AGE_LIMIT:
                logger.warning(
                    f"Token for {account_id} is {token_age}s old (limit: {self.TOKEN_AGE_LIMIT}s)"
                )
                return True, f'TOKEN_AGE_EXCEEDED:{int(token_age)}'
            
            # Check hard limit
            if token_age > self.MAX_TOKEN_LIFETIME:
                logger.error(
                    f"Token for {account_id} exceeded hard limit: {token_age}s (max: {self.MAX_TOKEN_LIFETIME}s)"
                )
                return True, f'HARD_LIMIT_EXCEEDED:{int(token_age)}'
            
            return False, None
        
        except Exception as e:
            logger.error(f"Error checking token rotation for {account_id}: {e}")
            return False, None
    
    async def enforce_freshness_before_post(self, account_id: str) -> Tuple[bool, str]:
        """
        Validate token freshness is sufficient for next post.
        
        Returns:
            (is_fresh: bool, status: str)
        """
        should_rotate, reason = await self.should_rotate_before_mutation(account_id)
        if should_rotate:
            return False, f'TOKEN_ROTATION_DUE:{reason}'
        return True, 'TOKEN_FRESH'
    
    async def record_mutation(self, account_id: str) -> None:
        """Record that a mutation was performed (increment counter)."""
        if not self.redis:
            return
        
        try:
            key = f'token_mutations:{account_id}'
            await maybe_await(self.redis.incr(key))
            await maybe_await(self.redis.expire(key, self.TOKEN_AGE_LIMIT))
            logger.debug(f"Recorded mutation for {account_id}")
        except Exception as e:
            logger.warning(f"Error recording mutation for {account_id}: {e}")
    
    async def reset_mutation_count(self, account_id: str) -> None:
        """Reset mutation counter (after token refresh)."""
        if not self.redis:
            return
        
        try:
            key = f'token_mutations:{account_id}'
            await maybe_await(self.redis.delete(key))
            logger.debug(f"Reset mutation counter for {account_id}")
        except Exception as e:
            logger.warning(f"Error resetting mutation counter: {e}")
    
    async def get_token_stats(self, account_id: str) -> dict:
        """Get current token statistics."""
        if not self.redis:
            return {}
        
        try:
            token_data = await maybe_await(self.redis.get(f'{self._TOKEN_REDIS_KEY_PREFIX}{account_id}'))
            if not token_data:
                return {'status': 'NO_TOKEN'}
            
            import json
            token_obj = json.loads(token_data.decode() if isinstance(token_data, bytes) else token_data)
            token_timestamp = float(token_obj.get('timestamp', 0))
            
            mutation_count = int(await maybe_await(self.redis.get(f'token_mutations:{account_id}')) or 0)
            token_age = time.time() - token_timestamp if token_timestamp else 0
            
            return {
                'status': 'OK',
                'token_age_seconds': int(token_age),
                'mutation_count': mutation_count,
                'age_until_rotation': max(0, self.TOKEN_AGE_LIMIT - int(token_age)),
                'mutations_until_rotation': max(0, self.MUTATION_COUNT_LIMIT - mutation_count),
                'should_rotate': token_age > self.TOKEN_AGE_LIMIT or mutation_count >= self.MUTATION_COUNT_LIMIT,
            }
        except Exception as e:
            logger.error(f"Error getting token stats for {account_id}: {e}")
            return {'status': 'ERROR', 'error': str(e)}


class TokenFreshnessValidator:
    """Validates token freshness and enables pre-flight checks."""
    
    def __init__(self, rotation_policy: TokenRotationPolicy):
        self.policy = rotation_policy
    
    async def validate_before_graphql_mutation(
        self,
        account_id: str,
        mutation_type: str = 'post',
    ) -> Tuple[bool, str]:
        """
        Comprehensive pre-flight validation before GraphQL mutation.
        
        Returns:
            (is_valid: bool, message: str)
        """
        # Check token freshness
        is_fresh, msg = await self.policy.enforce_freshness_before_post(account_id)
        if not is_fresh:
            return False, msg
        
        # Additional checks can be added here (token validity, signature, etc.)
        
        return True, 'FRESH_AND_VALID'
    
    async def track_mutation_usage(self, account_id: str) -> None:
        """Track mutation and trigger rotation if needed."""
        await self.policy.record_mutation(account_id)
        
        # Check if rotation is needed
        should_rotate, reason = await self.policy.should_rotate_before_mutation(account_id)
        if should_rotate:
            logger.warning(f"Token rotation triggered for {account_id}: {reason}")
    
    async def get_remaining_mutations(self, account_id: str) -> int:
        """Get number of mutations remaining before rotation."""
        stats = await self.policy.get_token_stats(account_id)
        return stats.get('mutations_until_rotation', 0)
    
    async def get_remaining_time(self, account_id: str) -> int:
        """Get seconds remaining before token age rotation."""
        stats = await self.policy.get_token_stats(account_id)
        return stats.get('age_until_rotation', 0)
