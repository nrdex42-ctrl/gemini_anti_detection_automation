"""WarmupPlanner — 7-day account warmup state machine.

New accounts start at BROWSING level and advance through LIKING, COMMENTING,
TEXT_POST, PHOTO_POST to FULL. Each level gates which actions are permitted
and enforces daily volume caps. Skipping a level (e.g. advancing to TEXT_POST
without ever doing a LIKE) is forbidden.

The full warmup takes 7 days minimum. Accelerating past 1 level per day
triggers a detection risk.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)


class WarmupLevel(str, Enum):
    BROWSING = "browsing"          # Day 1: browse, view profiles
    LIKING = "liking"              # Day 2-3: add likes
    COMMENTING = "commenting"      # Day 3-4: add comments
    TEXT_POST = "text_post"        # Day 4-5: text-only posts
    PHOTO_POST = "photo_post"      # Day 5-6: single photo posts
    FULL = "full"                  # Day 7+: all actions via rate limiter


# Actions permitted at each level (superset: higher levels include lower)
LEVEL_ACTIONS: Dict[WarmupLevel, Set[str]] = {
    WarmupLevel.BROWSING: {"browse", "view_profile"},
    WarmupLevel.LIKING: {"browse", "view_profile", "like"},
    WarmupLevel.COMMENTING: {"browse", "view_profile", "like", "comment"},
    WarmupLevel.TEXT_POST: {"browse", "view_profile", "like", "comment", "post:text"},
    WarmupLevel.PHOTO_POST: {
        "browse", "view_profile", "like", "comment",
        "post:text", "post:single_photo",
    },
    WarmupLevel.FULL: {
        "browse", "view_profile", "like", "comment",
        "post:text", "post:single_photo", "post:multi_photo",
        "post:video", "post:reel", "post:story",
    },
}

# Daily volume caps per level (lower levels = tighter caps)
DAILY_CAPS: Dict[WarmupLevel, Dict[str, int]] = {
    WarmupLevel.BROWSING: {"browse": 20, "view_profile": 5},
    WarmupLevel.LIKING: {"browse": 30, "view_profile": 8, "like": 3},
    WarmupLevel.COMMENTING: {"browse": 30, "view_profile": 8, "like": 5, "comment": 2},
    WarmupLevel.TEXT_POST: {
        "browse": 30, "like": 5, "comment": 2, "post:text": 1,
    },
    WarmupLevel.PHOTO_POST: {
        "browse": 30, "like": 5, "comment": 2,
        "post:text": 2, "post:single_photo": 1,
    },
    WarmupLevel.FULL: {},  # governed by rate limiter
}

# Minimum days before advancing to next level
LEVEL_AGE_REQUIREMENTS = {
    WarmupLevel.BROWSING: 1,
    WarmupLevel.LIKING: 2,
    WarmupLevel.COMMENTING: 3,
    WarmupLevel.TEXT_POST: 4,
    WarmupLevel.PHOTO_POST: 5,
    WarmupLevel.FULL: 7,
}

# Minimum actions required at current level before advancing
LEVEL_PREREQUISITES: Dict[WarmupLevel, Dict[str, int]] = {
    WarmupLevel.BROWSING: {"browse": 5},
    WarmupLevel.LIKING: {"like": 2},
    WarmupLevel.COMMENTING: {"comment": 1},
    WarmupLevel.TEXT_POST: {"post:text": 1},
    WarmupLevel.PHOTO_POST: {"post:single_photo": 1},
    WarmupLevel.FULL: {},
}

LEVEL_ORDER = [
    WarmupLevel.BROWSING,
    WarmupLevel.LIKING,
    WarmupLevel.COMMENTING,
    WarmupLevel.TEXT_POST,
    WarmupLevel.PHOTO_POST,
    WarmupLevel.FULL,
]


@dataclass
class WarmupState:
    """Mutable state for a single account's warmup progress."""
    level: WarmupLevel = WarmupLevel.BROWSING
    started_at: float = field(default_factory=time.time)
    actions_today: Dict[str, int] = field(default_factory=dict)
    total_actions: Dict[str, int] = field(default_factory=dict)
    last_action_at: float = 0.0
    last_level_advance: float = field(default_factory=time.time)

    @property
    def age_days(self) -> float:
        return (time.time() - self.started_at) / 86400.0

    def record_action(self, action: str):
        self.actions_today[action] = self.actions_today.get(action, 0) + 1
        self.total_actions[action] = self.total_actions.get(action, 0) + 1
        self.last_action_at = time.time()

    def reset_daily(self):
        self.actions_today.clear()


class WarmupPlanner:
    """Per-account warming state machine.

    Usage::

        planner = WarmupPlanner()
        state = WarmupState(level=WarmupLevel.BROWSING)

        if planner.can_perform(state, "post:text"):
            # allowed at this level
            state.record_action("post:text")

        new_level = planner.advance(state)
        if new_level != state.level:
            state.level = new_level
    """

    @staticmethod
    def can_perform(state: WarmupState, action: str) -> bool:
        """Check if *action* is allowed at the current level.

        Returns False if:
          - Action is not permitted at this level
          - Daily cap for this action has been reached
        """
        allowed = LEVEL_ACTIONS.get(state.level, set())
        if action not in allowed:
            return False

        caps = DAILY_CAPS.get(state.level)
        if caps is None or state.level == WarmupLevel.FULL:
            return True

        today_count = state.actions_today.get(action, 0)
        cap = caps.get(action)
        if cap is not None and today_count >= cap:
            return False

        return True

    @staticmethod
    def advance(state: WarmupState) -> WarmupLevel:
        """Compute the next warmup level based on age and prior activity.

        Returns the new level (may be same as current if prerequisites are not met).
        """
        age = state.age_days
        current_idx = LEVEL_ORDER.index(state.level)

        # Cannot advance past FULL
        if state.level == WarmupLevel.FULL:
            return WarmupLevel.FULL

        # Check if enough time has passed
        next_level = LEVEL_ORDER[current_idx + 1]
        min_age = LEVEL_AGE_REQUIREMENTS.get(next_level, 7)
        if age < min_age:
            return state.level

        # Check prerequisites at current level
        prereqs = LEVEL_PREREQUISITES.get(state.level, {})
        for action, required_count in prereqs.items():
            if state.total_actions.get(action, 0) < required_count:
                logger.info(
                    "Warmup advance blocked for level %s: "
                    "need %d '%s' actions, have %d",
                    state.level.value, required_count, action,
                    state.total_actions.get(action, 0),
                )
                return state.level

        # Advance
        logger.info(
            "Warmup advanced: %s -> %s (age=%.1fd)",
            state.level.value, next_level.value, age,
        )
        state.last_level_advance = time.time()
        return next_level

    @staticmethod
    def get_suggested_actions(state: WarmupState) -> list:
        """Return a list of suggested actions for the current warmup level."""
        allowed = LEVEL_ACTIONS.get(state.level, set())
        caps = DAILY_CAPS.get(state.level, {})
        suggestions = []
        for action in sorted(allowed):
            today = state.actions_today.get(action, 0)
            cap = caps.get(action)
            if cap is None:
                suggestions.append(f"{action} (unlimited)")
            elif today < cap:
                remaining = cap - today
                suggestions.append(f"{action} ({remaining}/{cap} remaining)")
            else:
                suggestions.append(f"{action} (capped for today)")
        return suggestions
