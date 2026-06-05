"""
StochasticTimer - Human-like timing patterns (advanced implementation).

Replaces fixed ``asyncio.sleep()`` calls with randomized delays
that mimic human behavioral patterns: log-normal distributions,
activity bursts, pauses that correlate with reading time, etc.

This extends the basic StochasticTimer already present in timing.py
with async support, Bezier mouse curves, and richer behavioral profiles.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


@dataclass
class TimingProfile:
    """
    A behavioral timing profile that defines how a "human" types,
    reads, and interacts with a page.
    """
    # Typing speed (characters per second)
    typing_speed_min: float = 4.0
    typing_speed_max: float = 12.0

    # Mouse movement speed (pixels per millisecond)
    mouse_speed_min: float = 0.3
    mouse_speed_max: float = 1.5

    # Reading speed (milliseconds per character)
    reading_speed_ms_per_char: float = 15.0
    reading_speed_variance: float = 5.0

    # Pause patterns
    pause_after_click_ms: Tuple[float, float] = (200, 800)
    pause_between_keystrokes_ms: Tuple[float, float] = (50, 150)
    pause_before_action_ms: Tuple[float, float] = (100, 500)
    pause_after_scroll_ms: Tuple[float, float] = (300, 1200)

    # Activity burst patterns (humans do things in bursts)
    burst_probability: float = 0.3       # 30% chance of a burst
    burst_speed_multiplier: float = 2.0   # 2x faster during bursts
    burst_duration_ms: Tuple[float, float] = (500, 2000)

    # Distraction probability
    distraction_probability: float = 0.05  # 5% chance of a long pause
    distraction_duration_ms: Tuple[float, float] = (2000, 8000)

    # Correlation: longer text → longer pause before next action
    reading_pause_correlation: float = 0.3


@dataclass
class BezierCurve:
    """Cubic Bezier curve for natural mouse movement."""
    p0: Tuple[float, float]
    p1: Tuple[float, float]
    p2: Tuple[float, float]
    p3: Tuple[float, float]

    @classmethod
    def natural_move(
        cls,
        start: Tuple[float, float],
        end: Tuple[float, float],
    ) -> "BezierCurve":
        """Generate a natural-looking Bezier curve between two points."""
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        distance = math.sqrt(dx * dx + dy * dy)

        # Control points create a slight curve
        offset = distance * 0.2
        angle = math.atan2(dy, dx)

        cp1 = (
            start[0] + dx * 0.3 + math.cos(angle + 0.5) * offset,
            start[1] + dy * 0.3 + math.sin(angle + 0.5) * offset,
        )
        cp2 = (
            start[0] + dx * 0.7 + math.cos(angle - 0.3) * offset,
            start[1] + dy * 0.7 + math.sin(angle - 0.3) * offset,
        )

        return cls(start, cp1, cp2, end)

    def evaluate(self, t: float) -> Tuple[float, float]:
        """Evaluate the curve at parameter t ∈ [0, 1]."""
        u = 1 - t
        x = (
            u ** 3 * self.p0[0]
            + 3 * u ** 2 * t * self.p1[0]
            + 3 * u * t ** 2 * self.p2[0]
            + t ** 3 * self.p3[0]
        )
        y = (
            u ** 3 * self.p0[1]
            + 3 * u ** 2 * t * self.p1[1]
            + 3 * u * t ** 2 * self.p2[1]
            + t ** 3 * self.p3[1]
        )
        return (x, y)

    def sample_points(self, num_points: int = 20) -> List[Tuple[float, float]]:
        """Sample points along the curve with non-uniform spacing."""
        # Use ease-in-out timing for natural deceleration
        points = []
        for i in range(num_points + 1):
            t = i / num_points
            # Ease-in-out cubic
            if t < 0.5:
                t_ease = 4 * t * t * t
            else:
                t_ease = 1 - (-2 * t + 2) ** 3 / 2
            points.append(self.evaluate(t_ease))
        return points


class AdvancedStochasticTimer:
    """
    Replaces fixed sleeps with human-like timing.

    Instead of:
        await asyncio.sleep(2.0)

    Use:
        await timer.pause_after_click()
        await timer.type_text(text)  # Types with variable speed
        await timer.human_delay(1.0, 3.0)  # Random in range

    Named AdvancedStochasticTimer to avoid collision with the existing
    StochasticTimer in timing.py.
    """

    def __init__(
        self,
        profile: Optional[TimingProfile] = None,
        seed: Optional[int] = None,
    ):
        self.profile = profile or TimingProfile()
        self._rng = random.Random(seed)
        if HAS_NUMPY:
            self._np_rng = np.random.RandomState(seed)
        else:
            self._np_rng = None
        self._in_burst = False
        self._burst_end_time = 0.0
        self._last_action_text_length = 0
        self._session_start = time.monotonic()

    @staticmethod
    def think_time(
        min_ms: float = 800,
        max_ms: float = 4500,
        focus_factor: float = 1.0,
    ) -> float:
        """Compatibility helper returning a human-like thinking delay in seconds."""
        min_seconds = max(min_ms, 1.0) / 1000.0
        max_seconds = max(max_ms, min_ms) / 1000.0
        sigma = 0.55 / max(0.2, focus_factor)
        value = random.lognormvariate(
            mu=math.log(max(0.001, min_seconds)),
            sigma=sigma,
        )
        return max(min_seconds, min(max_seconds, value))

    def _log_normal(
        self,
        mean: float,
        sigma: float,
        floor: float = 0.0,
        ceiling: Optional[float] = None,
    ) -> float:
        """Sample from a log-normal distribution, clamped to bounds."""
        if self._np_rng is not None:
            value = self._np_rng.lognormal(
                mean=math.log(max(0.001, mean)),
                sigma=sigma,
            )
        else:
            # Fallback to stdlib lognormvariate
            value = self._rng.lognormvariate(
                mu=math.log(max(0.001, mean)),
                sigma=sigma,
            )
        value = max(floor, value)
        if ceiling is not None:
            value = min(ceiling, value)
        return value

    def _check_burst(self) -> bool:
        """Check if we're in an activity burst."""
        now = time.monotonic()
        if self._in_burst and now > self._burst_end_time:
            self._in_burst = False
        if not self._in_burst and self._rng.random() < self.profile.burst_probability:
            self._in_burst = True
            duration = self._rng.uniform(*self.profile.burst_duration_ms) / 1000
            self._burst_end_time = now + duration
        return self._in_burst

    def _check_distraction(self) -> float:
        """Check for a random long pause (human distraction)."""
        if self._rng.random() < self.profile.distraction_probability:
            duration = self._rng.uniform(*self.profile.distraction_duration_ms) / 1000
            return duration
        return 0.0

    def _speed_multiplier(self) -> float:
        """Current speed multiplier based on burst state."""
        if self._check_burst():
            return self.profile.burst_speed_multiplier
        return 1.0

    async def human_delay(
        self,
        min_seconds: float,
        max_seconds: float,
        label: str = "",
    ) -> float:
        """
        A random delay between min and max seconds,
        with log-normal distribution for natural feel.
        """
        mean = (min_seconds + max_seconds) / 2
        sigma = (max_seconds - min_seconds) / 6  # ~99.7% within range

        delay = self._log_normal(mean, sigma, floor=min_seconds, ceiling=max_seconds)

        # Add distraction pause
        distraction = self._check_distraction()
        delay += distraction

        # Adjust for burst speed
        delay /= self._speed_multiplier()

        if delay > 0:
            await asyncio.sleep(delay)

        return delay

    async def pause_after_click(self) -> float:
        """Pause after clicking something (short, natural)."""
        return await self.human_delay(
            self.profile.pause_after_click_ms[0] / 1000,
            self.profile.pause_after_click_ms[1] / 1000,
            "click",
        )

    async def pause_before_action(self) -> float:
        """Pause before taking an action (reading/thinking time)."""
        # Longer pause if last action involved reading a lot of text
        reading_bonus = (
            self._last_action_text_length
            * self.profile.reading_pause_correlation
            * self.profile.reading_speed_ms_per_char
            / 1000
        )
        base_min = self.profile.pause_before_action_ms[0] / 1000
        base_max = self.profile.pause_before_action_ms[1] / 1000
        return await self.human_delay(
            base_min + reading_bonus,
            base_max + reading_bonus,
            "before_action",
        )

    async def pause_after_scroll(self) -> float:
        """Pause after scrolling (reading time)."""
        return await self.human_delay(
            self.profile.pause_after_scroll_ms[0] / 1000,
            self.profile.pause_after_scroll_ms[1] / 1000,
            "scroll",
        )

    async def type_text(
        self,
        element: Any,
        text: str,
        *,
        clear_first: bool = False,
    ) -> float:
        """
        Type text character by character with human-like timing.

        Returns total typing time.
        """
        if clear_first:
            await element.fill("")
            await self.human_delay(0.1, 0.3, "clear")

        total_time = 0.0
        current_text = ""

        for i, char in enumerate(text):
            # Variable inter-keystroke interval
            min_delay = self.profile.pause_between_keystrokes_ms[0] / 1000
            max_delay = self.profile.pause_between_keystrokes_ms[1] / 1000

            # Occasionally pause longer (thinking, reading)
            if self._rng.random() < 0.02:  # 2% chance per character
                max_delay *= 3  # Triple the max pause

            # Speed burst for fast typists
            delay = await self.human_delay(min_delay, max_delay, "keystroke")
            total_time += delay

            current_text += char
            try:
                await element.fill(current_text)
            except Exception:
                # Fallback: type the character directly
                try:
                    await element.type(char, delay=0)
                except Exception:
                    pass

        self._last_action_text_length = len(text)
        return total_time

    def type_text_time(self, text: str, profile: Optional[TimingProfile] = None) -> float:
        """Estimate the time needed to type text with human-like pauses."""
        active_profile = profile or self.profile
        text = str(text or "")
        if not text:
            return 0.0

        avg_chars_per_second = max(
            0.1,
            (active_profile.typing_speed_min + active_profile.typing_speed_max) / 2.0,
        )
        base_delay = 1.0 / avg_chars_per_second
        total_time = 0.0
        length = len(text)

        for index, char in enumerate(text):
            delay = max(0.01, self._rng.gauss(base_delay, base_delay * 0.35))
            fatigue = 1.0 + min(0.6, (index / max(1.0, length)) * 0.15)
            delay *= fatigue

            if char.isspace() or char in ',.;:!?':
                delay += self._rng.uniform(*active_profile.pause_between_keystrokes_ms) / 1000.0
            elif self._rng.random() < 0.15:
                delay += self._rng.uniform(*active_profile.pause_between_keystrokes_ms) / 1000.0

            total_time += delay

        return total_time

    async def mouse_move(
        self,
        page: Any,
        start: Tuple[float, float],
        end: Tuple[float, float],
    ) -> float:
        """
        Move the mouse along a natural Bezier curve.
        """
        curve = BezierCurve.natural_move(start, end)
        points = curve.sample_points(num_points=20)

        # Calculate total distance and time
        total_distance = 0
        for i in range(1, len(points)):
            dx = points[i][0] - points[i - 1][0]
            dy = points[i][1] - points[i - 1][1]
            total_distance += math.sqrt(dx * dx + dy * dy)

        speed = self._rng.uniform(
            self.profile.mouse_speed_min,
            self.profile.mouse_speed_max,
        )
        total_time = total_distance / speed
        time_per_step = total_time / len(points)

        for point in points:
            await page.mouse.move(point[0], point[1])
            # Add slight jitter to timing
            jitter = self._rng.gauss(0, time_per_step * 0.2)
            await asyncio.sleep(max(0, time_per_step + jitter))

        return total_time / 1000

    async def reading_pause(self, text_length: int) -> float:
        """
        Pause as if reading text of the given length.
        """
        base_time = text_length * self.profile.reading_speed_ms_per_char / 1000
        variance = math.sqrt(text_length) * self.profile.reading_speed_variance / 1000
        return await self.human_delay(
            max(0.3, base_time - variance),
            base_time + variance,
            "reading",
        )

    async def between_posts(self, post_index: int, total_posts: int) -> float:
        """
        Natural delay between posting to different pages.
        Increases slightly for later posts (fatigue effect).
        """
        # Base delay: 3-8 seconds
        base_min = 3.0
        base_max = 8.0

        # Fatigue factor: later posts have slightly longer pauses
        if total_posts > 1:
            progress = post_index / (total_posts - 1)
            fatigue = progress * 2.0  # Up to 2 extra seconds
            base_max += fatigue

        # First post might be faster (excitement)
        if post_index == 0:
            base_max *= 0.7

        return await self.human_delay(base_min, base_max, "between_posts")

    def get_session_duration(self) -> float:
        """How long this timer has been active."""
        return time.monotonic() - self._session_start


