#!/usr/bin/env python3
"""Register the Render webhook URL with Telegram."""

from __future__ import annotations

import os
import sys
import base64
import hashlib
import re
from urllib.parse import urlparse

import requests


def telegram_safe_webhook_secret(secret: str) -> str:
    raw = (secret or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{1,256}", raw):
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def placeholder(value: str) -> bool:
    normalized = (value or "").strip().lower()
    return (
        not normalized
        or "replace_me" in normalized
        or "replace-me" in normalized
        or normalized.startswith("your-")
    )


def normalize_base_url(value: str) -> str:
    raw = (value or "").strip().rstrip("/")
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    return raw.rstrip("/")


def hostname(value: str) -> str:
    try:
        return (urlparse(value).hostname or "").lower()
    except Exception:
        return ""


def is_onrender_url(value: str) -> bool:
    host = hostname(value)
    return host == "onrender.com" or host.endswith(".onrender.com")


def render_runtime_url() -> str:
    runtime_url = normalize_base_url(os.getenv("RENDER_EXTERNAL_URL", ""))
    if runtime_url and not placeholder(runtime_url):
        return runtime_url
    runtime_host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
    if runtime_host and not placeholder(runtime_host):
        return normalize_base_url(runtime_host)
    return ""


def selected_public_base_url() -> tuple[str, str]:
    configured_url = normalize_base_url(os.getenv("PUBLIC_BASE_URL", ""))
    runtime_url = render_runtime_url()
    if placeholder(configured_url):
        return (runtime_url, "RENDER_EXTERNAL_URL") if runtime_url else ("", "missing")
    if (
        runtime_url
        and is_onrender_url(configured_url)
        and is_onrender_url(runtime_url)
        and hostname(configured_url) != hostname(runtime_url)
        and os.getenv("PUBLIC_BASE_URL_FORCE", "").strip().lower() not in {"1", "true", "yes", "on"}
    ):
        return runtime_url, "RENDER_EXTERNAL_URL_STALE_PUBLIC_BASE_URL"
    return configured_url, "PUBLIC_BASE_URL"


def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    public_base_url, source = selected_public_base_url()
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN is required", file=sys.stderr)
        return 2
    if not public_base_url:
        print("PUBLIC_BASE_URL is required, for example https://your-service.onrender.com", file=sys.stderr)
        return 2
    if not secret:
        print("TELEGRAM_WEBHOOK_SECRET is required", file=sys.stderr)
        return 2

    safe_secret = telegram_safe_webhook_secret(secret)
    webhook_url = f"{public_base_url}/telegram/webhook"
    print(f"Using public base URL from {source}: {public_base_url}", file=sys.stderr)
    response = requests.post(
        f"https://api.telegram.org/bot{token}/setWebhook",
        json={
            "url": webhook_url,
            "secret_token": safe_secret,
            "drop_pending_updates": True,
            "allowed_updates": ["message", "edited_message", "callback_query"],
        },
        timeout=30,
    )
    print(response.text)
    response.raise_for_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
