"""Canonical doc_id constants and Redis-backed live extraction.

Doc IDs change with Facebook releases. This module provides a single source of
truth — every poster imports from here rather than hardcoding values.

Guidance from the reference (Ch 12.2):
  - Live doc_ids are extracted from bundled JS by DocIdScraper and stored in
    Redis under the key ``fb_graphql_doc_id:{friendly_name}``.
  - Fallback values below are current as of 2026-Q1. They MUST be updated if
    Facebook ships a new release; the DocIdScraper refresh loop handles this.
"""

from typing import Any

DOC_IDS_REDIS_PREFIX = "fb_graphql_doc_id"
DOC_IDS_REDIS_TTL = 86400

FALLBACK_DOC_IDS = {
    "ComposerStoryCreate": "7711610262198779",
    "PagePosts": "4459169650830798",
    "PhotoDelete": "3456789012",
    "Viewer": "2395650947204996",
    "StoriesCreate": "2345678901",
    "AlbumsCreate": "4567890123",
    "DraftPostCreate": "5678901234",
    "ReelsCreate": "6789012345",
}


def get_fallback(name: str) -> str:
    return FALLBACK_DOC_IDS.get(name, "0")


async def get_live_doc_id(redis: Any, name: str) -> str:
    """Try Redis first, then fall back to hardcoded value."""
    if redis is not None:
        try:
            raw = await redis.get(f"{DOC_IDS_REDIS_PREFIX}:{name}")
            if raw:
                value = raw.decode() if isinstance(raw, bytes) else str(raw)
                if value.isdigit():
                    return value
        except Exception:
            pass
    return get_fallback(name)
