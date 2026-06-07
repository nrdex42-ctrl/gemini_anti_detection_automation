import sys
import types


if "aiohttp" not in sys.modules:
    aiohttp_stub = types.ModuleType("aiohttp")
    aiohttp_stub.ClientSession = object
    aiohttp_stub.web = types.SimpleNamespace()
    sys.modules["aiohttp"] = aiohttp_stub

from telegram_bot import selected_public_base_url


def test_render_runtime_url_replaces_stale_onrender_public_base(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://anti-detection-fb-automation.onrender.com")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://anti-detection-fb-automation-3u1v.onrender.com")
    monkeypatch.delenv("PUBLIC_BASE_URL_FORCE", raising=False)

    url, source = selected_public_base_url()

    assert url == "https://anti-detection-fb-automation-3u1v.onrender.com"
    assert source == "RENDER_EXTERNAL_URL_STALE_PUBLIC_BASE_URL"


def test_custom_public_base_url_is_kept(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://bot.example.com")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://anti-detection-fb-automation-3u1v.onrender.com")

    url, source = selected_public_base_url()

    assert url == "https://bot.example.com"
    assert source == "PUBLIC_BASE_URL"


def test_render_runtime_url_is_used_when_public_base_missing(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://anti-detection-fb-automation-3u1v.onrender.com")

    url, source = selected_public_base_url()

    assert url == "https://anti-detection-fb-automation-3u1v.onrender.com"
    assert source == "RENDER_EXTERNAL_URL"
