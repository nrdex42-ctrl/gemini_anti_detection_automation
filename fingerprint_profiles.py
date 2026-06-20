"""Atomic fingerprint profile definitions — version-locked and internally consistent.

Every field must agree with every other field for a given profile. Never mix
values across profiles — Facebook's anti-bot layer specifically looks for
internal inconsistencies.

Profiles are frozen dataclasses, registered in the PROFILES dict by ID.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class FingerprintProfile:
    """Atomic fingerprint unit. All fields must agree with the impersonation target."""
    id: str
    impersonate_target: str
    user_agent: str
    sec_ch_ua: str
    sec_ch_ua_platform: str
    sec_ch_ua_mobile: str
    sec_ch_ua_arch: str
    sec_ch_ua_bitness: str
    sec_ch_ua_full_version_list: str
    sec_ch_ua_platform_version: str
    sec_ch_ua_model: str
    accept_language: str
    navigator_platform: str
    screen_width: int
    screen_height: int
    hardware_concurrency: int
    window_dims: str
    device_scale_factor: float
    locale: str


PROFILES: Dict[str, FingerprintProfile] = {
    "chrome_120_windows_x64": FingerprintProfile(
        id="chrome_120_windows_x64",
        impersonate_target="chrome120",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        sec_ch_ua=(
            '"Not_A Brand";v="8", "Chromium";v="120", '
            '"Google Chrome";v="120"'
        ),
        sec_ch_ua_platform='"Windows"',
        sec_ch_ua_mobile="?0",
        sec_ch_ua_arch='"x86"',
        sec_ch_ua_bitness='"64"',
        sec_ch_ua_full_version_list=(
            '"Not_A Brand";v="8.0.0.0", '
            '"Chromium";v="120.0.6099.130", '
            '"Google Chrome";v="120.0.6099.130"'
        ),
        sec_ch_ua_platform_version='"15.0.0"',
        sec_ch_ua_model='""',
        accept_language="en-US,en;q=0.9",
        navigator_platform="Win32",
        screen_width=1920,
        screen_height=1080,
        hardware_concurrency=8,
        window_dims="1920x946",
        device_scale_factor=1.25,
        locale="en_US",
    ),
    "chrome_120_macos_arm64": FingerprintProfile(
        id="chrome_120_macos_arm64",
        impersonate_target="chrome120",
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        sec_ch_ua=(
            '"Not_A Brand";v="8", "Chromium";v="120", '
            '"Google Chrome";v="120"'
        ),
        sec_ch_ua_platform='"macOS"',
        sec_ch_ua_mobile="?0",
        sec_ch_ua_arch='"arm"',
        sec_ch_ua_bitness='"64"',
        sec_ch_ua_full_version_list=(
            '"Not_A Brand";v="8.0.0.0", '
            '"Chromium";v="120.0.6099.130", '
            '"Google Chrome";v="120.0.6099.130"'
        ),
        sec_ch_ua_platform_version='"14.2.1"',
        sec_ch_ua_model='""',
        accept_language="en-US,en;q=0.9",
        navigator_platform="MacIntel",
        screen_width=1920,
        screen_height=1080,
        hardware_concurrency=8,
        window_dims="1920x946",
        device_scale_factor=2.0,
        locale="en_US",
    ),
    "chrome_120_linux_x64": FingerprintProfile(
        id="chrome_120_linux_x64",
        impersonate_target="chrome120",
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        sec_ch_ua=(
            '"Not_A Brand";v="8", "Chromium";v="120", '
            '"Google Chrome";v="120"'
        ),
        sec_ch_ua_platform='"Linux"',
        sec_ch_ua_mobile="?0",
        sec_ch_ua_arch='"x86"',
        sec_ch_ua_bitness='"64"',
        sec_ch_ua_full_version_list=(
            '"Not_A Brand";v="8.0.0.0", '
            '"Chromium";v="120.0.6099.130", '
            '"Google Chrome";v="120.0.6099.130"'
        ),
        sec_ch_ua_platform_version='""',
        sec_ch_ua_model='""',
        accept_language="en-US,en;q=0.9",
        navigator_platform="Linux x86_64",
        screen_width=1920,
        screen_height=1080,
        hardware_concurrency=8,
        window_dims="1920x946",
        device_scale_factor=1.0,
        locale="en_US",
    ),
}


def get_profile(profile_id: str) -> FingerprintProfile:
    """Look up a profile by ID. Raises KeyError if not found."""
    return PROFILES[profile_id]


def profile_to_fingerprint_dict(profile: FingerprintProfile) -> dict:
    """Convert a FingerprintProfile to the dict format expected by FBClient."""
    return {
        "impersonate": profile.impersonate_target,
        "user_agent": profile.user_agent,
        "sec_ch_ua": profile.sec_ch_ua,
        "sec_ch_ua_mobile": profile.sec_ch_ua_mobile,
        "sec_ch_ua_platform": profile.sec_ch_ua_platform,
        "sec_ch_ua_arch": profile.sec_ch_ua_arch,
        "sec_ch_ua_bitness": profile.sec_ch_ua_bitness,
        "sec_ch_ua_full_version_list": profile.sec_ch_ua_full_version_list,
        "sec_ch_ua_platform_version": profile.sec_ch_ua_platform_version,
        "sec_ch_ua_model": profile.sec_ch_ua_model,
        "screen_width": profile.screen_width,
        "screen_height": profile.screen_height,
        "locale": profile.locale,
        "platform": profile.navigator_platform,
    }
