"""Guarded image upload client.

The private Facebook HTTP upload path is disabled by default. This module keeps
validation/mutation/test seams available without making private endpoint calls
unless the caller explicitly enables AppConfig.enable_private_facebook_http.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import random
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from .config import AppConfig, SafetyConfig
from .models import IdentityContext
from .network import HeaderForge, ProxyManager, StealthConnector
from .timing import StochasticTimer
from .tokens import TokenVault
from .utils import classify_error, generate_client_id


class PrivateEndpointDisabled(RuntimeError):
    pass


_PHOTO_INIT_URL = 'https://rupload.facebook.com/photo-upload/v1'
_PHOTO_TRANSFER_URL = 'https://rupload.facebook.com/photo-upload/v1/{session_id}'


class HardenedRupload:
    def __init__(
        self,
        token_vault: TokenVault,
        identity: IdentityContext,
        redis_client: Any,
        proxy_manager: ProxyManager,
        config: Optional[AppConfig] = None,
    ):
        self.tokens = token_vault
        self.identity = identity
        self.redis = redis_client
        self.proxy = proxy_manager
        self.config = config or AppConfig()
        self.safety = SafetyConfig()

    async def upload_image(self, image_path: str) -> Tuple[bool, Optional[str], str]:
        if not self.config.enable_private_facebook_http:
            return False, None, 'Private Facebook HTTP upload is disabled by configuration.'
        if not self._validate_image(image_path):
            return False, None, 'Image validation failed.'

        mutated_path = ''
        proxy_url = ''
        try:
            mutated_path, image_bytes = self._mutate_and_encode(image_path)
            tokens = await self.tokens.get(self.identity.account_id)
            if not tokens:
                return False, None, 'TOKEN_EXPIRED'

            proxy_url = await self.proxy.get_proxy_for_account(self.identity.account_id)
            await self._sleep(StochasticTimer.think_time(200, 800))

            session_id, init_detail = await self._rupload_init(
                tokens,
                image_bytes,
                proxy_url,
            )
            if not session_id:
                return False, None, init_detail

            media_fbid, transfer_detail = await self._rupload_transfer(
                tokens,
                image_bytes,
                session_id,
                proxy_url,
            )
            if not media_fbid:
                return False, None, transfer_detail

            await self.tokens.increment_usage(self.identity.account_id)
            return True, str(media_fbid), 'Success'
        except Exception as exc:
            await self.proxy.report_failure(self.identity.account_id, proxy_url, str(exc))
            return False, None, f'Exception: {str(exc)[:200]}'
        finally:
            if mutated_path and mutated_path != image_path:
                try:
                    os.unlink(mutated_path)
                except OSError:
                    pass

    def _validate_image(self, path: str) -> bool:
        try:
            from PIL import Image
        except Exception:
            return False
        try:
            size = os.path.getsize(path)
            if size < 1024 or size > self.safety.max_image_size_bytes:
                logger.warning(
                    'Image validation failed for %s: size %s outside allowed range',
                    path,
                    size,
                )
                return False
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                return image.width >= 100 and image.height >= 100 and image.format in {
                    'JPEG',
                    'PNG',
                    'WEBP',
                    'GIF',
                    'TIFF',
                }
        except Exception as exc:
            logger.warning('Image validation failed for %s: %s', path, exc)
            return False

    def _mutate_and_encode(self, path: str) -> Tuple[str, bytes]:
        """
        Re-encode the source as JPEG and calculate bytes before upload headers.

        The photo rupload path validates X-Entity-Length/Content-Length against
        the actual transfer body, so the in-memory JPEG bytes are authoritative.
        """
        try:
            from PIL import Image, ImageOps
        except Exception as exc:
            raise RuntimeError(f'Pillow is required for image mutation: {exc}') from exc

        with Image.open(path) as original:
            image = ImageOps.exif_transpose(original)
            if image.mode in {'RGBA', 'LA'} or (image.mode == 'P' and 'transparency' in image.info):
                rgba = image.convert('RGBA')
                flattened = Image.new('RGB', rgba.size, (255, 255, 255))
                flattened.paste(rgba, mask=rgba.getchannel('A'))
                image = flattened
            elif image.mode != 'RGB':
                image = image.convert('RGB')
            else:
                image = image.copy()

        resampling = getattr(getattr(Image, 'Resampling', Image), 'LANCZOS', 1)
        width, height = image.size
        new_width = max(100, width + random.choice([-1, 0, 1]))
        new_height = max(100, height + random.choice([-1, 0, 1]))
        if (new_width, new_height) != image.size:
            image = image.resize((new_width, new_height), resampling)

        pixels = image.load()
        x = random.randint(0, max(0, image.width - 1))
        y = random.randint(0, max(0, image.height - 1))
        red, green, blue = pixels[x, y]
        pixels[x, y] = (
            max(0, min(255, red + random.randint(-2, 2))),
            max(0, min(255, green + random.randint(-2, 2))),
            max(0, min(255, blue + random.randint(-2, 2))),
        )

        buffer = io.BytesIO()
        image.save(
            buffer,
            format='JPEG',
            quality=random.randint(88, 94),
            optimize=True,
            progressive=False,
            exif=b'',
        )
        image_bytes = buffer.getvalue()
        if len(image_bytes) <= 0:
            raise RuntimeError('Mutated JPEG encoder returned an empty payload.')

        handle = tempfile.NamedTemporaryFile(prefix='fb_upload_', suffix='.jpg', delete=False)
        try:
            handle.write(image_bytes)
            handle.close()
        except Exception:
            handle.close()
            raise
        return handle.name, image_bytes

    @staticmethod
    def _entity_name(image_bytes: bytes) -> str:
        return f'fb_img_{hashlib.md5(image_bytes[:128]).hexdigest()[:12]}.jpg'

    @staticmethod
    def _headers_with_updates(headers: Dict[str, str], updates: Dict[str, str]) -> Dict[str, str]:
        update_keys = {key.lower() for key in updates}
        merged = {
            str(key): str(value)
            for key, value in headers.items()
            if str(key).lower() not in update_keys
        }
        merged.update({str(key): str(value) for key, value in updates.items()})
        return merged

    def _rupload_headers(
        self,
        tokens: Dict[str, Any],
        image_bytes: bytes,
        *,
        init_request: bool,
    ) -> Dict[str, str]:
        content_length = len(image_bytes)
        entity_name = self._entity_name(image_bytes)
        headers = HeaderForge.forge_rupload_headers(tokens, self.identity, content_length, offset=0)
        updates = {
            'Content-Type': 'application/x-www-form-urlencoded' if init_request else 'image/jpeg',
            'Content-Length': '0' if init_request else str(content_length),
            'X-Entity-Length': str(content_length),
            'X-Entity-Type': 'image/jpeg',
            'X-Entity-Name': entity_name,
            'X-Attempts-Count': '1',
        }
        cookie_header = str(tokens.get('cookie_header') or '').strip()
        if cookie_header:
            updates['Cookie'] = cookie_header
        if not init_request:
            updates['X-Start-Offset'] = '0'
        return self._headers_with_updates(headers, updates)

    async def _rupload_init(
        self,
        tokens: Dict[str, Any],
        image_bytes: bytes,
        proxy_url: str,
    ) -> Tuple[Optional[str], str]:
        headers = self._rupload_headers(tokens, image_bytes, init_request=True)
        init_status, init_text = await self._post_form(
            _PHOTO_INIT_URL,
            b'',
            headers,
            proxy_url,
            timeout_seconds=15,
        )
        if init_status >= 400:
            return None, f'RUPLOAD_INIT_HTTP_{init_status}: {init_text[:300]}'
        try:
            init_data = self._loads_json(init_text)
        except Exception:
            return None, f'RUPLOAD_INIT_PARSE_FAILED: {init_text[:300]}'
        session_id = None
        if isinstance(init_data, dict):
            session_id = init_data.get('upload_session_id') or init_data.get('h')
        if not session_id:
            return None, f'RUPLOAD_INIT_FAILED: {init_text[:300]}'
        return str(session_id), ''

    async def _rupload_transfer(
        self,
        tokens: Dict[str, Any],
        image_bytes: bytes,
        session_id: str,
        proxy_url: str,
    ) -> Tuple[Optional[str], str]:
        headers = self._rupload_headers(tokens, image_bytes, init_request=False)
        result_status, result_text = await self._post_form(
            _PHOTO_TRANSFER_URL.format(session_id=session_id),
            image_bytes,
            headers,
            proxy_url,
            timeout_seconds=45,
        )
        if result_status >= 400:
            return None, f'RUPLOAD_TRANSFER_HTTP_{result_status}: {result_text[:300]}'
        try:
            result = self._loads_json(result_text)
        except Exception:
            return None, f'RUPLOAD_TRANSFER_PARSE_FAILED: {result_text[:300]}'
        media_fbid = (
            result.get('fbid') or result.get('media_fbid')
            if isinstance(result, dict)
            else None
        )
        if not media_fbid:
            return None, f'{classify_error(result_text)}: {result_text[:300]}'
        return str(media_fbid), ''

    def build_upload_init_payload(
        self,
        tokens: Dict[str, Any],
        file_size: int,
        file_name: str,
    ) -> Dict[str, str]:
        payload = {
            'fb_dtsg': str(tokens.get('fb_dtsg') or ''),
            'lsd': str(tokens.get('lsd') or ''),
            'file_size': str(int(file_size)),
            'media_type': 'image/jpeg',
            'file_name': str(file_name),
            'client_id': generate_client_id(self.identity.account_id),
        }
        self.validate_upload_init_payload(payload)
        return payload

    @staticmethod
    def validate_upload_init_payload(payload: Dict[str, str]) -> None:
        required = {'fb_dtsg', 'lsd', 'file_size', 'media_type', 'file_name', 'client_id'}
        missing = [key for key in sorted(required) if key not in payload]
        if missing:
            raise ValueError(f'missing rupload init fields: {missing}')
        if payload['media_type'] != 'image/jpeg':
            raise ValueError('rupload image media_type must be image/jpeg')
        if int(payload['file_size']) <= 0:
            raise ValueError('file_size must be positive')
        if not payload['file_name'].endswith('.jpg'):
            raise ValueError('file_name must be a mutated JPEG name')

    async def _post_form(
        self,
        url: str,
        data: Any,
        headers: Dict[str, str],
        proxy: str,
        timeout_seconds: int,
    ) -> Tuple[int, str]:
        try:
            import aiohttp  # type: ignore[reportMissingImports]
        except Exception:
            aiohttp = None

        if aiohttp is not None:
            connector = StealthConnector.create_connector()
            async with aiohttp.ClientSession(connector=connector, trust_env=True) as session:
                async with session.post(
                    url,
                    data=data,
                    headers=headers,
                    proxy=proxy or None,
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                ) as resp:
                    return int(resp.status), await resp.text()

        def _sync_post() -> Tuple[int, str]:
            request_headers = dict(headers)
            request_headers['accept-encoding'] = 'identity'
            if isinstance(data, bytes):
                body = data
            elif isinstance(data, str):
                body = data.encode('utf-8')
            elif isinstance(data, dict):
                body = urllib_parse.urlencode(data).encode('utf-8')
            else:
                body = urllib_parse.urlencode(dict(data)).encode('utf-8') if data else b''

            request = urllib_request.Request(url, data=body, headers=request_headers, method='POST')
            handlers = []
            if proxy:
                handlers.append(urllib_request.ProxyHandler({'http': proxy, 'https': proxy}))
            opener = urllib_request.build_opener(*handlers)
            try:
                with opener.open(request, timeout=timeout_seconds) as resp:
                    status = int(getattr(resp, 'status', resp.getcode() or 200))
                    return status, resp.read().decode('utf-8', errors='replace')
            except urllib_error.HTTPError as exc:
                return int(exc.code), exc.read().decode('utf-8', errors='replace')

        return await asyncio.to_thread(_sync_post)

    @staticmethod
    def _loads_json(text: str) -> Any:
        cleaned = str(text or '').strip()
        if cleaned.startswith('for(;;);'):
            cleaned = cleaned[len('for(;;);'):].strip()
        if cleaned.startswith('for (;;);'):
            cleaned = cleaned[len('for (;;);'):].strip()
        return json.loads(cleaned or '{}')

    @staticmethod
    async def _sleep(seconds: float) -> None:
        import asyncio

        await asyncio.sleep(max(0.0, seconds))
