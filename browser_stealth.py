"""
BrowserStealth - Comprehensive browser fingerprint hardening.

Injects JavaScript that overrides browser APIs to remove automation
detection vectors: webdriver flag, canvas fingerprint, WebGL,
AudioContext, plugins, languages, permissions, and more.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class StealthConfig:
    """Controls which stealth techniques are enabled."""
    # Core automation flags
    remove_webdriver: bool = True
    remove_automation: bool = True
    remove_selenium: bool = True
    remove_cdp: bool = True

    # Canvas fingerprint
    canvas_noise: bool = False
    canvas_noise_amplitude: float = 0.5  # Very subtle

    # WebGL fingerprint
    spoof_webgl_vendor: bool = True
    spoof_webgl_renderer: bool = True
    webgl_vendor: str = "Google Inc. (NVIDIA)"
    webgl_renderer: str = "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"

    # AudioContext fingerprint
    spoof_audio_context: bool = True

    # Plugin/Navigator spoofing
    spoof_plugins: bool = False
    spoof_languages: bool = False
    spoof_platform: bool = False
    spoof_hardware_concurrency: bool = False
    spoof_device_memory: bool = False

    # Permission spoofing
    spoof_permissions: bool = False

    # Chrome-specific
    spoof_chrome_runtime: bool = True
    spoof_notification_permission: bool = False

    # iframe contentWindow
    protect_iframe_content_window: bool = False

    # Date/Timezone consistency
    fix_timezone_consistency: bool = False

    # Connection info
    spoof_connection: bool = False

    # Media devices
    spoof_media_devices: bool = False

    # Screen properties
    spoof_screen: bool = False
    screen_width: int = 1280
    screen_height: int = 720
    color_depth: int = 24
    pixel_ratio: float = 1.0

    # Reduce motion (accessibility)
    respect_reduced_motion: bool = False


# WebGL profiles for different GPU vendors
WEBGL_PROFILES = [
    {
        "vendor": "Google Inc. (NVIDIA)",
        "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "unmasked_vendor": "NVIDIA",
        "unmasked_renderer": "NVIDIA GeForce GTX 1650",
    },
    {
        "vendor": "Google Inc. (Intel)",
        "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "unmasked_vendor": "Intel",
        "unmasked_renderer": "Intel(R) UHD Graphics 630",
    },
    {
        "vendor": "Google Inc. (AMD)",
        "renderer": "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "unmasked_vendor": "AMD",
        "unmasked_renderer": "AMD Radeon RX 580",
    },
]


class BrowserStealth:
    """
    Generates and injects JavaScript stealth scripts into Playwright pages.
    """

    def __init__(self, config: Optional[StealthConfig] = None):
        self.config = config or StealthConfig()
        self._webgl_profile = WEBGL_PROFILES[0]
        self._init_script: Optional[str] = None

    def select_webgl_profile(self, index: Optional[int] = None):
        """Select a WebGL GPU profile."""
        if index is not None:
            self._webgl_profile = WEBGL_PROFILES[index % len(WEBGL_PROFILES)]
        else:
            self._webgl_profile = random.choice(WEBGL_PROFILES)
        logger.debug(f"BrowserStealth: WebGL profile = {self._webgl_profile['unmasked_renderer']}")

    def build_init_script(self) -> str:
        """
        Build the complete stealth JavaScript to be injected
        before any page code runs.

        This must be added via ``context.add_init_script()``.
        """
        if self._init_script is not None:
            return self._init_script

        parts = []

        # ── 1. Remove automation flags ──
        if self.config.remove_webdriver:
            parts.append(self._script_remove_webdriver())
        if self.config.remove_automation:
            parts.append(self._script_remove_automation())
        if self.config.remove_selenium:
            parts.append(self._script_remove_selenium())
        if self.config.remove_cdp:
            parts.append(self._script_remove_cdp())

        # ── 2. Canvas noise ──
        if self.config.canvas_noise:
            parts.append(self._script_canvas_noise())

        # ── 3. WebGL spoofing ──
        if self.config.spoof_webgl_vendor or self.config.spoof_webgl_renderer:
            parts.append(self._script_webgl_spoof())

        # ── 4. AudioContext spoofing ──
        if self.config.spoof_audio_context:
            parts.append(self._script_audio_spoof())

        # ── 5. Navigator spoofing ──
        if self.config.spoof_plugins:
            parts.append(self._script_plugins_spoof())
        if self.config.spoof_languages:
            parts.append(self._script_languages_spoof())
        if self.config.spoof_platform:
            parts.append(self._script_platform_spoof())
        if self.config.spoof_hardware_concurrency:
            parts.append(self._script_hardware_spoof())
        if self.config.spoof_device_memory:
            parts.append(self._script_device_memory_spoof())

        # ── 6. Permissions ──
        if self.config.spoof_permissions:
            parts.append(self._script_permissions_spoof())

        # ── 7. Chrome runtime ──
        if self.config.spoof_chrome_runtime:
            parts.append(self._script_chrome_runtime_spoof())

        # ── 8. Notification permission ──
        if self.config.spoof_notification_permission:
            parts.append(self._script_notification_spoof())

        # ── 9. iframe protection ──
        if self.config.protect_iframe_content_window:
            parts.append(self._script_iframe_protection())

        # ── 10. Connection spoofing ──
        if self.config.spoof_connection:
            parts.append(self._script_connection_spoof())

        # ── 11. Media devices ──
        if self.config.spoof_media_devices:
            parts.append(self._script_media_devices_spoof())

        # ── 12. Screen spoofing ──
        if self.config.spoof_screen:
            parts.append(self._script_screen_spoof())

        self._init_script = "\n".join(f"({part})();" for part in parts)
        return self._init_script

    def _script_remove_webdriver(self) -> str:
        return """
        () => {
            // Remove navigator.webdriver
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
                configurable: true,
            });

            // Also handle WebDriver BiDi
            delete navigator.webdriver;
        }
        """

    def _script_remove_automation(self) -> str:
        return """
        () => {
            // Remove window.chrome.cdc_* (ChromeDriver markers)
            if (window.chrome) {
                const cdcProps = Object.keys(window.chrome).filter(
                    k => k.match(/^cdc_/) || k.match(/^[a-z]{20}_[a-z]{20}$/i)
                );
                for (const prop of cdcProps) {
                    delete window.chrome[prop];
                }
            }

            // Remove __webdriver_evaluate, __selenium_evaluate, etc.
            const automationKeys = [
                '__webdriver_evaluate', '__selenium_evaluate',
                '__webdriver_unwrapped', '__driver_unwrapped',
                '__webdriver_script_fn', '__driver_evaluate',
                '__selenium_unwrapped', '__fxdriver_unwrapped',
                '__fxdriver_evaluate', '_Selenium_IDE_Recorder',
                '_selenium', 'calledSelenium', '__nightmare',
                '__phantomas', 'domAutomation', 'domAutomationController',
            ];
            for (const key of automationKeys) {
                Object.defineProperty(window, key, {
                    get: () => undefined,
                    configurable: true,
                });
                try { delete window[key]; } catch(e) {}
            }

            // Remove document.$cdc_* and document.$wdc_*
            for (const key of Object.keys(document)) {
                if (key.match(/^\\$[cw]dc_/)) {
                    Object.defineProperty(document, key, {
                        get: () => undefined,
                        configurable: true,
                    });
                }
            }
        }
        """

    def _script_remove_selenium(self) -> str:
        return """
        () => {
            const seleniumKeys = [
                '__selenium_unwrapped', '__webdriver_unwrapped',
                '__driver_unwrapped', '__webdriver_evaluate',
                '__driver_evaluate', '__selenium_evaluate',
                '__fxdriver_evaluate', '__fxdriver_unwrapped',
            ];
            for (const key of seleniumKeys) {
                Object.defineProperty(document, key, {
                    get: () => undefined,
                    configurable: true,
                });
            }
        }
        """

    def _script_remove_cdp(self) -> str:
        return """
        () => {
            // Hide Chrome DevTools Protocol detection
            const originalQuery = window.navigator.permissions?.query;
            if (originalQuery) {
                window.navigator.permissions.query = (parameters) =>
                    parameters.name === 'notifications'
                        ? Promise.resolve({ state: Notification.permission })
                        : originalQuery(parameters);
            }
        }
        """

    def _script_canvas_noise(self) -> str:
        amp = self.config.canvas_noise_amplitude
        return f"""
        () => {{
            const noiseAmplitude = {amp};

            const toDataURL = HTMLCanvasElement.prototype.toDataURL;
            HTMLCanvasElement.prototype.toDataURL = function(type) {{
                const context = this.getContext('2d');
                if (context) {{
                    const imageData = context.getImageData(0, 0, this.width, this.height);
                    const data = imageData.data;
                    for (let i = 0; i < data.length; i += 4) {{
                        data[i] = data[i] + (Math.random() - 0.5) * noiseAmplitude * 2;
                        data[i+1] = data[i+1] + (Math.random() - 0.5) * noiseAmplitude * 2;
                        data[i+2] = data[i+2] + (Math.random() - 0.5) * noiseAmplitude * 2;
                    }}
                    context.putImageData(imageData, 0, 0);
                }}
                return toDataURL.apply(this, arguments);
            }};

            const toBlob = HTMLCanvasElement.prototype.toBlob;
            HTMLCanvasElement.prototype.toBlob = function(callback, type) {{
                const context = this.getContext('2d');
                if (context) {{
                    const imageData = context.getImageData(0, 0, this.width, this.height);
                    const data = imageData.data;
                    for (let i = 0; i < data.length; i += 4) {{
                        data[i] = data[i] + (Math.random() - 0.5) * noiseAmplitude * 2;
                        data[i+1] = data[i+1] + (Math.random() - 0.5) * noiseAmplitude * 2;
                        data[i+2] = data[i+2] + (Math.random() - 0.5) * noiseAmplitude * 2;
                    }}
                    context.putImageData(imageData, 0, 0);
                }}
                return toBlob.apply(this, arguments);
            }};
        }}
        """

    def _script_webgl_spoof(self) -> str:
        profile = self._webgl_profile
        return f"""
        () => {{
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {{
                // UNMASKED_VENDOR_WEBGL
                if (parameter === 37445) return '{profile["unmasked_vendor"]}';
                // UNMASKED_RENDERER_WEBGL
                if (parameter === 37446) return '{profile["unmasked_renderer"]}';
                return getParameter.apply(this, arguments);
            }};

            const getExtension = WebGLRenderingContext.prototype.getExtension;
            WebGLRenderingContext.prototype.getExtension = function(name) {{
                const ext = getExtension.apply(this, arguments);
                if (name === 'WEBGL_debug_renderer_info') {{
                    return {{
                        UNMASKED_VENDOR_WEBGL: 37445,
                        UNMASKED_RENDERER_WEBGL: 37446,
                    }};
                }}
                return ext;
            }};

            // Also handle WebGL2
            if (typeof WebGL2RenderingContext !== 'undefined') {{
                const getParameter2 = WebGL2RenderingContext.prototype.getParameter;
                WebGL2RenderingContext.prototype.getParameter = function(parameter) {{
                    if (parameter === 37445) return '{profile["unmasked_vendor"]}';
                    if (parameter === 37446) return '{profile["unmasked_renderer"]}';
                    return getParameter2.apply(this, arguments);
                }};
            }}
        }}
        """

    def _script_audio_spoof(self) -> str:
        return """
        () => {
            const AudioContext = window.AudioContext || window.webkitAudioContext;
            if (AudioContext) {
                const originalCreateOscillator = AudioContext.prototype.createOscillator;
                AudioContext.prototype.createOscillator = function() {
                    const osc = originalCreateOscillator.apply(this, arguments);
                    // Add slight frequency jitter to make audio fingerprint unique
                    const originalFrequency = Object.getOwnPropertyDescriptor(
                        OscillatorNode.prototype, 'frequency'
                    );
                    if (originalFrequency && originalFrequency.get) {
                        const realFrequency = originalFrequency.get.call(osc);
                        // Inject tiny noise into the frequency AudioParam
                    }
                    return osc;
                };
            }
        }
        """

    def _script_plugins_spoof(self) -> str:
        return """
        () => {
            Object.defineProperty(navigator, 'plugins', {
                get: () => {
                    const plugins = [
                        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                        { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
                    ];
                    plugins.length = 3;
                    return plugins;
                },
                configurable: true,
            });

            Object.defineProperty(navigator, 'mimeTypes', {
                get: () => ({
                    length: 3,
                    0: { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
                    1: { type: 'application/x-nacl', suffixes: '', description: 'Native Client Executable' },
                    2: { type: 'application/x-pnacl', suffixes: '', description: 'Portable Native Client Executable' },
                }),
                configurable: true,
            });
        }
        """

    def _script_languages_spoof(self) -> str:
        return """
        () => {
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en'],
                configurable: true,
            });
            Object.defineProperty(navigator, 'language', {
                get: () => 'en-US',
                configurable: true,
            });
        }
        """

    def _script_platform_spoof(self) -> str:
        return """
        () => {
            Object.defineProperty(navigator, 'platform', {
                get: () => 'Win32',
                configurable: true,
            });
            Object.defineProperty(navigator, 'vendor', {
                get: () => 'Google Inc.',
                configurable: true,
            });
            Object.defineProperty(navigator, 'appVersion', {
                get: () => '5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                configurable: true,
            });
            Object.defineProperty(navigator, 'userAgent', {
                get: () => 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                configurable: true,
            });
        }
        """

    def _script_hardware_spoof(self) -> str:
        return """
        () => {
            Object.defineProperty(navigator, 'hardwareConcurrency', {
                get: () => 8,
                configurable: true,
            });
        }
        """

    def _script_device_memory_spoof(self) -> str:
        return """
        () => {
            Object.defineProperty(navigator, 'deviceMemory', {
                get: () => 8,
                configurable: true,
            });
        }
        """

    def _script_permissions_spoof(self) -> str:
        return """
        () => {
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => {
                if (parameters.name === 'notifications') {
                    return Promise.resolve({ state: 'default' });
                }
                return originalQuery(parameters);
            };
        }
        """

    def _script_chrome_runtime_spoof(self) -> str:
        return """
        () => {
            if (!window.chrome) window.chrome = {};
            if (!window.chrome.runtime) {
                window.chrome.runtime = {
                    connect: function() { return { onMessage: { addListener: function() {} } }; },
                    sendMessage: function() {},
                    onMessage: { addListener: function() {} },
                    id: undefined,
                };
            }
        }
        """

    def _script_notification_spoof(self) -> str:
        return """
        () => {
            const originalNotification = window.Notification;
            Object.defineProperty(window, 'Notification', {
                get: () => originalNotification,
                configurable: true,
            });
        }
        """

    def _script_iframe_protection(self) -> str:
        return """
        () => {
            // Prevent iframe contentWindow fingerprinting
            const originalAttachShadow = Element.prototype.attachShadow;
            Element.prototype.attachShadow = function() {
                const shadow = originalAttachShadow.apply(this, arguments);
                // Make shadow root appear as a regular element
                Object.defineProperty(shadow, 'host', {
                    get: () => this,
                });
                return shadow;
            };
        }
        """

    def _script_connection_spoof(self) -> str:
        return """
        () => {
            Object.defineProperty(navigator, 'connection', {
                get: () => ({
                    effectiveType: '4g',
                    rtt: 50,
                    downlink: 10,
                    saveData: false,
                    type: 'wifi',
                    onchange: null,
                    addEventListener: function() {},
                    removeEventListener: function() {},
                }),
                configurable: true,
            });
        }
        """

    def _script_media_devices_spoof(self) -> str:
        return """
        () => {
            if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
                const originalEnumerate = navigator.mediaDevices.enumerateDevices;
                navigator.mediaDevices.enumerateDevices = function() {
                    return originalEnumerate.apply(this, arguments).then(devices => {
                        // Return a realistic set of devices
                        return devices.length > 0 ? devices : [
                            { deviceId: 'default', kind: 'audioinput', label: '', groupId: 'default' },
                            { deviceId: 'communications', kind: 'audioinput', label: '', groupId: 'communications' },
                            { deviceId: 'default', kind: 'audiooutput', label: '', groupId: 'default' },
                        ];
                    });
                };
            }
        }
        """

    def _script_screen_spoof(self) -> str:
        w = self.config.screen_width
        h = self.config.screen_height
        cd = self.config.color_depth
        pr = self.config.pixel_ratio
        return f"""
        () => {{
            Object.defineProperty(screen, 'width', {{ get: () => {w}, configurable: true }});
            Object.defineProperty(screen, 'height', {{ get: () => {h}, configurable: true }});
            Object.defineProperty(screen, 'availWidth', {{ get: () => {w}, configurable: true }});
            Object.defineProperty(screen, 'availHeight', {{ get: () => {h - 40}, configurable: true }});
            Object.defineProperty(screen, 'colorDepth', {{ get: () => {cd}, configurable: true }});
            Object.defineProperty(screen, 'pixelDepth', {{ get: () => {cd}, configurable: true }});
            Object.defineProperty(window, 'devicePixelRatio', {{ get: () => {pr}, configurable: true }});
            Object.defineProperty(window, 'innerWidth', {{ get: () => {w}, configurable: true }});
            Object.defineProperty(window, 'innerHeight', {{ get: () => {h - 80}, configurable: true }});
            Object.defineProperty(window, 'outerWidth', {{ get: () => {w}, configurable: true }});
            Object.defineProperty(window, 'outerHeight', {{ get: () => {h}, configurable: true }});
        }}
        """

    def get_chromium_launch_args(self) -> List[str]:
        """Additional Chromium arguments for stealth."""
        return [
            '--disable-blink-features=AutomationControlled',
            '--disable-component-extensions-with-background-pages',
            '--disable-default-apps',
            '--disable-extensions',
            '--disable-hang-monitor',
            '--disable-popup-blocking',
            '--disable-prompt-on-repost',
            '--disable-sync',
            '--disable-translate',
            '--metrics-recording-only',
            '--no-first-run',
            '--no-default-browser-check',
            '--password-store=basic',
            '--use-mock-keychain',
            '--flag-switches-begin',
            '--flag-switches-end',
        ]

    async def apply_to_context(self, context: Any) -> None:
        """Apply all stealth measures to a Playwright BrowserContext."""
        script = self.build_init_script()
        await context.add_init_script(script)
        logger.debug(f"BrowserStealth: injected init script ({len(script)} bytes)")


# Global singleton
_stealth: Optional[BrowserStealth] = None


def get_browser_stealth() -> BrowserStealth:
    global _stealth
    if _stealth is None:
        _stealth = BrowserStealth()
    return _stealth
