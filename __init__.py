"""Guarded Facebook automation package.

This package is intentionally isolated from the existing production bot. Private
Facebook HTTP endpoints are disabled by default and require an explicit runtime
flag plus caller-provided tokens/configuration.
"""

import asyncio
from typing import Any, Callable


if not hasattr(asyncio, 'to_thread'):
    async def _asyncio_to_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: func(*args, **kwargs))

    asyncio.to_thread = _asyncio_to_thread  # type: ignore[attr-defined]

from .browser_fallback import BrowserTokenExtractor, BrowserVideoUploader
from .config import AppConfig, EthicalGuardrails, SafetyConfig
from .health import AccountHealthSnapshot, HealthMonitor
from .lifecycle import ApplicationLifecycle
from .models import IdentityContext, PostJob, PostResult, QuarantineRecord, TokenBundle
from .orchestrator import QueueOrchestrator
from .runner import WorkerLoop

# Anti-detection components
from .anti_detection import AntiDetectionOrchestrator, AntiDetectionConfig, get_anti_detection
from .browser_stealth import BrowserStealth, StealthConfig, get_browser_stealth
from .header_forge import AdvancedHeaderForge
from .image_mutator import ImageMutator, MutationConfig, mutate_image_for_upload
from .proxy_manager import AdvancedProxyManager, ProxyManagerConfig
from .stealth_connector import AdvancedStealthConnector, StealthConnectorConfig, get_stealth_connector
from .stochastic_timer import AdvancedStochasticTimer, TimingProfile, BezierCurve

# Smart routing for upload-blocked accounts
from .smart_poster import (
    smart_image_post,
    looks_like_upload_block,
    mark_upload_blocked,
    is_upload_blocked,
    clear_upload_block,
    get_image_upload_strategy,
    build_text_fallback_caption,
)

__all__ = [
    'AppConfig',
    'BrowserTokenExtractor',
    'BrowserVideoUploader',
    'EthicalGuardrails',
    'SafetyConfig',
    'AccountHealthSnapshot',
    'ApplicationLifecycle',
    'HealthMonitor',
    'IdentityContext',
    'PostJob',
    'PostResult',
    'QueueOrchestrator',
    'QuarantineRecord',
    'TokenBundle',
    'WorkerLoop',
    # Anti-detection
    'AntiDetectionOrchestrator',
    'AntiDetectionConfig',
    'get_anti_detection',
    'BrowserStealth',
    'StealthConfig',
    'get_browser_stealth',
    'AdvancedHeaderForge',
    'ImageMutator',
    'MutationConfig',
    'mutate_image_for_upload',
    'AdvancedProxyManager',
    'ProxyManagerConfig',
    'AdvancedStealthConnector',
    'StealthConnectorConfig',
    'get_stealth_connector',
    'AdvancedStochasticTimer',
    'TimingProfile',
    'BezierCurve',
    # Smart routing
    'smart_image_post',
    'looks_like_upload_block',
    'mark_upload_blocked',
    'is_upload_blocked',
    'clear_upload_block',
    'get_image_upload_strategy',
    'build_text_fallback_caption',
]
