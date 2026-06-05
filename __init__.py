"""Guarded Facebook automation package."""

import asyncio
from typing import Any, Callable


if not hasattr(asyncio, "to_thread"):
    async def _asyncio_to_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: func(*args, **kwargs))

    asyncio.to_thread = _asyncio_to_thread  # type: ignore[attr-defined]


if __package__:
    from .browser_fallback import BrowserTokenExtractor, BrowserVideoUploader
    from .config import AppConfig, EthicalGuardrails, SafetyConfig
    from .health import AccountHealthSnapshot, HealthMonitor
    from .lifecycle import ApplicationLifecycle
    from .models import IdentityContext, PostJob, PostResult, QuarantineRecord, TokenBundle
    from .orchestrator import QueueOrchestrator
    from .runner import WorkerLoop

    from .anti_detection import AntiDetectionOrchestrator, AntiDetectionConfig, get_anti_detection
    from .browser_stealth import BrowserStealth, StealthConfig, get_browser_stealth
    from .header_forge import AdvancedHeaderForge
    from .image_mutator import ImageMutator, MutationConfig, mutate_image_for_upload
    from .proxy_manager import AdvancedProxyManager, ProxyManagerConfig
    from .stealth_connector import AdvancedStealthConnector, StealthConnectorConfig, get_stealth_connector
    from .stochastic_timer import AdvancedStochasticTimer, TimingProfile, BezierCurve

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
        "AppConfig",
        "BrowserTokenExtractor",
        "BrowserVideoUploader",
        "EthicalGuardrails",
        "SafetyConfig",
        "AccountHealthSnapshot",
        "ApplicationLifecycle",
        "HealthMonitor",
        "IdentityContext",
        "PostJob",
        "PostResult",
        "QueueOrchestrator",
        "QuarantineRecord",
        "TokenBundle",
        "WorkerLoop",
        "AntiDetectionOrchestrator",
        "AntiDetectionConfig",
        "get_anti_detection",
        "BrowserStealth",
        "StealthConfig",
        "get_browser_stealth",
        "AdvancedHeaderForge",
        "ImageMutator",
        "MutationConfig",
        "mutate_image_for_upload",
        "AdvancedProxyManager",
        "ProxyManagerConfig",
        "AdvancedStealthConnector",
        "StealthConnectorConfig",
        "get_stealth_connector",
        "AdvancedStochasticTimer",
        "TimingProfile",
        "BezierCurve",
        "smart_image_post",
        "looks_like_upload_block",
        "mark_upload_blocked",
        "is_upload_blocked",
        "clear_upload_block",
        "get_image_upload_strategy",
        "build_text_fallback_caption",
    ]
else:
    # Pytest imports repository-root __init__.py as a top-level module when this
    # directory is the rootdir. In that mode relative package exports are not
    # resolvable, so keep collection side-effect free.
    __all__ = []
