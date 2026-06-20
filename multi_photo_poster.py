"""MultiPhotoPoster — Multi-image post with parallel uploads and rollback.

Uploads N photos with bounded concurrency (max 3 simultaneous), then publishes
as a single ComposerStoryCreate. On partial failure, already-uploaded photo_ids
are deleted via PhotoDelete to avoid orphan accumulation against the account's
photo quota.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MultiPhotoError(Exception):
    pass


class SilentFailureError(Exception):
    pass


class MultiPhotoPoster:
    """Multi-image post: N parallel uploads + single publish.

    Concurrency: 3 simultaneous uploads per account (FB limit).
    Rollback: PhotoDelete any orphans on partial failure.
    """

    MAX_CONCURRENT_UPLOADS = 3
    MAX_PHOTOS_PER_POST = 10

    def __init__(self, client: Any, photo_uploader: Any):
        self.client = client
        self.photo_uploader = photo_uploader

    async def post(
        self,
        images: List[bytes],
        caption: str = "",
        mentions: Optional[List[Dict]] = None,
        scheduled_publish_time: Optional[int] = None,
        page_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload N photos in parallel, then publish as single post."""
        if len(images) > self.MAX_PHOTOS_PER_POST:
            raise ValueError(f"Too many photos: {len(images)} > {self.MAX_PHOTOS_PER_POST}")

        sem = asyncio.Semaphore(self.MAX_CONCURRENT_UPLOADS)
        successful_photo_ids: List[str] = []
        failures: List[tuple] = []

        async def upload_one(idx: int, image_bytes: bytes):
            async with sem:
                try:
                    await asyncio.sleep(idx * 0.3)
                    photo_id = await self.photo_uploader._upload(image_bytes)
                    successful_photo_ids.append(photo_id)
                    return photo_id
                except Exception as e:
                    failures.append((idx, e))
                    logger.warning("Photo upload %d failed: %s", idx, e)
                    return None

        tasks = [upload_one(i, img) for i, img in enumerate(images)]
        await asyncio.gather(*tasks)

        if failures and not successful_photo_ids:
            raise MultiPhotoError(f"All {len(images)} uploads failed: {failures}")

        if failures:
            logger.warning(
                "Partial photo failure: %d success, %d failed — rolling back",
                len(successful_photo_ids), len(failures),
            )
            await self._rollback(successful_photo_ids)
            raise MultiPhotoError(f"{len(failures)}/{len(images)} uploads failed: {failures}")

        return await self._publish_multi(
            photo_ids=successful_photo_ids,
            caption=caption,
            mentions=mentions or [],
            scheduled_publish_time=scheduled_publish_time,
            page_id=page_id,
        )

    async def _rollback(self, photo_ids: List[str]):
        """Best-effort delete of orphaned photo_ids via PhotoDelete mutation."""
        for pid in photo_ids:
            try:
                variables = {
                    "input": {
                        "photo_id": pid,
                        "actor_id": self._actor_id(),
                    },
                }
                from .doc_ids import get_fallback as _gf
                doc_ids = getattr(self.client, "doc_ids", None) or {}
                doc_id = doc_ids.get("PhotoDelete", _gf("PhotoDelete"))
                await self._graphql(doc_id=doc_id, variables=variables, friendly_name="PhotoDelete")
            except Exception:
                logger.warning("PhotoDelete failed for orphan %s", pid, exc_info=True)

    async def _publish_multi(
        self,
        photo_ids: List[str],
        caption: str,
        mentions: Optional[List[Dict]] = None,
        scheduled_publish_time: Optional[int] = None,
        page_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """ComposerStoryCreate with N photo attachments."""
        composer_session_id = str(uuid.uuid4())
        actor_id = self._actor_id(page_id)

        ranges = []
        if mentions:
            for m in mentions:
                ranges.append({
                    "entity": {"id": m["id"], "text": caption[m["offset"]:m["offset"] + m["length"]]},
                    "offset": m["offset"],
                    "length": m["length"],
                })

        variables = {
            "input": {
                "composer_session_id": composer_session_id,
                "source_type": "COMPOSE",
                "publish_type": "SCHEDULED" if scheduled_publish_time else "PUBLISHED",
                "actor_id": actor_id,
                "message": {"text": caption, "ranges": ranges},
                "attachments": [
                    {"photo": {"id": pid, "uncomposed_photo_id": pid}}
                    for pid in photo_ids
                ],
                "explicit_place": None,
                "text_format_preset_id": None,
                "backdated_time": None,
                "scheduled_publish_time": scheduled_publish_time,
            },
            "displayCommentsFeedbackSource": None,
        }

        from .doc_ids import get_fallback as _gf
        doc_ids = getattr(self.client, "doc_ids", None) or {}
        doc_id = doc_ids.get("ComposerStoryCreate", _gf("ComposerStoryCreate"))

        result = await self._graphql(doc_id=doc_id, variables=variables, friendly_name="ComposerStoryCreate")

        post_id = self._extract_post_id(result)
        if not post_id:
            raise MultiPhotoError(f"No post_id in response: {result}")

        if not scheduled_publish_time:
            verified = await self._verify_post_succeeded(post_id, timeout_s=60)
            if not verified:
                raise SilentFailureError(f"Multi-photo post {post_id} not visible")

        return {
            "post_id": post_id,
            "photo_ids": photo_ids,
            "url": f"https://www.facebook.com/{actor_id}/posts/{post_id}",
            "published_at": time.time(),
        }

    def _actor_id(self, page_id: Optional[str] = None) -> str:
        jar = getattr(self.client, "jar", None)
        if page_id:
            return page_id
        if jar:
            return jar.c_user
        return "0"

    async def _graphql(self, doc_id: str, variables: dict, friendly_name: str = "") -> dict:
        jar = getattr(self.client, "jar", None)
        dtsg = getattr(jar, "dtsg_token", None) or ""
        lsd = getattr(jar, "lsd", None) or ""
        user_id = getattr(jar, "c_user", None) or "0"

        body = {
            "av": user_id,
            "__user": user_id,
            "__a": "1",
            "__req": "h",
            "__comet_req": "15",
            "fb_dtsg": dtsg,
            "lsd": lsd,
            "jazoest": "0",
            "doc_id": doc_id,
            "variables": json.dumps(variables),
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": friendly_name,
            "server_timestamps": "true",
        }

        status, body_text, _ = await self.client.post(
            "https://www.facebook.com/api/graphql/",
            data=body,
            timeout=15,
        )
        if status != 200:
            raise MultiPhotoError(f"GraphQL HTTP {status}: {body_text[:200]}")
        try:
            return json.loads(body_text) if isinstance(body_text, str) else body_text
        except (json.JSONDecodeError, TypeError):
            raise MultiPhotoError(f"Non-JSON GraphQL response: {body_text[:200]}")

    def _extract_post_id(self, result: dict) -> Optional[str]:
        try:
            return result["data"]["composer_post"]["short_code"]
        except (KeyError, TypeError):
            pass
        try:
            return result["data"]["story_create"]["story"]["legacy_story_id"]
        except (KeyError, TypeError):
            pass
        try:
            return result["data"]["composerstorycreate"]["story"]["id"]
        except (KeyError, TypeError):
            pass
        return None

    async def _verify_post_succeeded(self, post_id: str, timeout_s: int = 30) -> bool:
        deadline = time.time() + timeout_s
        jar = getattr(self.client, "jar", None)
        while time.time() < deadline:
            try:
                variables = {
                    "pageID": getattr(jar, "c_user", "0"),
                    "after": None,
                    "first": 5,
                }
                from .doc_ids import get_fallback as _gf
                doc_id = _gf("PagePosts")
                body_data = {
                    "av": getattr(jar, "c_user", "0"),
                    "__user": getattr(jar, "c_user", "0"),
                    "__a": "1",
                    "__req": "g",
                    "__comet_req": "15",
                    "fb_dtsg": getattr(jar, "dtsg_token", ""),
                    "lsd": getattr(jar, "lsd", ""),
                    "doc_id": doc_id,
                    "variables": json.dumps(variables),
                    "fb_api_caller_class": "RelayModern",
                    "fb_api_req_friendly_name": "PagePosts",
                    "server_timestamps": "true",
                }
                status, body, _ = await self.client.post(
                    "https://www.facebook.com/api/graphql/",
                    data=body_data,
                    timeout=8,
                )
                if status >= 400:
                    await asyncio.sleep(3)
                    continue
                data = json.loads(body) if isinstance(body, str) else body
                post_ids = [
                    n.get("node", {}).get("id", "")
                    for n in (data or {})
                    .get("data", {}).get("page", {}).get("timeline", {}).get("edges", [])
                ]
                if post_id in post_ids:
                    return True
                await asyncio.sleep(3)
            except Exception:
                await asyncio.sleep(3)
        return False
