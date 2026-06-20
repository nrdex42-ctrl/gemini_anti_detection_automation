"""PhotoUploader — Single-image upload + publish via ComposerStoryCreate.

Two-phase flow:
  1. Upload JPEG to rupload.facebook.com/ajax/photo/async/upload/
  2. Publish via ComposerStoryCreate GraphQL mutation with resulting photo_id

Pre-processing (automatic):
  - EXIF strip (privacy + fingerprint removal)
  - JPEG recompression at quality 85
  - Dimension capping at 2048px
  - Perceptual hash dedup (avoids re-uploading identical photos)
"""

from __future__ import annotations

import io
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    from PIL import Image, ImageOps
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import imagehash
    HAS_IMAGEHASH = True
except ImportError:
    HAS_IMAGEHASH = False


class PhotoUploadError(Exception):
    pass


class SilentFailureError(Exception):
    pass


class PhotoUploader:
    """Single-image upload + publish.

    Pre-processing:
      1. EXIF strip (privacy + fingerprint)
      2. JPEG recompress at q=85
      3. Compute pHash for dedup

    Upload:
      4. POST multipart to rupload.facebook.com/ajax/photo/async/upload/
      5. Extract photo_id from JSON response

    Publish:
      6. Call ComposerStoryCreate with photo attachment
    """

    JPEG_QUALITY = 85
    MAX_DIMENSION = 2048

    def __init__(self, client: Any, dedup_cache: Optional[Any] = None):
        self.client = client
        self.dedup_cache = dedup_cache

    async def upload_and_publish(
        self,
        image_bytes: bytes,
        caption: str = "",
        mentions: Optional[List[Dict]] = None,
        scheduled_publish_time: Optional[int] = None,
        page_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Full flow: preprocess -> upload -> publish."""
        processed = self._preprocess(image_bytes)

        phash = None
        cache_key = None
        if HAS_IMAGEHASH and self.dedup_cache is not None:
            phash = imagehash.phash(Image.open(io.BytesIO(processed)))
            cache_key = f"photo:{self._actor_id(page_id)}:{phash}"
            cached_photo_id = await self._dedup_get(cache_key)
            if cached_photo_id:
                logger.info(
                    "Photo dedup hit for account %s: %s",
                    self._actor_id(page_id), str(phash),
                )
                return await self._publish_with_photo(
                    photo_id=cached_photo_id,
                    caption=caption,
                    mentions=mentions or [],
                    scheduled_publish_time=scheduled_publish_time,
                    page_id=page_id,
                )

        photo_id = await self._upload(processed)
        if cache_key and self.dedup_cache is not None:
            await self._dedup_set(cache_key, 86400, photo_id)

        return await self._publish_with_photo(
            photo_id=photo_id,
            caption=caption,
            mentions=mentions or [],
            scheduled_publish_time=scheduled_publish_time,
            page_id=page_id,
        )

    def _actor_id(self, page_id: Optional[str] = None) -> str:
        jar = getattr(self.client, "jar", None)
        if page_id:
            return page_id
        if jar:
            return jar.c_user
        return "0"

    def _preprocess(self, image_bytes: bytes) -> bytes:
        """Strip EXIF, recompress JPEG, cap dimensions."""
        if not HAS_PIL:
            return image_bytes

        img = Image.open(io.BytesIO(image_bytes))
        if img.mode != "RGB":
            img = img.convert("RGB")

        if max(img.size) > self.MAX_DIMENSION:
            img.thumbnail(
                (self.MAX_DIMENSION, self.MAX_DIMENSION),
                Image.Resampling.LANCZOS,
            )

        out = io.BytesIO()
        img.save(
            out, format="JPEG", quality=self.JPEG_QUALITY,
            optimize=True, progressive=True,
        )
        return out.getvalue()

    async def _upload(self, jpeg_bytes: bytes) -> str:
        """POST to rupload.facebook.com/ajax/photo/async/upload/.

        Returns photo_id extracted from JSON response.
        """
        waterfall_id = str(uuid.uuid4())
        photo_sid = str(uuid.uuid4())
        upload_id = str(int(time.time() * 1000))

        data = {
            "fbid": upload_id,
            "target": self._actor_id(),
            "waterfall_id": waterfall_id,
            "photo_sid": photo_sid,
            "image_type": "FILE_ATTACHMENT",
            "upload_id": upload_id,
            "watermark": "0",
            "share_scrape": "0",
            "lds_type": "0",
        }

        headers = {
            "x-fb-photo-sid": photo_sid,
            "x-fb-photo-waterfall-id": waterfall_id,
            "x-entity-length": str(len(jpeg_bytes)),
            "x-entity-name": "blob",
            "x-entity-type": "image/jpeg",
        }

        files = {
            "file": ("image.jpg", jpeg_bytes, "image/jpeg"),
        }

        status, body, resp_headers = await self.client.post(
            "https://rupload.facebook.com/ajax/photo/async/upload/",
            data=data,
            headers=headers,
            files=files,
        )

        if status != 200:
            raise PhotoUploadError(f"HTTP {status}: {body[:200]}")

        try:
            result = json.loads(body) if isinstance(body, str) else body
        except (json.JSONDecodeError, TypeError):
            raise PhotoUploadError(f"Non-JSON response: {body[:200]}")

        if isinstance(result, dict) and result.get("error"):
            raise PhotoUploadError(f"FB error: {result['error']}")

        photo_id = None
        if isinstance(result, dict):
            photo_id = result.get("pid") or result.get("photo_id")
        if not photo_id:
            raise PhotoUploadError(f"No photo_id in response: {body[:200]}")
        return photo_id

    async def _publish_with_photo(
        self,
        photo_id: str,
        caption: str,
        mentions: Optional[List[Dict]] = None,
        scheduled_publish_time: Optional[int] = None,
        page_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Call ComposerStoryCreate with the photo attached."""
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
                "attachments": [{
                    "photo": {
                        "id": photo_id,
                        "uncomposed_photo_id": photo_id,
                    },
                }],
                "explicit_place": None,
                "text_format_preset_id": None,
                "backdated_time": None,
                "scheduled_publish_time": scheduled_publish_time,
                "logging": {
                    "composer_session_id": composer_session_id,
                    "entry_point": "feed_composer",
                },
            },
            "displayCommentsFeedbackSource": None,
            "displayCommentsContextIsReply": False,
        }

        from .doc_ids import get_fallback
        doc_ids = getattr(self.client, "doc_ids", None) or {}
        doc_id = doc_ids.get("ComposerStoryCreate", get_fallback("ComposerStoryCreate"))

        result = await self._graphql(
            doc_id=doc_id,
            variables=variables,
            friendly_name="ComposerStoryCreate",
        )

        post_id = self._extract_post_id(result)
        if not post_id:
            raise PhotoUploadError(f"No post_id in response: {result}")

        if not scheduled_publish_time:
            verified = await self._verify_post_succeeded(post_id, timeout_s=45)
            if not verified:
                raise SilentFailureError(
                    f"Photo post {post_id} not visible after 45s",
                )

        return {
            "post_id": post_id,
            "photo_id": photo_id,
            "url": f"https://www.facebook.com/{actor_id}/posts/{post_id}",
            "published_at": time.time(),
        }

    async def _graphql(self, doc_id: str, variables: dict, friendly_name: str = "") -> dict:
        prepared = self._prepare_graphql(doc_id, variables, friendly_name)
        status, body, _ = await self.client.post(
            "https://www.facebook.com/api/graphql/",
            data=prepared,
            timeout=15,
        )
        if status != 200:
            raise PhotoUploadError(f"GraphQL HTTP {status}: {body[:200]}")
        try:
            return json.loads(body) if isinstance(body, str) else body
        except (json.JSONDecodeError, TypeError):
            raise PhotoUploadError(f"Non-JSON GraphQL response: {body[:200]}")

    def _prepare_graphql(self, doc_id: str, variables: dict, friendly_name: str) -> Dict[str, str]:
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
        return body

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
                prepared = self._prepare_graphql(_gf("PagePosts"), variables, "PagePosts")
                status, body, _ = await self.client.post(
                    "https://www.facebook.com/api/graphql/",
                    data=prepared,
                    timeout=8,
                )
                if status >= 400:
                    await self._sleep(3)
                    continue
                data = json.loads(body) if isinstance(body, str) else body
                post_ids = [
                    n.get("node", {}).get("id", "")
                    for n in (data or {})
                    .get("data", {}).get("page", {}).get("timeline", {}).get("edges", [])
                ]
                if post_id in post_ids:
                    return True
                await self._sleep(3)
            except Exception:
                await self._sleep(3)
        return False

    async def _sleep(self, seconds: float):
        await __import__("asyncio").sleep(seconds)

    async def _dedup_get(self, key: str) -> Optional[str]:
        try:
            val = await self.dedup_cache.get(key)
            if isinstance(val, bytes):
                val = val.decode()
            return val
        except Exception:
            return None

    async def _dedup_set(self, key: str, ttl: int, value: str):
        try:
            await self.dedup_cache.setex(key, ttl, value)
        except Exception:
            pass
