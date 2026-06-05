"""
AntiDetectionOrchestrator - Ties all stealth components together.

Provides a single interface to create fully stealthed browser
sessions and HTTP clients.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class AntiDetectionConfig:
    """Master configuration for all anti-detection components."""
    # Connector
    use_tls_client: bool = True
    chrome_version: str = "120.0.0.0"

    # Stealth
    enable_canvas_noise: bool = True
    enable_webgl_spoof: bool = True
    enable_audio_spoof: bool = True
    webgl_profile_index: Optional[int] = None

    # Proxy
    proxy_urls: List[str] = field(default_factory=list)
    proxy_country: Optional[str] = None
    proxy_strategy: str = "least_used"

    # Timing
    enable_human_timing: bool = True
    typing_speed_min: float = 4.0
    typing_speed_max: float = 12.0

    # Image mutation
    enable_image_mutation: bool = True
    image_mutation_seed: Optional[int] = None

    # Browser
    headless: bool = True
    locale: str = "en-US"
    timezone: str = "Africa/Cairo"
    viewport_width: int = 1280
    viewport_height: int = 900

    @classmethod
    def from_env(cls) -> "AntiDetectionConfig":
        """Load configuration from environment variables."""
        return cls(
            use_tls_client=os.getenv("STEALTH_USE_TLS_CLIENT", "true").lower() == "true",
            chrome_version=os.getenv("STEALTH_CHROME_VERSION", "120.0.0.0"),
            enable_canvas_noise=os.getenv("STEALTH_CANVAS_NOISE", "true").lower() == "true",
            enable_webgl_spoof=os.getenv("STEALTH_WEBGL_SPOOF", "true").lower() == "true",
            enable_audio_spoof=os.getenv("STEALTH_AUDIO_SPOOF", "true").lower() == "true",
            webgl_profile_index=int(os.getenv("STEALTH_WEBGL_PROFILE", "0") or "0"),
            proxy_urls=[
                url.strip()
                for url in os.getenv("STEALTH_PROXY_URLS", "").split(",")
                if url.strip()
            ],
            proxy_country=os.getenv("STEALTH_PROXY_COUNTRY", "").strip() or None,
            proxy_strategy=os.getenv("STEALTH_PROXY_STRATEGY", "least_used"),
            enable_human_timing=os.getenv("STEALTH_HUMAN_TIMING", "true").lower() == "true",
            typing_speed_min=float(os.getenv("STEALTH_TYPING_MIN", "4.0")),
            typing_speed_max=float(os.getenv("STEALTH_TYPING_MAX", "12.0")),
            enable_image_mutation=os.getenv("STEALTH_IMAGE_MUTATION", "true").lower() == "true",
            headless=os.getenv("HEADLESS", "true").lower() == "true",
        )


class AntiDetectionOrchestrator:
    """
    Single entry point for creating stealthed browser sessions
    and HTTP clients.

    Usage::

        ad = AntiDetectionOrchestrator()

        # For browser automation:
        context, page = await ad.create_stealth_session(
            cookies_json=cookies,
            account_key="user123:account1",
        )

        # For HTTP API calls:
        session = ad.create_stealth_http_session(
            account_key="user123:account1",
        )

        # For image mutation:
        mutated_bytes, info = ad.mutate_image(image_bytes)

        # For human-like delays:
        await ad.timer.pause_after_click()
    """

    def __init__(self, config: Optional[AntiDetectionConfig] = None):
        self.config = config or AntiDetectionConfig.from_env()
        self._connector = None
        self._header_forge = None
        self._browser_stealth = None
        self._proxy_manager = None
        self._timer = None
        self._image_mutator = None
        self._initialized = False
        self._playwright = None
        self._browser = None

    async def initialize(self):
        """Initialize all components."""
        if self._initialized:
            return

        # 1. Connector (TLS fingerprint)
        from .stealth_connector import AdvancedStealthConnector, StealthConnectorConfig
        self._connector = AdvancedStealthConnector(
            config=StealthConnectorConfig(
                use_tls_client_for_api=self.config.use_tls_client,
            )
        )
        self._connector.select_profile()

        # 2. Header forge
        from .header_forge import AdvancedHeaderForge
        self._header_forge = AdvancedHeaderForge(
            chrome_version=self.config.chrome_version,
        )
        self._header_forge.freeze_random_fields()

        # 3. Browser stealth
        from .browser_stealth import BrowserStealth, StealthConfig
        stealth_config = StealthConfig(
            canvas_noise=self.config.enable_canvas_noise,
            spoof_webgl_vendor=self.config.enable_webgl_spoof,
            spoof_webgl_renderer=self.config.enable_webgl_spoof,
            spoof_audio_context=self.config.enable_audio_spoof,
            screen_width=self.config.viewport_width,
            screen_height=self.config.viewport_height,
        )
        self._browser_stealth = BrowserStealth(config=stealth_config)
        if self.config.webgl_profile_index is not None:
            self._browser_stealth.select_webgl_profile(self.config.webgl_profile_index)

        # 4. Proxy manager
        if self.config.proxy_urls:
            from .proxy_manager import AdvancedProxyManager, ProxyManagerConfig, init_advanced_proxy_manager
            self._proxy_manager = init_advanced_proxy_manager(
                proxy_urls=self.config.proxy_urls,
                config=ProxyManagerConfig(
                    proxy_selection_strategy=self.config.proxy_strategy,
                ),
            )

        # 5. Timer
        from .stochastic_timer import AdvancedStochasticTimer, TimingProfile
        if self.config.enable_human_timing:
            self._timer = AdvancedStochasticTimer(
                profile=TimingProfile(
                    typing_speed_min=self.config.typing_speed_min,
                    typing_speed_max=self.config.typing_speed_max,
                ),
            )
        else:
            self._timer = AdvancedStochasticTimer(
                profile=TimingProfile(
                    burst_probability=0.0,
                    distraction_probability=0.0,
                ),
            )

        # 6. Image mutator
        if self.config.enable_image_mutation:
            from .image_mutator import ImageMutator, MutationConfig
            self._image_mutator = ImageMutator(
                config=MutationConfig(
                    deterministic_seed=self.config.image_mutation_seed,
                ),
            )

        # 7. Launch browser
        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()
        launch_args = self._browser_stealth.get_chromium_launch_args()
        self._browser = await self._playwright.chromium.launch(
            headless=self.config.headless,
            args=launch_args,
        )

        self._initialized = True
        logger.info(
            f"AntiDetectionOrchestrator: initialized "
            f"Chrome/{self.config.chrome_version} "
            f"proxies={len(self.config.proxy_urls)} "
            f"headless={self.config.headless}"
        )

    async def create_stealth_session(
        self,
        cookies_json: str,
        account_key: str = "default",
        proxy_url: Optional[str] = None,
    ) -> Tuple[Any, Any]:
        """
        Create a fully stealthed browser session.

        Returns (context, page) ready for automation.
        """
        if not self._initialized:
            await self.initialize()

        # Resolve proxy
        if proxy_url is None and self._proxy_manager:
            proxy_url = self._proxy_manager.get_proxy_for_account(
                account_key,
                prefer_country=self.config.proxy_country,
            )

        # Build context options
        context_options = await self._connector.create_stealth_context_options(
            proxy_url=proxy_url,
        )
        context_options["viewport"] = {
            "width": self.config.viewport_width,
            "height": self.config.viewport_height,
        }
        context_options["timezone_id"] = self.config.timezone
        context_options["locale"] = self.config.locale

        # Create context
        context = await self._browser.new_context(**context_options)

        # Inject stealth scripts
        await self._browser_stealth.apply_to_context(context)

        # Add cookies
        cookies = json.loads(cookies_json)
        await context.add_cookies(cookies)

        # Create page
        page = await context.new_page()

        # Attach network monitoring
        page.on("requestfailed", lambda req: logger.debug(
            f"NETWORK FAILED: {req.url[:120]}"
        ))

        logger.debug(
            f"AntiDetection: session created for {account_key} "
            f"proxy={proxy_url or 'direct'}"
        )

        return context, page

    def create_stealth_http_session(
        self,
        account_key: str = "default",
        proxy_url: Optional[str] = None,
    ) -> Any:
        """
        Create a stealthed HTTP session for API calls.
        Uses tls-client if available, falls back to requests.
        """
        if not self._initialized:
            raise RuntimeError("Call initialize() first")

        if proxy_url is None and self._proxy_manager:
            proxy_url = self._proxy_manager.get_proxy_for_account(
                account_key,
                prefer_country=self.config.proxy_country,
            )

        return self._connector.get_http_session(proxy_url=proxy_url)

    def build_xhr_headers(
        self,
        host: str,
        origin: str,
        referer: str,
        cookies: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, str]:
        """Build Chrome-matching XHR headers."""
        return self._header_forge.build_xhr_headers(
            host=host,
            origin=origin,
            referer=referer,
            cookies=cookies,
            **kwargs,
        )

    async def mutate_image(
        self,
        image_data: bytes,
        target_format: str = "JPEG",
    ) -> Tuple[bytes, Dict[str, Any]]:
        """Mutate an image to change its perceptual hash."""
        if self._image_mutator is None:
            return image_data, {"success": False, "error": "Image mutation disabled"}
        return self._image_mutator.mutate_bytes(image_data, target_format)

    @property
    def timer(self):
        """Access the stochastic timer for human-like delays."""
        return self._timer

    def report_proxy_success(self, proxy_url: str):
        if self._proxy_manager:
            self._proxy_manager.report_success(proxy_url)

    def report_proxy_failure(self, proxy_url: str):
        if self._proxy_manager:
            self._proxy_manager.report_failure(proxy_url)

    def release_proxy(self, proxy_url: str):
        if self._proxy_manager:
            self._proxy_manager.release_proxy(proxy_url)

    async def close(self):
        """Clean up all resources."""
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        if self._connector:
            self._connector.close()
        self._initialized = False
        logger.info("AntiDetectionOrchestrator: closed")


# ── Convenience: Global singleton ──
_orchestrator: Optional[AntiDetectionOrchestrator] = None


async def get_anti_detection() -> AntiDetectionOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = AntiDetectionOrchestrator()
        await _orchestrator.initialize()
    return _orchestrator
