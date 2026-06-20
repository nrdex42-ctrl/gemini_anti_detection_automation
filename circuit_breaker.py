"""CircuitBreaker — Per-account failure rate tracking with open/half-open/closed states.

The breaker tracks the last N request outcomes per account. When the failure rate
exceeds a threshold, the breaker trips (opens) and all subsequent requests are
rejected without hitting Facebook. After a cooldown period, it transitions to
half-open where a single probe request is allowed.

State transitions:
  CLOSED + failure_rate > threshold in last window → OPEN
  OPEN + cooldown elapsed → HALF_OPEN
  HALF_OPEN + 1 success → CLOSED
  HALF_OPEN + 1 failure → OPEN (timer resets)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class BreakerState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Per-account circuit breaker.

    Thread-safe via asyncio.Lock. Intended to be used as::

        if not await breaker.before_request():
            raise CircuitBreakerOpen("account 123 is in circuit breaker open state")
        try:
            result = await do_request()
            await breaker.after_request(success=True)
        except Exception:
            await breaker.after_request(success=False)
            raise
    """

    account_id: str
    window_size: int = 10
    failure_threshold: float = 0.5  # 50% failure opens the breaker
    cooldown_seconds: int = 1800   # 30 minutes

    state: BreakerState = BreakerState.CLOSED
    recent_results: deque = field(default_factory=lambda: deque(maxlen=10))
    opened_at: float = 0.0
    half_open_in_flight: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def before_request(self) -> bool:
        """Check if request is allowed. Returns True if allowed."""
        async with self._lock:
            if self.state == BreakerState.OPEN:
                if time.time() - self.opened_at > self.cooldown_seconds:
                    self.state = BreakerState.HALF_OPEN
                    self.half_open_in_flight = True
                    logger.info(
                        "Breaker half-open for account %s (cooldown elapsed)",
                        self.account_id,
                    )
                    return True
                return False

            if self.state == BreakerState.HALF_OPEN:
                if self.half_open_in_flight:
                    return False
                self.half_open_in_flight = True
                return True

            return True

    async def after_request(self, success: bool):
        """Record request outcome and transition state if needed."""
        async with self._lock:
            if self.state == BreakerState.HALF_OPEN:
                self.half_open_in_flight = False
                if success:
                    self.state = BreakerState.CLOSED
                    self.recent_results.clear()
                    logger.info(
                        "Breaker closed for account %s (probe succeeded)",
                        self.account_id,
                    )
                else:
                    self.state = BreakerState.OPEN
                    self.opened_at = time.time()
                    logger.warning(
                        "Breaker re-opened for account %s (probe failed)",
                        self.account_id,
                    )
                return

            self.recent_results.append(success)
            if len(self.recent_results) >= self.window_size:
                failures = sum(1 for s in self.recent_results if not s)
                rate = failures / len(self.recent_results)
                if rate > self.failure_threshold:
                    self.state = BreakerState.OPEN
                    self.opened_at = time.time()
                    logger.warning(
                        "Breaker opened for account %s "
                        "(failure_rate=%.0f%%, window=%d)",
                        self.account_id,
                        rate * 100,
                        len(self.recent_results),
                    )

    async def reset(self):
        """Force-reset the breaker to closed state."""
        async with self._lock:
            self.state = BreakerState.CLOSED
            self.recent_results.clear()
            self.opened_at = 0.0
            self.half_open_in_flight = False

    @property
    def failure_rate(self) -> float:
        if not self.recent_results:
            return 0.0
        return sum(1 for s in self.recent_results if not s) / len(self.recent_results)

    def to_dict(self) -> Dict[str, object]:
        return {
            "account_id": self.account_id,
            "state": self.state.value,
            "window_size": self.window_size,
            "failure_threshold": self.failure_threshold,
            "cooldown_seconds": self.cooldown_seconds,
            "current_failure_rate": self.failure_rate,
            "observations": len(self.recent_results),
            "opened_at": self.opened_at,
            "half_open_in_flight": self.half_open_in_flight,
        }


class CircuitBreakerOpen(Exception):
    """Raised when a request is rejected by an open circuit breaker."""
    pass
