"""Proxy URL helpers shared by the Telegram bot and Playwright engine."""

from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import unquote, urlparse


SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks5"}


def normalize_proxy_url(value: Any) -> str:
    """Validate and normalize a proxy URL without testing network reachability."""
    raw = " ".join(str(value or "").strip().split())
    if not raw:
        return ""
    lowered = raw.lower()
    if lowered in {"none", "clear", "off", "disable", "disabled"}:
        return ""
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()
    if scheme not in SUPPORTED_PROXY_SCHEMES:
        allowed = ", ".join(sorted(SUPPORTED_PROXY_SCHEMES))
        raise ValueError(f"Proxy scheme must be one of: {allowed}")
    if not parsed.hostname:
        raise ValueError("Proxy host is required")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Proxy port is invalid") from exc
    if not port:
        raise ValueError("Proxy port is required")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("Proxy URL must not include a path, query, or fragment")
    return parsed.geturl()


def proxy_display_url(value: Any) -> str:
    proxy_url = normalize_proxy_url(value)
    if not proxy_url:
        return ""
    parsed = urlparse(proxy_url)
    return f"{parsed.scheme.lower()}://{parsed.hostname}:{parsed.port}"


def proxy_is_configured(account: Dict[str, Any]) -> bool:
    value = account.get("proxy_configured")
    if isinstance(value, bool):
        return value
    if value is not None:
        return str(value).strip().lower() in {"1", "true", "yes", "on", "set"}
    return bool(str(account.get("proxy_url") or "").strip())


def requests_proxy_config(value: Any) -> Optional[Dict[str, str]]:
    proxy_url = normalize_proxy_url(value)
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def playwright_proxy_config(value: Any) -> Optional[Dict[str, str]]:
    proxy_url = normalize_proxy_url(value)
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    config: Dict[str, str] = {
        "server": f"{parsed.scheme.lower()}://{parsed.hostname}:{parsed.port}",
    }
    if parsed.username:
        config["username"] = unquote(parsed.username)
    if parsed.password:
        config["password"] = unquote(parsed.password)
    return config
