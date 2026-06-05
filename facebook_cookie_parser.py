"""Facebook cookie payload parsing helpers for Telegram ingestion.

The bot stores cookies as a raw Cookie header string because the posting
engine already consumes that format. This module accepts the common operator
input formats and normalizes them before storage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List


COOKIE_HINT_KEYS = ("c_user", "i_user", "xs")


@dataclass(frozen=True)
class ParsedCookiePayload:
    cookies: List[Dict[str, Any]]
    cookie_header: str
    account_id: str


def _normalize_cookie(cookie: Dict[str, Any]) -> Dict[str, Any]:
    name = str(cookie.get("name") or "").strip()
    value = str(cookie.get("value") or "")
    if not name:
        raise ValueError("Cookie entry has no name")

    normalized: Dict[str, Any] = {
        "name": name,
        "value": value,
        "domain": str(cookie.get("domain") or ".facebook.com"),
        "path": str(cookie.get("path") or "/"),
    }

    for key in ("expires", "expirationDate", "httpOnly", "secure", "sameSite"):
        if key in cookie and cookie[key] is not None:
            normalized[key] = cookie[key]

    return normalized


def parse_cookie_json_payload(payload: str) -> List[Dict[str, Any]]:
    parsed = json.loads(payload)
    if isinstance(parsed, dict) and isinstance(parsed.get("cookies"), list):
        parsed = parsed["cookies"]
    if not isinstance(parsed, list):
        raise ValueError("Cookies must be a JSON array or an object with a cookies array.")

    cookies: List[Dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            raise ValueError("Each cookie entry must be an object.")
        try:
            cookies.append(_normalize_cookie(item))
        except ValueError:
            continue

    if not cookies:
        raise ValueError("No cookies found in JSON payload.")
    return cookies


def parse_cookie_header_payload(payload: str) -> List[Dict[str, Any]]:
    normalized = payload.strip().replace("\r", "\n").replace("\n", ";")
    cookies: List[Dict[str, Any]] = []
    for raw_part in normalized.split(";"):
        part = raw_part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if not name:
            continue
        cookies.append(
            {
                "name": name,
                "value": value.strip(),
                "domain": ".facebook.com",
                "path": "/",
            }
        )

    if not cookies:
        raise ValueError("No cookies found in cookie string.")
    return cookies


def parse_cookie_payload(payload: str) -> List[Dict[str, Any]]:
    payload = (payload or "").strip()
    if not payload:
        raise ValueError("Cookie payload is empty.")

    try:
        return parse_cookie_json_payload(payload)
    except Exception:
        return parse_cookie_header_payload(payload)


def cookies_to_header(cookies: List[Dict[str, Any]]) -> str:
    parts = []
    for cookie in cookies:
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        if name:
            parts.append(f"{name}={value}")
    if not parts:
        raise ValueError("No usable cookie name/value pairs were found.")
    return "; ".join(parts)


def extract_account_id(cookies: List[Dict[str, Any]], payload: str = "", fallback: str = "") -> str:
    values = {
        str(cookie.get("name") or "").strip(): str(cookie.get("value") or "").strip()
        for cookie in cookies
        if str(cookie.get("name") or "").strip()
    }
    for key in COOKIE_HINT_KEYS:
        if values.get(key):
            return values[key]

    payload_values: Dict[str, str] = {}
    for raw_part in (payload or "").replace("\n", ";").split(";"):
        part = raw_part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        payload_values[name.strip()] = value.strip()
    for key in COOKIE_HINT_KEYS:
        if payload_values.get(key):
            return payload_values[key]

    return fallback.strip()


def parse_account_cookie_payload(payload: str, account_hint: str = "") -> ParsedCookiePayload:
    cookies = parse_cookie_payload(payload)
    account_id = extract_account_id(cookies, payload, "" if account_hint in {"auto", "-"} else account_hint)
    if not account_id:
        raise ValueError("Could not determine account id. Include c_user or pass an account id.")
    return ParsedCookiePayload(cookies=cookies, cookie_header=cookies_to_header(cookies), account_id=account_id)
