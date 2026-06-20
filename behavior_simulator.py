"""BehaviorSimulator — Synthetic /ajax/bz behavioral telemetry.

Facebook's web client continuously streams behavioral telemetry to /ajax/bz as a
JSON payload of batched events (mouse movements, scrolls, clicks, focus changes,
performance timings). For automation purposes, the system must send synthetic
/ajax/bz requests at the same cadence as a real browser — approximately every
60 seconds.

Without this telemetry, Facebook's server-side behavioral models will flag the
session as non-human within hours, triggering a soft checkpoint.

Event types sent:
  - MOUSE_MOVE (1):     Small random mouse movements
  - SCROLL (4):         Periodic scroll position changes
  - CLICK (2):          Rare click events (pre-action)
  - FOCUS (8):          Focus state changes
  - RESIZE (9):         Viewport resize (rare)
  - PERF_TIMING (20):   Performance timing data

The state machine uses a Markov model favoring small movements over large ones,
matching real user behavior distributions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .fb_client import FBClient

logger = logging.getLogger(__name__)

# Event type constants (from Facebook's internal event enum)
EVT_MOUSE_MOVE = 1
EVT_CLICK = 2
EVT_SCROLL = 4
EVT_FOCUS = 8
EVT_RESIZE = 9
EVT_PERF_TIMING = 20


@dataclass
class BehaviorSimulator:
    """Generates plausible /ajax/bz telemetry batches.

    Maintains internal state for mouse position, scroll position, and focus,
    evolving between batches via a Markov model that favors small incremental
    movements over large jumps.
    """

    screen_w: int = 1920
    screen_h: int = 1080

    mouse_x: int = field(default_factory=lambda: random.randint(400, 800))
    mouse_y: int = field(default_factory=lambda: random.randint(300, 600))
    scroll_y: int = 0
    focused: bool = True
    last_event_at: float = field(default_factory=time.time)
    session_start: float = field(default_factory=time.time)
    batch_count: int = 0

    def _jitter_mouse(self) -> Tuple[int, int]:
        """Move mouse by a small random amount (human-like jitter)."""
        dx = random.choice([-1, 0, 0, 1, 1, 2, -2, 0, 0, 0])
        dy = random.choice([-1, 0, 0, 1, 1, 2, -2, 0, 0, 0])
        new_x = max(0, min(self.screen_w, self.mouse_x + dx))
        new_y = max(0, min(self.screen_h, self.mouse_y + dy))
        self.mouse_x, self.mouse_y = new_x, new_y
        return new_x, new_y

    def _jitter_scroll(self) -> int:
        """Adjust scroll position by a small random amount."""
        dy = random.choice([0, 0, 1, 2, -1, 0, 0, 3, 0, -2, 5, 0, 0, -1])
        self.scroll_y = max(0, self.scroll_y + dy)
        if random.random() < 0.05:
            self.scroll_y = max(0, self.scroll_y - random.randint(10, 50))
        return self.scroll_y

    def generate_batch(self, window_s: float = 60.0) -> List[List]:
        """Generate a batch of events covering the last *window_s* seconds.

        Returns a list of [type, timestamp_offset, count, payload] tuples ready
        to be sent to /ajax/bz.
        """
        now = time.time()
        events: List[List] = []
        ts_start = self.last_event_at
        ts_end = now
        self.last_event_at = now
        self.batch_count += 1

        duration = max(0.1, ts_end - ts_start)

        # Mouse movement events (1-3 per batch)
        num_moves = random.randint(1, 3)
        for _ in range(num_moves):
            x, y = self._jitter_mouse()
            ts = ts_start + random.uniform(0, duration)
            offset = int((ts - self.session_start) * 1000)
            events.append([
                EVT_MOUSE_MOVE,
                offset,
                1,
                [x, y, 0, 0, 0],
            ])

        # Scroll events (0-2 per batch)
        if random.random() < 0.7:
            scroll_pos = self._jitter_scroll()
            ts = ts_start + random.uniform(0, duration)
            offset = int((ts - self.session_start) * 1000)
            scroll_delta = random.choice([0, 1, 2, 3, -1])
            max_pos = max(1000, self.screen_h * 3)
            scroll_pos = max(0, min(max_pos, scroll_pos))
            events.append([
                EVT_SCROLL,
                offset,
                1,
                [scroll_pos, scroll_delta, self.screen_w, self.screen_h],
            ])

        # Focus event (occasional)
        if random.random() < 0.15:
            ts = ts_start + random.uniform(0, duration)
            offset = int((ts - self.session_start) * 1000)
            events.append([
                EVT_FOCUS,
                offset,
                1,
                [1 if self.focused else 0],
            ])

        # Performance timing (occasional)
        if random.random() < 0.2 and self.batch_count % 5 == 0:
            ts = ts_start + random.uniform(0, duration)
            offset = int((ts - self.session_start) * 1000)
            memory = random.randint(100, 500)
            events.append([
                EVT_PERF_TIMING,
                offset,
                1,
                [memory, random.randint(0, 100), random.randint(0, 50)],
            ])

        return events

    def pre_action_burst(self, target_x: Optional[int] = None, target_y: Optional[int] = None) -> List[List]:
        """Generate a burst of events immediately before an action.

        Real browsers send a cluster of telemetry events in the second before
        a mutation (post, comment, like). The pattern is:
          1. 3-5 small mouse moves toward the action target
          2. Brief hover (~200ms)
          3. Click event
          4. Optional focus change

        This burst is flushed via TelemetryFlusher.flush_pre_action() and the
        server correlates it with the subsequent GraphQL mutation.
        """
        now = time.time()
        events: List[List] = []

        if target_x is None:
            target_x = self.screen_w // 2 + random.randint(-100, 100)
        if target_y is None:
            target_y = self.screen_h // 2 + random.randint(-50, 50)

        # 3-5 interpolated mouse moves toward target
        n_moves = random.randint(3, 5)
        for i in range(n_moves):
            t = now - (n_moves - i) * 0.15 + random.uniform(-0.05, 0.05)
            alpha = (i + 1) / n_moves
            x = round(self.mouse_x + (target_x - self.mouse_x) * alpha)
            y = round(self.mouse_y + (target_y - self.mouse_y) * alpha)
            x = max(0, min(self.screen_w, x))
            y = max(0, min(self.screen_h, y))
            offset = int((t - self.session_start) * 1000)
            events.append([
                EVT_MOUSE_MOVE,
                offset,
                1,
                [x, y, 0, 0, 0],
            ])

        self.mouse_x, self.mouse_y = target_x, target_y

        # Hover (200ms gap with no events) then click
        click_t = now - 0.2
        offset = int((click_t - self.session_start) * 1000)
        events.append([
            EVT_CLICK,
            offset,
            1,
            [target_x, target_y, 0, 0, 1],
        ])

        return events


class TelemetryFlusher:
    """Sends synthetic /ajax/bz telemetry events to Facebook on a regular cadence.

    Runs as a background task alongside the heartbeat manager. Sends a batch of
    events every TELEMETRY_INTERVAL seconds, plus a pre-action burst before posts.

    Usage::

        flusher = TelemetryFlusher(fb_client, account_id)
        # Start background loop:
        task = asyncio.create_task(flusher.run_forever())
        # Send pre-action burst before posting:
        await flusher.flush_pre_action()
    """

    TELEMETRY_INTERVAL = 60  # seconds between telemetry batches

    def __init__(
        self,
        client: Any,
        account_id: str,
        simulator: Optional[BehaviorSimulator] = None,
        interval: int = TELEMETRY_INTERVAL,
    ):
        self.client = client
        self.account_id = account_id
        self.simulator = simulator or BehaviorSimulator()
        self.interval = interval
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def run_forever(self):
        """Background loop: send telemetry every *interval* seconds."""
        self._running = True
        logger.info(
            "Telemetry flusher started for account %s (every %ds)",
            self.account_id, self.interval,
        )
        try:
            while self._running:
                await asyncio.sleep(self.interval)
                if not self._running:
                    break
                await self._send_batch()
        except asyncio.CancelledError:
            logger.info("Telemetry flusher cancelled for account %s", self.account_id)
        except Exception as exc:
            logger.error("Telemetry flusher error for account %s: %s", self.account_id, exc)
        finally:
            self._running = False

    async def flush_pre_action(self, target_x: Optional[int] = None, target_y: Optional[int] = None):
        """Send a pre-action telemetry burst immediately before an action.

        Args:
            target_x: Target x-coordinate for mouse move interpolation.
            target_y: Target y-coordinate for mouse move interpolation.
        """
        if not self._running:
            return
        try:
            burst = self.simulator.pre_action_burst(target_x=target_x, target_y=target_y)
            if burst:
                await self._post_to_bz(burst)
                logger.debug(
                    "Pre-action telemetry burst sent for account %s (%d events)",
                    self.account_id, len(burst),
                )
        except Exception as exc:
            logger.debug("Pre-action telemetry burst failed (non-fatal): %s", exc)

    async def _send_batch(self):
        """Generate and send a batch of telemetry events."""
        batch = self.simulator.generate_batch(window_s=float(self.interval))
        if not batch:
            return
        await self._post_to_bz(batch)

    async def _post_to_bz(self, events: List[List]):
        """POST the event batch to /ajax/bz.

        Payload shape per reference (Ch 9.1):
          - Outer list wraps the batch: ``[[evt1, evt2, ...]]``
          - ``ts`` is the current epoch timestamp
          - Sent as form-encoded ``q=<json>``

        Telemetry requests are fire-and-forget — no retry, no error propagation.
        """
        url = "https://www.facebook.com/ajax/bz"
        payload = {
            "q": json.dumps({"q": [events], "ts": int(time.time())}),
            "__user": "0",
            "__a": "1",
            "__req": str(random.choice(["2", "3", "4", "5", "6", "7", "8", "9"])),
            "__dyn": self._generate_dyn_param(),
            "__csr": self._generate_csr_param(),
        }

        try:
            status, body, _ = await self.client.post(
                url,
                data=payload,
                timeout=10.0,
            )
            if status >= 400:
                logger.debug("Telemetry POST returned %d (non-fatal)", status)
        except Exception as exc:
            logger.debug("Telemetry POST failed (non-fatal): %s", exc)

    @staticmethod
    def _generate_dyn_param() -> str:
        """Generate a plausible __dyn parameter (Facebook's JS execution context)."""
        chars = "7ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        length = random.randint(8, 14)
        return "".join(random.choices(chars, k=length))

    @staticmethod
    def _generate_csr_param() -> str:
        """Generate a plausible __csr parameter."""
        return f"{random.randint(100, 999)}:{random.randint(100000, 999999)}"

    def stop(self):
        self._running = False
        if self._task is not None:
            self._task.cancel()
