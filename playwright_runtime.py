"""
Small Playwright runtime helpers that are safe to import before Playwright.
"""

import os
from pathlib import Path

from dotenv import load_dotenv


def default_playwright_browsers_path() -> str:
    docker_path = '/ms-playwright'
    if os.path.isdir(docker_path):
        return docker_path
    configured_fallback = os.getenv('PLAYWRIGHT_FALLBACK_BROWSERS_PATH', '').strip()
    if configured_fallback:
        return configured_fallback
    if os.getenv('RENDER', '').lower() == 'true' or str(Path.cwd()).startswith('/opt/render/project/src'):
        return str(Path.cwd() / '.playwright')
    if os.getenv('USE_TEMP_PLAYWRIGHT', '').lower() == 'true':
        return '/tmp/playwright_browsers'
    return ''


def configure_playwright_browsers_path() -> None:
    load_dotenv()
    if not os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '').strip():
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = default_playwright_browsers_path()
    browsers_path = os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '').strip()
    if browsers_path and browsers_path != '/ms-playwright':
        Path(browsers_path).mkdir(parents=True, exist_ok=True)
