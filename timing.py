"""Human-paced timing helpers."""

from __future__ import annotations

import math
import random
from typing import List, Tuple


class StochasticTimer:
    @staticmethod
    def think_time(min_ms: int = 800, max_ms: int = 4500, focus_factor: float = 1.0) -> float:
        mean = math.log(max(min_ms, 1) / 1000.0)
        sigma = 0.55 / max(0.2, focus_factor)
        value = random.lognormvariate(mean, sigma)
        return max(min_ms / 1000.0, min(max_ms / 1000.0, value))

    @staticmethod
    def typing_delay(char_count: int, wpm: float = 65.0) -> List[float]:
        base = 60.0 / max(1.0, wpm * 5.0)
        delays = []
        for index in range(max(0, char_count)):
            fatigue = 1.0 + min(0.6, index / 1200.0)
            pause = random.uniform(0.18, 0.9) if random.random() < 0.15 else 0.0
            delays.append(max(0.015, random.gauss(base, base * 0.35) * fatigue + pause))
        return delays

    @staticmethod
    def scroll_pattern(total_height: int) -> List[Tuple[int, float]]:
        height = max(0, total_height)
        if height == 0:
            return []
        phases = [(0.25, 600, 0.25), (0.5, 180, 0.65), (0.25, 520, 0.2)]
        pattern: List[Tuple[int, float]] = []
        for share, step, delay in phases:
            remaining = int(height * share)
            while remaining > 0:
                delta = min(remaining, max(40, int(random.gauss(step, step * 0.25))))
                pattern.append((delta, max(0.05, random.gauss(delay, delay * 0.25))))
                remaining -= delta
        return pattern

    @staticmethod
    def post_interval(base_seconds: float, account_age_days: int) -> float:
        if account_age_days < 30:
            multiplier = 3.0
        elif account_age_days < 90:
            multiplier = 1.5
        else:
            multiplier = 1.0
        jitter = random.uniform(0.75, 1.25)
        return max(60.0, base_seconds * multiplier * jitter)


class ActionRandomizer:
    WORKFLOWS = [
        ['open_page', 'pause', 'open_composer', 'type_caption', 'publish'],
        ['open_page', 'scroll_feed', 'open_composer', 'type_caption', 'publish'],
        ['open_page', 'hover_profile', 'open_composer', 'type_caption', 'pause', 'publish'],
        ['open_page', 'pause', 'scroll_feed', 'open_composer', 'type_caption', 'publish'],
    ]
    NOISE = ['hover_notification', 'click_profile_pic', 'scroll_feed', 'pause']

    @classmethod
    def randomize_post_workflow(cls) -> List[str]:
        return list(random.choice(cls.WORKFLOWS))

    @classmethod
    def add_noise_actions(cls, actions: List[str], noise_probability: float = 0.15) -> List[str]:
        output: List[str] = []
        for action in actions:
            if random.random() < noise_probability:
                output.append(random.choice(cls.NOISE))
            output.append(action)
        return output
