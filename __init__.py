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
    from .session_heartbeat import SessionHeartbeatManager, get_live_cookie_header
    from .doc_id_scraper import DocIdScraper, run_doc_id_refresh_loop

    from .smart_poster import (
        smart_image_post,
        smart_video_post,
        looks_like_upload_block,
        mark_upload_blocked,
        is_upload_blocked,
        clear_upload_block,
        get_image_upload_strategy,
        build_text_fallback_caption,
    )

    # New anti-detection modules
    from .fb_client import FBClient, FBClientPool, FingerprintProfile
    from .jazoest import compute_jazoest, inject_jazoest
    from .circuit_breaker import CircuitBreaker, BreakerState, CircuitBreakerOpen
    from .rate_limiter import RateLimiter
    from .behavior_simulator import BehaviorSimulator, TelemetryFlusher
    from .checkpoint import CheckpointDetector, CheckpointEncountered, handle_checkpoint
    from .warmup_planner import WarmupLevel, WarmupState, WarmupPlanner
    from .token_trinity import TokenTrinityManager
    from .backoff import (
        NetworkError, TransientGraphQLError, PermanentGraphQLError,
        SilentFailureError, RetryBudgetExhausted,
        retry_with_backoff, classify_http_response, extract_retry_after,
    )
    from .mention_parser import Mention, parse_mentions, build_mention_ranges, strip_mentions
    from .cookie_jar import FBCookieJar
    from .fingerprint_profiles import FingerprintProfile as AtomicFingerprintProfile, get_profile, profile_to_fingerprint_dict, PROFILES
    from .photo_uploader import PhotoUploader, PhotoUploadError
    from .multi_photo_poster import MultiPhotoPoster, MultiPhotoError
    from .structured_logging import setup_logging, get_logger as get_structured_logger, FBJsonFormatter
    from .doc_ids import get_live_doc_id, get_fallback, FALLBACK_DOC_IDS
    from .worker import AccountState, STATE_BUDGET_MULTIPLIER

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
        "SessionHeartbeatManager",
        "get_live_cookie_header",
        "DocIdScraper",
        "run_doc_id_refresh_loop",
        "smart_image_post",
        "smart_video_post",
        "looks_like_upload_block",
        "mark_upload_blocked",
        "is_upload_blocked",
        "clear_upload_block",
        "get_image_upload_strategy",
        "build_text_fallback_caption",
        "FBClient",
        "FBClientPool",
        "FingerprintProfile",
        "compute_jazoest",
        "inject_jazoest",
        "CircuitBreaker",
        "BreakerState",
        "CircuitBreakerOpen",
        "RateLimiter",
        "BehaviorSimulator",
        "TelemetryFlusher",
        "CheckpointDetector",
        "CheckpointEncountered",
        "handle_checkpoint",
        "WarmupLevel",
        "WarmupState",
        "WarmupPlanner",
        "TokenTrinityManager",
        "NetworkError",
        "TransientGraphQLError",
        "PermanentGraphQLError",
        "SilentFailureError",
        "RetryBudgetExhausted",
        "retry_with_backoff",
        "classify_http_response",
        "extract_retry_after",
        "Mention",
        "parse_mentions",
        "build_mention_ranges",
        "strip_mentions",
        "FBCookieJar",
        "AtomicFingerprintProfile",
        "get_profile",
        "profile_to_fingerprint_dict",
        "PROFILES",
        "PhotoUploader",
        "PhotoUploadError",
        "MultiPhotoPoster",
        "MultiPhotoError",
        "setup_logging",
        "get_structured_logger",
        "FBJsonFormatter",
        "get_live_doc_id",
        "get_fallback",
        "FALLBACK_DOC_IDS",
        "AccountState",
        "STATE_BUDGET_MULTIPLIER",
    ]
else:
    # Pytest imports repository-root __init__.py as a top-level module when this
    # directory is the rootdir. In that mode relative package exports are not
    # resolvable, so keep collection side-effect free.
    __all__ = []
