"""Mention parsing for Facebook text posts.

Facebook's GraphQL mutations expect mentions as:
    {"id": "<user_id>", "offset": <int>, "length": <int>}

Where offset and length refer to the character positions in the post text.
This module provides parsing and injection of @mentions.

Supports formats:
  - @username                (bare)
  - @[username]              (bracketed)
  - @[username:user_id]      (bracketed with ID)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class Mention:
    """A single @mention in a post."""
    user_id: str
    username: str
    offset: int
    length: int


# Pattern matches @username, @[username], and @[username:user_id]
MENTION_PATTERN = re.compile(
    r'@(?:\[([^\]]+?)(?::(\d+))?\]|(\w+))'
)


def parse_mentions(text: str, known_users: Optional[Dict[str, str]] = None) -> List[Mention]:
    """Parse @mentions from post text.

    Args:
        text: Post body text containing @mentions
        known_users: Optional dict mapping username → user_id for resolution

    Returns:
        List of Mention dataclasses with offset/length referring to positions
        in the original text (before replacement).

    The returned mentions reference positions in the *original* text.
    When building the GraphQL variables, Facebook expects:
        {"text": "<text with @mentions>", "ranges": [{"id": "...", "offset": N, "length": M}]}
    """
    mentions: List[Mention] = []
    for match in MENTION_PATTERN.finditer(text):
        # Group 1: bracketed username (e.g., "username" from @[username])
        # Group 2: user_id from @[username:user_id]
        # Group 3: bare username (e.g., "username" from @username)
        bracketed_username = match.group(1)
        resolved_id = match.group(2)
        bare_username = match.group(3)

        username = (bracketed_username or bare_username or "").strip()
        if not username:
            continue

        # Resolve user_id
        user_id = resolved_id or ""
        if not user_id and known_users:
            user_id = known_users.get(username, "")

        if not user_id:
            continue

        mentions.append(Mention(
            user_id=user_id,
            username=username,
            offset=match.start(),
            length=match.end() - match.start(),
        ))

    return mentions


def build_mention_ranges(text: str, known_users: Optional[Dict[str, str]] = None) -> List[Dict]:
    """Build the 'ranges' field for a GraphQL post variables dict.

    Returns a list of dicts:
        [{"id": "<user_id>", "offset": N, "length": M}, ...]

    These are inserted into the message.ranges field of the mutation variables.
    """
    mentions = parse_mentions(text, known_users=known_users)
    return [
        {"id": m.user_id, "offset": m.offset, "length": m.length}
        for m in mentions
    ]


def strip_mentions(text: str) -> str:
    """Remove @mention markup, leaving readable text.

    E.g., "Hello @[John Doe:12345]" → "Hello @John Doe"
    """
    def _replace(match: re.Match) -> str:
        bracketed = match.group(1)
        bare = match.group(3)
        username = bracketed or bare or ""
        return f"@{username}"

    return MENTION_PATTERN.sub(_replace, text)
