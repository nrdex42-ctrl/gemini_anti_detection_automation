#!/usr/bin/env python3
"""Register the Render webhook URL with Telegram."""

from __future__ import annotations

import os
import sys

import requests


def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
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

    webhook_url = f"{public_base_url}/telegram/webhook"
    response = requests.post(
        f"https://api.telegram.org/bot{token}/setWebhook",
        json={
            "url": webhook_url,
            "secret_token": secret,
            "drop_pending_updates": True,
            "allowed_updates": ["message", "edited_message"],
        },
        timeout=30,
    )
    print(response.text)
    response.raise_for_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
