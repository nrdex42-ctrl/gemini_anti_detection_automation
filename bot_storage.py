"""Supabase/Postgres storage helpers for the Telegram bot."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg
from cryptography.fernet import Fernet, InvalidToken
from psycopg.rows import dict_row


TOKEN_PREFIX = "fernet:"


class SecretCipher:
    """Encrypt stored Facebook cookies when ENCRYPTION_KEY is configured."""

    def __init__(self, key: str = "") -> None:
        raw = (key or os.getenv("ENCRYPTION_KEY", "")).strip()
        self._fernet: Optional[Fernet] = None
        if not raw:
            return
        key_bytes = raw.encode("utf-8")
        try:
            self._fernet = Fernet(key_bytes)
        except Exception:
            derived = base64.urlsafe_b64encode(hashlib.sha256(key_bytes).digest())
            self._fernet = Fernet(derived)

    def encrypt(self, value: str) -> str:
        if not value or value.startswith(TOKEN_PREFIX) or self._fernet is None:
            return value
        return f"{TOKEN_PREFIX}{self._fernet.encrypt(value.encode('utf-8')).decode('utf-8')}"

    def decrypt(self, value: str) -> str:
        if not value or not value.startswith(TOKEN_PREFIX):
            return value
        if self._fernet is None:
            raise RuntimeError("ENCRYPTION_KEY is required to decrypt stored cookies")
        try:
            return self._fernet.decrypt(value[len(TOKEN_PREFIX):].encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError("Stored cookie could not be decrypted with this ENCRYPTION_KEY") from exc


class BotStorage:
    def __init__(self, database_url: str, cipher: Optional[SecretCipher] = None) -> None:
        if not database_url:
            raise RuntimeError("DATABASE_URL is required")
        self.database_url = database_url
        self.cipher = cipher or SecretCipher()

    @classmethod
    def from_env(cls) -> "BotStorage":
        return cls(os.getenv("DATABASE_URL", "").strip())

    def connect(self):
        return psycopg.connect(self.database_url, connect_timeout=15, row_factory=dict_row)

    def ensure_schema(self) -> None:
        schema_path = Path(__file__).resolve().parent / "supabase" / "schema.sql"
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(schema_path.read_text(encoding="utf-8"))
            conn.commit()

    def upsert_account(self, account_id: str, cookie_string: str, label: str = "", created_by: int = 0) -> None:
        encrypted_cookie = self.cipher.encrypt(cookie_string)
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into fb_accounts (account_id, label, cookie_ciphertext, created_by, updated_at)
                    values (%s, %s, %s, %s, now())
                    on conflict (account_id) do update set
                        label = excluded.label,
                        cookie_ciphertext = excluded.cookie_ciphertext,
                        active = true,
                        updated_at = now()
                    """,
                    (account_id, label or account_id, encrypted_cookie, created_by or None),
                )
            conn.commit()

    def deactivate_account(self, account_id: str) -> bool:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("update fb_accounts set active=false, updated_at=now() where account_id=%s", (account_id,))
                changed = cur.rowcount > 0
            conn.commit()
        return changed

    def list_accounts(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select account_id, label, active, created_at, updated_at
                    from fb_accounts
                    order by updated_at desc
                    """
                )
                return list(cur.fetchall())

    def get_account_cookie(self, account_id: str) -> str:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "select cookie_ciphertext from fb_accounts where account_id=%s and active=true",
                    (account_id,),
                )
                row = cur.fetchone()
        if not row:
            raise RuntimeError(f"Active account not found: {account_id}")
        return self.cipher.decrypt(str(row["cookie_ciphertext"]))

    def upsert_pages(self, account_id: str, pages: List[Dict[str, str]]) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                for page in pages:
                    page_id = str(page.get("id") or "").strip()
                    page_url = str(page.get("url") or "").strip()
                    page_name = str(page.get("name") or page_id or page_url).strip()
                    if not page_id and "id=" in page_url:
                        page_id = page_url.split("id=", 1)[1].split("&", 1)[0]
                    if not page_id:
                        page_id = hashlib.sha256(page_url.encode("utf-8")).hexdigest()[:24]
                    cur.execute(
                        """
                        insert into fb_pages (account_id, page_id, page_name, page_url, updated_at)
                        values (%s, %s, %s, %s, now())
                        on conflict (account_id, page_id) do update set
                            page_name = excluded.page_name,
                            page_url = excluded.page_url,
                            updated_at = now()
                        """,
                        (account_id, page_id, page_name, page_url),
                    )
            conn.commit()

    def list_pages(self, account_id: str) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select page_id, page_name, page_url, updated_at
                    from fb_pages
                    where account_id=%s
                    order by page_name, page_id
                    """,
                    (account_id,),
                )
                return list(cur.fetchall())

    def dashboard_summary(self) -> Dict[str, Any]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("select count(*)::int as page_count from fb_pages")
                page_row = cur.fetchone() or {}

                cur.execute(
                    """
                    select status, count(*)::int as count
                    from fb_post_jobs
                    group by status
                    """
                )
                status_counts = {str(row["status"]): int(row["count"]) for row in cur.fetchall()}

                cur.execute(
                    """
                    select account_id, last_cookie_used_at, locked_until, locked_by
                    from fb_account_runtime
                    where locked_until is not null and locked_until > now()
                    order by locked_until desc
                    limit 10
                    """
                )
                locked_accounts = list(cur.fetchall())

                cur.execute(
                    """
                    select id::text, account_id, page_id_or_url, post_type, status, created_at
                    from fb_post_jobs
                    order by created_at desc
                    limit 8
                    """
                )
                recent_jobs = list(cur.fetchall())

        return {
            "page_count": int(page_row.get("page_count") or 0),
            "job_status_counts": status_counts,
            "locked_accounts": locked_accounts,
            "recent_jobs": recent_jobs,
        }

    def create_post_job(
        self,
        *,
        telegram_chat_id: int,
        telegram_user_id: int,
        account_id: str,
        page_id_or_url: str,
        page_name: str,
        post_type: str,
        caption: str,
        media_path: str = "",
    ) -> str:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into fb_post_jobs (
                        telegram_chat_id, telegram_user_id, account_id, page_id_or_url,
                        page_name, post_type, caption, media_path
                    )
                    values (%s, %s, %s, %s, %s, %s, %s, %s)
                    returning id::text
                    """,
                    (
                        telegram_chat_id,
                        telegram_user_id,
                        account_id,
                        page_id_or_url,
                        page_name,
                        post_type,
                        caption,
                        media_path,
                    ),
                )
                job_id = str(cur.fetchone()["id"])
            conn.commit()
        return job_id

    def claim_account_runtime(self, account_id: str, owner: str, lease_seconds: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into fb_account_runtime (account_id, locked_until, locked_by, updated_at)
                    values (%s, now() + (%s || ' seconds')::interval, %s, now())
                    on conflict (account_id) do update set
                        locked_until = excluded.locked_until,
                        locked_by = excluded.locked_by,
                        updated_at = now()
                    where
                        fb_account_runtime.locked_until is null
                        or fb_account_runtime.locked_until < now()
                        or fb_account_runtime.locked_by = excluded.locked_by
                    returning account_id, last_cookie_used_at, locked_until, locked_by
                    """,
                    (account_id, int(lease_seconds), owner),
                )
                row = cur.fetchone()
            conn.commit()
        return dict(row) if row else None

    def extend_account_runtime(self, account_id: str, owner: str, lease_seconds: int) -> bool:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update fb_account_runtime
                    set locked_until = now() + (%s || ' seconds')::interval,
                        updated_at = now()
                    where account_id=%s and locked_by=%s
                    """,
                    (int(lease_seconds), account_id, owner),
                )
                changed = cur.rowcount > 0
            conn.commit()
        return changed

    def release_account_runtime(self, account_id: str, owner: str, mark_used: bool = True) -> bool:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update fb_account_runtime
                    set last_cookie_used_at = case when %s then now() else last_cookie_used_at end,
                        locked_until = null,
                        locked_by = null,
                        updated_at = now()
                    where account_id=%s and locked_by=%s
                    """,
                    (bool(mark_used), account_id, owner),
                )
                changed = cur.rowcount > 0
            conn.commit()
        return changed

    def mark_job_started(self, job_id: str) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("update fb_post_jobs set status='processing', started_at=now() where id=%s", (job_id,))
            conn.commit()

    def mark_job_completed(self, job_id: str, success: bool, result: Dict[str, Any], error: str = "") -> None:
        status = "success" if success else "failed"
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update fb_post_jobs
                    set status=%s, result=%s::jsonb, error=%s, completed_at=now()
                    where id=%s
                    """,
                    (status, json.dumps(result, ensure_ascii=False), error, job_id),
                )
            conn.commit()
