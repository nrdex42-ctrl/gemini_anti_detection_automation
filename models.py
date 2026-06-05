"""Data contracts for the guarded automation package."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


POST_TYPES = {'text', 'image', 'video'}


def _parse_datetime(value: Any) -> Optional[datetime]:
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if text.endswith('Z'):
        text = f'{text[:-1]}+00:00'
    return datetime.fromisoformat(text)


@dataclass(frozen=True)
class IdentityContext:
    account_id: str
    proxy_url: str
    user_agent: str = (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
    )
    viewport: Tuple[int, int] = (1365, 768)
    timezone: str = 'UTC'
    locale: str = 'en-US'
    geolocation: Optional[Dict[str, float]] = None
    screen_resolution: Tuple[int, int] = (1920, 1080)
    color_depth: int = 24
    platform: str = 'Win32'
    chrome_version: str = '126.0.0.0'
    webgl_vendor: str = 'Google Inc. (Intel)'
    webgl_renderer: str = 'ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0)'
    fonts: List[str] = field(default_factory=lambda: ['Arial', 'Times New Roman', 'Segoe UI'])
    audio_sample_rate: int = 48000
    facebook_user_id: str = ''

    def __post_init__(self) -> None:
        if self.viewport[0] > self.screen_resolution[0] or self.viewport[1] > self.screen_resolution[1]:
            raise ValueError('viewport must be less than or equal to screen_resolution')
        if not self.account_id:
            raise ValueError('account_id is required')
        if self.proxy_url and not self.proxy_url.startswith(('http://', 'https://', 'socks5://')):
            raise ValueError('proxy_url must start with http://, https://, or socks5://')
        if 'Chrome/' not in self.user_agent or not re.search(r'Chrome/\d+\.\d+', self.user_agent):
            raise ValueError('user_agent must contain a valid Chrome/ version')
        if self.chrome_version and self.chrome_version not in self.user_agent:
            raise ValueError('chrome_version must appear in user_agent')
        platform_markers = {
            'Win32': 'Windows',
            'MacIntel': 'Macintosh',
            'Linux x86_64': 'Linux',
        }
        marker = platform_markers.get(self.platform)
        if marker and marker not in self.user_agent:
            raise ValueError(f'platform {self.platform} requires {marker} in user_agent')
        self._validate_locale_timezone()
        self._validate_geolocation()
        if self.platform == 'Win32' and not any(
            marker in self.webgl_vendor for marker in ('Google Inc.', 'Intel', 'NVIDIA', 'AMD')
        ):
            raise ValueError('Windows platform should have a consistent WebGL vendor')

    def _validate_locale_timezone(self) -> None:
        timezone_region = self.timezone.split('/', 1)[0] if '/' in self.timezone else ''
        if not timezone_region:
            return
        normalized_locale = self.locale.replace('-', '_')
        locale_map = {
            'America': {'en_US', 'en_CA', 'es_MX', 'pt_BR'},
            'Europe': {'en_GB', 'de_DE', 'fr_FR', 'es_ES', 'it_IT'},
            'Asia': {'en_SG', 'ja_JP', 'ko_KR', 'zh_CN', 'zh_TW'},
            'Australia': {'en_AU'},
            'Africa': {'en_ZA', 'en_US'},
        }
        allowed = locale_map.get(timezone_region)
        if allowed and normalized_locale not in allowed:
            raise ValueError(f'locale {self.locale} does not match timezone region {timezone_region}')

    def _validate_geolocation(self) -> None:
        if not self.geolocation:
            return
        latitude = float(self.geolocation.get('latitude', self.geolocation.get('lat', 0.0)))
        longitude = float(self.geolocation.get('longitude', self.geolocation.get('lon', 0.0)))
        if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
            raise ValueError('geolocation latitude/longitude out of bounds')

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> 'IdentityContext':
        data = dict(value or {})
        for key in ('viewport', 'screen_resolution'):
            if isinstance(data.get(key), list):
                data[key] = tuple(data[key])
        if isinstance(data.get('fonts'), tuple):
            data['fonts'] = list(data['fonts'])
        geolocation = data.get('geolocation')
        if isinstance(geolocation, (list, tuple)) and len(geolocation) >= 2:
            data['geolocation'] = {
                'latitude': float(geolocation[0]),
                'longitude': float(geolocation[1]),
            }
        return cls(**data)

    def to_browser_args(self) -> Dict[str, Any]:
        proxy = {'server': self.proxy_url} if self.proxy_url else None
        options: Dict[str, Any] = {
            'user_agent': self.user_agent,
            'viewport': {'width': self.viewport[0], 'height': self.viewport[1]},
            'locale': self.locale,
            'timezone_id': self.timezone,
            'device_scale_factor': 1,
            'is_mobile': False,
            'has_touch': False,
        }
        if proxy:
            options['proxy'] = proxy
        if self.geolocation:
            options['geolocation'] = self.geolocation
            options['permissions'] = ['geolocation']
        return options


@dataclass(frozen=True)
class PostJob:
    account_id: str
    page_id: str
    caption: str
    media_url: Optional[str] = None
    post_type: str = 'text'
    scheduled_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.post_type not in POST_TYPES:
            raise ValueError(f'post_type must be one of {sorted(POST_TYPES)}')
        if not self.account_id or not self.page_id:
            raise ValueError('account_id and page_id are required')

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if self.scheduled_at:
            data['scheduled_at'] = self.scheduled_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> 'PostJob':
        data = dict(value or {})
        data['scheduled_at'] = _parse_datetime(data.get('scheduled_at'))
        return cls(**data)


@dataclass(frozen=True)
class PostResult:
    success: bool
    status: str
    page_id: str
    post_id: Optional[str] = None
    error_message: Optional[str] = None
    execution_time_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> 'PostResult':
        return cls(**dict(value or {}))


@dataclass(frozen=True)
class TokenBundle:
    fb_dtsg: str
    lsd: str
    user_id: str
    xs: str = ''
    revision: str = ''
    timestamp: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> 'TokenBundle':
        return cls(**dict(value or {}))


@dataclass(frozen=True)
class QuarantineRecord:
    account_id: str
    level: str
    reason: str
    expires_at: Optional[datetime]
    created_at: datetime

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        for key in ('expires_at', 'created_at'):
            if data.get(key):
                data[key] = data[key].isoformat()
        return data

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> 'QuarantineRecord':
        data = dict(value or {})
        data['expires_at'] = _parse_datetime(data.get('expires_at'))
        created_at = _parse_datetime(data.get('created_at'))
        if created_at is None:
            raise ValueError('created_at is required')
        data['created_at'] = created_at
        return cls(**data)
