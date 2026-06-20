"""Backoff — Per-error-type retry strategies with exponential backoff + jitter.

Different error types warrant different backoff strategies. This module encodes
the matrix from Chapter 23.2 of the reference:

    Error Type                   Strategy                     Max Backoff  Max Retries
    ───────────────────────────  ───────────────────────────  ───────────  ───────────
    NetworkError                 Exponential 1s × 2^n + jit   30s          3
    HTTP 5xx                     Exponential 5s × 2^n + jit   120s         2
    HTTP 429                     Respect Retry-After          300s         1
    GraphQL transient            Exponential 2s × 2^n + jit   60s          2
    GraphQL permanent            No retry                     —            0
    Rate limit denied            Wait for token bucket        3600s        1
    CheckpointEncountered        No retry — halt worker       —            0
    SilentFailureError           No retry — cooldown          —            0

Uses "full jitter" (uniform between 0 and exponential value) per AWS
Architecture Blog recommendation.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Callable, Optional, Tuple, Type, Union

logger = logging.getLogger(__name__)


class NetworkError(IOError):
    """Transient network-level failure (timeout, connection reset, DNS)."""
    pass


class TransientGraphQLError(Exception):
    """Temporary GraphQL error — safe to retry."""
    pass


class PermanentGraphQLError(Exception):
    """Permanent GraphQL error — do not retry."""
    pass


class SilentFailureError(Exception):
    """Post succeeded (200 OK) but content is not visible — shadow restriction."""
    pass


class RetryBudgetExhausted(Exception):
    """All retry attempts exhausted."""
    pass


BACKOFF_STRATEGIES = {
    "network": {
        "base_delay": 1.0,
        "max_delay": 30.0,
        "max_retries": 3,
        "exponential_base": 2.0,
        "retryable_exceptions": (NetworkError, ConnectionError, TimeoutError, OSError),
    },
    "http_5xx": {
        "base_delay": 5.0,
        "max_delay": 120.0,
        "max_retries": 2,
        "exponential_base": 2.0,
        "retryable_exceptions": (),
        "status_codes": range(500, 600),
    },
    "http_429": {
        "base_delay": 60.0,
        "max_delay": 300.0,
        "max_retries": 1,
        "exponential_base": 1.0,
        "retryable_exceptions": (),
        "status_codes": {429},
    },
    "graphql_transient": {
        "base_delay": 2.0,
        "max_delay": 60.0,
        "max_retries": 2,
        "exponential_base": 2.0,
        "retryable_exceptions": (TransientGraphQLError,),
    },
}


def full_jitter(delay: float) -> float:
    """Full jitter: uniform between 0 and delay (AWS recommended)."""
    return random.uniform(0, delay)


async def retry_with_backoff(
    func: Callable[..., Any],
    *args: Any,
    strategy: str = "network",
    jitter: bool = True,
    **kwargs: Any,
) -> Any:
    """Retry an async function with the specified backoff strategy.

    Args:
        func: Async function to retry
        strategy: One of "network", "http_5xx", "http_429", "graphql_transient"
        jitter: Apply full jitter to delay

    Raises:
        RetryBudgetExhausted: All attempts exhausted
        PermanentGraphQLError: Permanent error, not retried
        CheckpointEncountered: Checkpoint, not retried
        SilentFailureError: Shadow restriction, not retried
    """
    config = BACKOFF_STRATEGIES.get(strategy)
    if not config:
        raise ValueError(f"Unknown backoff strategy: {strategy}")

    max_retries = config["max_retries"]
    base_delay = config["base_delay"]
    max_delay = config["max_delay"]
    exponential_base = config["exponential_base"]
    retryable_exceptions = config.get("retryable_exceptions", ())

    last_exception: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            return await func(*args, **kwargs)
        except (PermanentGraphQLError, SilentFailureError) as e:
            raise
        except Exception as e:
            last_exception = e
            # Check if this is a non-retryable exception type
            if retryable_exceptions and not isinstance(e, retryable_exceptions):
                raise

            if attempt == max_retries:
                raise RetryBudgetExhausted(
                    f"All {max_retries} retries exhausted for {func.__name__}: {e}"
                ) from e

            delay = min(base_delay * (exponential_base ** attempt), max_delay)
            if jitter:
                delay = full_jitter(delay)

            logger.warning(
                "Retry %d/%d for %s after %.1fs: %s",
                attempt + 1, max_retries, func.__name__, delay, e,
            )
            await asyncio.sleep(delay)

    raise RetryBudgetExhausted(f"All retries exhausted (unreachable)")


def classify_http_response(status_code: int, body: str = "") -> str:
    """Classify an HTTP response into a backoff strategy name.

    Returns one of: "network", "http_5xx", "http_429", "graphql_transient",
    "permanent", or "success".
    """
    if 200 <= status_code < 300:
        return "success"
    if status_code == 429:
        return "http_429"
    if 500 <= status_code < 600:
        return "http_5xx"
    if status_code in (401, 403):
        return "permanent"
    if status_code in (400,):
        body_lower = body.lower() if body else ""
        if "checkpoint" in body_lower or "security" in body_lower:
            return "permanent"
        return "graphql_transient"
    return "network"


def extract_retry_after(headers: Optional[dict]) -> Optional[int]:
    """Extract Retry-After header value in seconds."""
    if not headers:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None
