"""
Session Manager
Tracks Facebook cookie-session usage and cooldowns after security failures.
"""

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - python-dotenv is optional in minimal installs.
    load_dotenv = None

try:
    import redis as redis_module
except Exception:  # pragma: no cover - Redis is optional for local runs.
    redis_module = None

logger = logging.getLogger(__name__)

_PUBLISH_SENT_UNCONFIRMED_MARKER = 'publish_sent_unconfirmed'
_SECURITY_ERROR_RE = re.compile(
    r'locked|checkpoint|account restricted|temporarily blocked|confirm your identity|'
    r'unusual activity|trusted device|suspended|unlock|hacked|recover/initiate|'
    r'login_identify|cookies? expired|invalid cookies?|session expired|session invalid|'
    r'login required|log in|required to log|not logged in|logged out|auth(?:entication)? failure|'
    r'قفل|تحقق|تأكيد|مخترق|تسجيل الدخول|انتهت الجلسة',
    re.I,
)

if load_dotenv is not None:
    load_dotenv()


class SessionManager:
    """Persist lightweight safety state for Facebook cookie sessions."""

    def __init__(self, sessions_file: str = 'session_states.json'):
        self.sessions_file = Path(sessions_file)
        self.redis_url = os.getenv('REDIS_URL', '').strip()
        self._redis_client = None
        self._lock = threading.Lock()
        self._states: Dict[str, Dict[str, Any]] = {}
        self._load_states()

    def _load_states(self) -> None:
        try:
            if self.sessions_file.exists():
                raw = json.loads(self.sessions_file.read_text(encoding='utf-8'))
                self._states = raw if isinstance(raw, dict) else {}
        except Exception as exc:
            logger.warning(f'Failed to load session states: {exc}')
            self._states = {}

    def _save_states(self) -> None:
        try:
            self.sessions_file.write_text(
                json.dumps(self._states, ensure_ascii=False, indent=2),
                encoding='utf-8',
            )
        except Exception as exc:
            logger.warning(f'Failed to save session states: {exc}')

    def _get_redis_client(self):
        current_url = os.getenv('REDIS_URL', '').strip()
        if current_url != self.redis_url:
            self.redis_url = current_url
            self._redis_client = None
        if not current_url or redis_module is None:
            return None
        if self._redis_client is None:
            self._redis_client = redis_module.from_url(
                current_url,
                socket_connect_timeout=5,
                socket_timeout=5,
                decode_responses=True,
            )
        return self._redis_client

    @staticmethod
    def _redis_state_key(session_key: str) -> str:
        return f'fb-cookie-session-state:{session_key}'

    def _read_state(self, session_key: str) -> Dict[str, Any]:
        client = self._get_redis_client()
        if client is not None:
            try:
                raw = client.get(self._redis_state_key(session_key))
                if raw:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        return parsed
            except Exception as exc:
                logger.warning(f'Failed to read Redis session state; using local state: {exc}')
        return dict(self._states.get(session_key, {}))

    def _write_state(self, session_key: str, state: Dict[str, Any]) -> None:
        self._states[session_key] = dict(state)
        client = self._get_redis_client()
        if client is not None:
            try:
                client.set(
                    self._redis_state_key(session_key),
                    json.dumps(state, ensure_ascii=False),
                )
            except Exception as exc:
                logger.warning(f'Failed to write Redis session state; using local state only: {exc}')
        self._save_states()

    def _get_session_key(self, cookies_json: str) -> str:
        try:
            cookies = json.loads(cookies_json)
            if isinstance(cookies, list):
                c_user = next(
                    (
                        str(cookie.get('value') or '')
                        for cookie in cookies
                        if isinstance(cookie, dict) and cookie.get('name') == 'c_user'
                    ),
                    '',
                )
                xs = next(
                    (
                        str(cookie.get('value') or '')
                        for cookie in cookies
                        if isinstance(cookie, dict) and cookie.get('name') == 'xs'
                    ),
                    '',
                )
                if c_user:
                    return hashlib.sha256(f'{c_user}:{xs[:32]}'.encode('utf-8')).hexdigest()
        except Exception:
            pass
        return hashlib.sha256(cookies_json[:4096].encode('utf-8')).hexdigest()

    @staticmethod
    def _is_security_error(error: str) -> bool:
        error_text = str(error or '')
        if _PUBLISH_SENT_UNCONFIRMED_MARKER in error_text:
            # Batch code appends generated skip text after the first page result.
            # Only the originating failure should decide whether a cookie cooldown is needed.
            error_text = error_text.split(';', 1)[0]
        return bool(_SECURITY_ERROR_RE.search(error_text))

    def can_use_session(
        self,
        cookies_json: str,
        *,
        min_interval_seconds: int = 0,
        security_cooldown_seconds: int = 3600,
    ) -> Tuple[bool, str]:
        """Return whether this cookie session can safely be used now."""
        cooldown = self.get_session_cooldown(
            cookies_json,
            min_interval_seconds=min_interval_seconds,
            security_cooldown_seconds=security_cooldown_seconds,
        )
        if cooldown.get('active'):
            return False, str(cooldown.get('reason') or 'Session cooldown is active.')
        return True, 'OK'

    def get_session_cooldown(
        self,
        cookies_json: str,
        *,
        min_interval_seconds: int = 0,
        security_cooldown_seconds: int = 3600,
    ) -> Dict[str, Any]:
        """Return structured cooldown details for UI preflight checks."""
        session_key = self._get_session_key(cookies_json)
        now = time.time()
        with self._lock:
            state = self._read_state(session_key)
            if (
                bool(state.get('last_error_is_security'))
                and not self._is_security_error(str(state.get('last_error') or ''))
            ):
                state['last_error_is_security'] = False
                state['security_flag_cleared_at'] = now
                self._write_state(session_key, state)

        last_used = float(state.get('last_used') or 0)
        if min_interval_seconds > 0 and last_used:
            time_since_last = now - last_used
            if time_since_last < min_interval_seconds:
                wait_time = max(1, int(math.ceil(min_interval_seconds - time_since_last)))
                return {
                    'active': True,
                    'kind': 'min_interval',
                    'remaining_seconds': wait_time,
                    'reason': f'Must wait {wait_time}s before reusing this Facebook cookie session.',
                }

        last_error = str(state.get('last_error') or '')
        error_time = float(state.get('error_time') or 0)
        if (
            last_error
            and bool(state.get('last_error_is_security'))
            and security_cooldown_seconds > 0
            and now - error_time < security_cooldown_seconds
        ):
            occurred_at = datetime.fromtimestamp(error_time).isoformat()
            remaining = max(1, int(math.ceil(security_cooldown_seconds - (now - error_time))))
            return {
                'active': True,
                'kind': 'security',
                'remaining_seconds': remaining,
                'reason': (
                    f'Facebook security issue was detected at {occurred_at}: {last_error}. '
                    'Unlock the account manually and re-add fresh cookies before posting again.'
                ),
            }

        return {
            'active': False,
            'kind': '',
            'remaining_seconds': 0,
            'reason': 'OK',
        }

    def mark_session_used(self, cookies_json: str, success: bool, error: str = '') -> None:
        """Record the latest posting result for this cookie session."""
        session_key = self._get_session_key(cookies_json)
        now = time.time()
        with self._lock:
            state = self._read_state(session_key)
            state.setdefault('created_at', now)
            state['last_used'] = now
            state['use_count'] = int(state.get('use_count') or 0) + 1

            if success:
                state['success_count'] = int(state.get('success_count') or 0) + 1
                state.pop('last_error', None)
                state.pop('error_time', None)
                state.pop('last_error_is_security', None)
            else:
                error_text = str(error or 'Unknown posting failure')[:300]
                state['error_count'] = int(state.get('error_count') or 0) + 1
                state['last_error'] = error_text
                state['error_time'] = now
                state['last_error_is_security'] = self._is_security_error(error_text)

            self._write_state(session_key, state)

    def get_session_stats(self, cookies_json: str) -> Dict[str, Any]:
        session_key = self._get_session_key(cookies_json)
        with self._lock:
            return self._read_state(session_key)

    def reset_session(self, cookies_json: str) -> None:
        session_key = self._get_session_key(cookies_json)
        with self._lock:
            state = self._read_state(session_key)
            state.setdefault('created_at', time.time())
            state['use_count'] = 0
            state['reset_at'] = time.time()
            state.pop('last_used', None)
            state.pop('last_error', None)
            state.pop('error_time', None)
            state.pop('last_error_is_security', None)
            self._write_state(session_key, state)


session_manager = SessionManager()