# Compatibility presets used by older demo scripts.
TimingProfile.BALANCED = TimingProfile()
TimingProfile.CAUTIOUS = TimingProfile(
    typing_speed_min=3.0,
    typing_speed_max=7.0,
    mouse_speed_min=0.2,
    mouse_speed_max=1.0,
    reading_speed_ms_per_char=18.0,
    reading_speed_variance=6.0,
    pause_after_click_ms=(300, 900),
    pause_between_keystrokes_ms=(80, 180),
    pause_before_action_ms=(250, 900),
    pause_after_scroll_ms=(500, 1500),
    burst_probability=0.2,
    burst_speed_multiplier=1.4,
    burst_duration_ms=(500, 1500),
    distraction_probability=0.08,
    distraction_duration_ms=(2500, 9000),
    reading_pause_correlation=0.4,
)
TimingProfile.FAST = TimingProfile(
    typing_speed_min=6.0,
    typing_speed_max=14.0,
    mouse_speed_min=0.4,
    mouse_speed_max=1.8,
    reading_speed_ms_per_char=12.0,
    reading_speed_variance=4.0,
    pause_after_click_ms=(120, 450),
    pause_between_keystrokes_ms=(30, 100),
    pause_before_action_ms=(80, 300),
    pause_after_scroll_ms=(150, 700),
    burst_probability=0.45,
    burst_speed_multiplier=2.3,
    burst_duration_ms=(400, 1200),
    distraction_probability=0.03,
    distraction_duration_ms=(1500, 5000),
    reading_pause_correlation=0.2,
)
