"""Helpers for normalizing Facebook Page display names."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


_BLOCKED_NAMES = {
    "ad center",
    "advertise",
    "boost",
    "create ad",
    "create page",
    "create post",
    "manage posts",
    "meta business suite",
    "notifications",
    "post",
    "professional dashboard",
    "promote",
}


_WRAPPER_PATTERNS = (
    r"^\s*Profile picture (?:for|of)\s+(.+?)\s*$",
    r"^\s*(.+?)\s+profile picture\s*$",
    r"^\s*Photo de profil de\s+(.+?)\s*$",
    r"^\s*Foto del perfil de\s+(.+?)\s*$",
    r"^\s*Foto de perfil de\s+(.+?)\s*$",
)


def _compact(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _slug_name_from_url(page_url: str) -> str:
    try:
        parsed = urlparse(page_url)
    except Exception:
        return ""
    path_parts = [unquote(part) for part in parsed.path.split("/") if part]
    if not path_parts:
        return ""
    if path_parts[0].lower() == "profile.php":
        return ""
    if len(path_parts) >= 2 and path_parts[0].lower() in {"pages", "pg"}:
        candidate = path_parts[1]
    else:
        candidate = path_parts[0]
    candidate = candidate.replace("-", " ").replace("_", " ").strip()
    return _compact(candidate)


def page_id_from_url(page_url: str) -> str:
    try:
        parsed = urlparse(page_url)
        if parsed.path == "/profile.php":
            return (parse_qs(parsed.query).get("id") or [""])[0].strip()
    except Exception:
        return ""
    return ""


def clean_facebook_page_name(raw_name: Any, page_url: str = "", fallback: str = "") -> str:
    """Return a user-facing page name, stripping profile-photo alt text wrappers."""
    text = _compact(raw_name)
    for pattern in _WRAPPER_PATTERNS:
        match = re.match(pattern, text, re.I)
        if match:
            text = _compact(match.group(1))
            break
    text = re.sub(
        r"\s+(?:profile picture|photo de profil|foto del perfil|foto de perfil)$",
        "",
        text,
        flags=re.I,
    ).strip()
    text = re.sub(r"^\d+\s*-\s*", "", text).strip()
    lower_text = text.lower()
    if not text or text.isdigit() or lower_text in _BLOCKED_NAMES:
        text = _slug_name_from_url(page_url)
    if not text or text.isdigit() or text.lower() in _BLOCKED_NAMES:
        text = _compact(fallback)
    return text
