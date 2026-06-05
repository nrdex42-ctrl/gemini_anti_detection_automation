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

from page_name_utils import clean_facebook_page_name


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
                    insert into fb_accounts (
                        account_id,
                        label,
                        cookie_ciphertext,
                        created_by,
                        cookie_status,
                        cookie_status_detail,
                        cookie_status_checked_at,
                        cookie_status_updated_at,
                        updated_at
                    )
                    values (%s, %s, %s, %s, 'unverified', 'New or updated cookie is not verified against Facebook yet.', null, now(), now())
                    on conflict (account_id) do update set
                        label = excluded.label,
                        cookie_ciphertext = excluded.cookie_ciphertext,
                        created_by = coalesce(fb_accounts.created_by, excluded.created_by),
                        active = true,
                        cookie_status = 'unverified',
                        cookie_status_detail = 'New or updated cookie is not verified against Facebook yet.',
                        cookie_status_checked_at = null,
                        cookie_status_updated_at = now(),
                        updated_at = now()
                    where fb_accounts.created_by is null
                       or fb_accounts.created_by = excluded.created_by
                       or excluded.created_by is null
                    returning account_id
                    """,
                    (account_id, label or account_id, encrypted_cookie, created_by or None),
                )
                if cur.fetchone() is None:
                    raise RuntimeError("This Facebook account is already stored by another Telegram user")
            conn.commit()

    def update_account_label(self, account_id: str, label: str, owner_id: Optional[int] = None) -> bool:
        label = (label or "").strip()
        if not label:
            return False
        with self.connect() as conn:
            with conn.cursor() as cur:
                if owner_id is None:
                    cur.execute(
                        """
                        update fb_accounts
                        set label=%s, updated_at=now()
                        where account_id=%s
                        """,
                        (label, account_id),
                    )
                else:
                    cur.execute(
                        """
                        update fb_accounts
                        set label=%s, updated_at=now()
                        where account_id=%s and created_by=%s
                        """,
                        (label, account_id, int(owner_id)),
                    )
                changed = cur.rowcount > 0
            conn.commit()
        return changed

    def update_account_cookie_validation(
        self,
        account_id: str,
        status: str,
        detail: str = "",
        owner_id: Optional[int] = None,
    ) -> bool:
        normalized = (status or "").strip().lower()
        if normalized not in {"valid", "invalid", "unverified"}:
            normalized = "invalid"
        compact_detail = " ".join(str(detail or "").split())[:1000]
        with self.connect() as conn:
            with conn.cursor() as cur:
                if owner_id is None:
                    cur.execute(
                        """
                        update fb_accounts
                        set cookie_status=%s,
                            cookie_status_detail=%s,
                            cookie_status_checked_at=now(),
                            cookie_status_updated_at=now()
                        where account_id=%s
                        """,
                        (normalized, compact_detail, account_id),
                    )
                else:
                    cur.execute(
                        """
                        update fb_accounts
                        set cookie_status=%s,
                            cookie_status_detail=%s,
                            cookie_status_checked_at=now(),
                            cookie_status_updated_at=now()
                        where account_id=%s and created_by=%s
                        """,
                        (normalized, compact_detail, account_id, int(owner_id)),
                    )
                changed = cur.rowcount > 0
            conn.commit()
        return changed

    def set_active_account(self, telegram_user_id: int, account_id: str) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into telegram_user_state (telegram_user_id, active_account_id, updated_at, last_seen_at)
                    values (%s, %s, now(), now())
                    on conflict (telegram_user_id) do update set
                        active_account_id = excluded.active_account_id,
                        updated_at = now(),
                        last_seen_at = now()
                    """,
                    (int(telegram_user_id), account_id),
                )
            conn.commit()

    def set_user_language(self, telegram_user_id: int, lang: str) -> bool:
        normalized = (lang or "").strip().lower()
        if normalized not in {"ar", "en"}:
            return False
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into telegram_user_state (telegram_user_id, lang, updated_at, last_seen_at)
                    values (%s, %s, now(), now())
                    on conflict (telegram_user_id) do update set
                        lang = excluded.lang,
                        updated_at = now(),
                        last_seen_at = now()
                    """,
                    (int(telegram_user_id), normalized),
                )
            conn.commit()
        return True

    def get_user_language(self, telegram_user_id: int) -> str:
        if not telegram_user_id:
            return "en"
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "select lang from telegram_user_state where telegram_user_id=%s",
                    (int(telegram_user_id),),
                )
                row = cur.fetchone()
        lang = str((row or {}).get("lang") or "en").strip().lower()
        return lang if lang in {"ar", "en"} else "en"

    def touch_user(self, telegram_user_id: int, chat_id: int) -> None:
        if not telegram_user_id:
            return
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into telegram_user_state (telegram_user_id, last_chat_id, updated_at, last_seen_at)
                    values (%s, %s, now(), now())
                    on conflict (telegram_user_id) do update set
                        last_chat_id = excluded.last_chat_id,
                        updated_at = now(),
                        last_seen_at = now()
                    """,
                    (int(telegram_user_id), int(chat_id or telegram_user_id)),
                )
            conn.commit()

    def get_active_account(self, telegram_user_id: int, owner_id: Optional[int] = None) -> str:
        with self.connect() as conn:
            with conn.cursor() as cur:
                sql = """
                    select s.active_account_id
                    from telegram_user_state s
                    join fb_accounts a on a.account_id = s.active_account_id
                    where s.telegram_user_id=%s and a.active=true
                    """
                params: List[Any] = [int(telegram_user_id)]
                if owner_id is not None:
                    sql += " and a.created_by=%s"
                    params.append(int(owner_id))
                cur.execute(sql, params)
                row = cur.fetchone()
        return str((row or {}).get("active_account_id") or "")

    def clear_active_account(self, telegram_user_id: int, account_id: str = "") -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                if account_id:
                    cur.execute(
                        """
                        update telegram_user_state
                        set active_account_id=null, updated_at=now()
                        where telegram_user_id=%s and active_account_id=%s
                        """,
                        (int(telegram_user_id), account_id),
                    )
                else:
                    cur.execute(
                        """
                        update telegram_user_state
                        set active_account_id=null, updated_at=now()
                        where telegram_user_id=%s
                        """,
                        (int(telegram_user_id),),
                    )
            conn.commit()

    def deactivate_account(self, account_id: str, owner_id: Optional[int] = None) -> bool:
        with self.connect() as conn:
            with conn.cursor() as cur:
                if owner_id is None:
                    cur.execute("update fb_accounts set active=false, updated_at=now() where account_id=%s", (account_id,))
                else:
                    cur.execute(
                        """
                        update fb_accounts
                        set active=false, updated_at=now()
                        where account_id=%s and created_by=%s
                        """,
                        (account_id, int(owner_id)),
                    )
                changed = cur.rowcount > 0
            conn.commit()
        return changed

    def list_accounts(self, owner_id: Optional[int] = None) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                if owner_id is None:
                    cur.execute(
                        """
                        select account_id, label, active, created_by, created_at, updated_at,
                               cookie_status, cookie_status_detail, cookie_status_checked_at, cookie_status_updated_at
                        from fb_accounts
                        order by greatest(coalesce(cookie_status_updated_at, updated_at), updated_at) desc
                        """
                    )
                else:
                    cur.execute(
                        """
                        select account_id, label, active, created_by, created_at, updated_at,
                               cookie_status, cookie_status_detail, cookie_status_checked_at, cookie_status_updated_at
                        from fb_accounts
                        where created_by=%s
                        order by greatest(coalesce(cookie_status_updated_at, updated_at), updated_at) desc
                        """,
                        (int(owner_id),),
                    )
                return list(cur.fetchall())

    def get_account(self, account_id: str, owner_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                sql = """
                    select account_id, label, active, created_by, created_at, updated_at,
                           cookie_status, cookie_status_detail, cookie_status_checked_at, cookie_status_updated_at
                    from fb_accounts
                    where account_id=%s
                    """
                params: List[Any] = [account_id]
                if owner_id is not None:
                    sql += " and created_by=%s"
                    params.append(int(owner_id))
                cur.execute(sql, params)
                row = cur.fetchone()
        return dict(row) if row else None

    def account_exists(self, account_id: str, active_only: bool = True, owner_id: Optional[int] = None) -> bool:
        with self.connect() as conn:
            with conn.cursor() as cur:
                sql = "select 1 from fb_accounts where account_id=%s"
                params: List[Any] = [account_id]
                if active_only:
                    sql += " and active=true"
                if owner_id is not None:
                    sql += " and created_by=%s"
                    params.append(int(owner_id))
                cur.execute(sql, params)
                return cur.fetchone() is not None

    def get_account_cookie(self, account_id: str, owner_id: Optional[int] = None) -> str:
        with self.connect() as conn:
            with conn.cursor() as cur:
                sql = "select cookie_ciphertext from fb_accounts where account_id=%s and active=true"
                params: List[Any] = [account_id]
                if owner_id is not None:
                    sql += " and created_by=%s"
                    params.append(int(owner_id))
                cur.execute(sql, params)
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
                    page_name = clean_facebook_page_name(page.get("name"), page_url, page_id or page_url)
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

    def list_pages(self, account_id: str, owner_id: Optional[int] = None) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                if owner_id is None:
                    cur.execute(
                        """
                        select page_id, page_name, page_url, updated_at
                        from fb_pages
                        where account_id=%s
                        order by page_name, page_id
                        """,
                        (account_id,),
                    )
                else:
                    cur.execute(
                        """
                        select p.page_id, p.page_name, p.page_url, p.updated_at
                        from fb_pages p
                        join fb_accounts a on a.account_id = p.account_id
                        where p.account_id=%s and a.created_by=%s
                        order by p.page_name, p.page_id
                        """,
                        (account_id, int(owner_id)),
                    )
                return list(cur.fetchall())

    def dashboard_summary(self, owner_id: Optional[int] = None) -> Dict[str, Any]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                if owner_id is None:
                    cur.execute("select count(*)::int as page_count from fb_pages")
                else:
                    cur.execute(
                        """
                        select count(*)::int as page_count
                        from fb_pages p
                        join fb_accounts a on a.account_id = p.account_id
                        where a.created_by=%s
                        """,
                        (int(owner_id),),
                    )
                page_row = cur.fetchone() or {}

                if owner_id is None:
                    cur.execute(
                        """
                        select account_id, count(*)::int as count
                        from fb_pages
                        group by account_id
                        """
                    )
                else:
                    cur.execute(
                        """
                        select p.account_id, count(*)::int as count
                        from fb_pages p
                        join fb_accounts a on a.account_id = p.account_id
                        where a.created_by=%s
                        group by p.account_id
                        """,
                        (int(owner_id),),
                    )
                page_counts_by_account = {
                    str(row["account_id"]): int(row["count"])
                    for row in cur.fetchall()
                }

                if owner_id is None:
                    cur.execute(
                        """
                        select status, count(*)::int as count
                        from fb_post_jobs
                        group by status
                        """
                    )
                else:
                    cur.execute(
                        """
                        select status, count(*)::int as count
                        from fb_post_jobs
                        where telegram_user_id=%s
                        group by status
                        """,
                        (int(owner_id),),
                    )
                status_counts = {str(row["status"]): int(row["count"]) for row in cur.fetchall()}

                if owner_id is None:
                    cur.execute(
                        """
                        select account_id, last_cookie_used_at, locked_until, locked_by
                        from fb_account_runtime
                        where locked_until is not null and locked_until > now()
                        order by locked_until desc
                        limit 10
                        """
                    )
                else:
                    cur.execute(
                        """
                        select r.account_id, r.last_cookie_used_at, r.locked_until, r.locked_by
                        from fb_account_runtime r
                        join fb_accounts a on a.account_id = r.account_id
                        where r.locked_until is not null
                          and r.locked_until > now()
                          and a.created_by=%s
                        order by r.locked_until desc
                        limit 10
                        """,
                        (int(owner_id),),
                    )
                locked_accounts = list(cur.fetchall())

                if owner_id is None:
                    cur.execute(
                        """
                        select id::text, account_id, page_id_or_url, page_name, post_type, status, created_at
                        from fb_post_jobs
                        order by created_at desc
                        limit 8
                        """
                    )
                else:
                    cur.execute(
                        """
                        select id::text, account_id, page_id_or_url, page_name, post_type, status, created_at
                        from fb_post_jobs
                        where telegram_user_id=%s
                        order by created_at desc
                        limit 8
                        """,
                        (int(owner_id),),
                    )
                recent_jobs = list(cur.fetchall())

        return {
            "page_count": int(page_row.get("page_count") or 0),
            "page_counts_by_account": page_counts_by_account,
            "job_status_counts": status_counts,
            "locked_accounts": locked_accounts,
            "recent_jobs": recent_jobs,
        }

    def admin_summary(self) -> Dict[str, Any]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select
                        count(*)::int as total_accounts,
                        count(*) filter (where active)::int as active_accounts,
                        count(*) filter (where not active)::int as inactive_accounts
                    from fb_accounts
                    """
                )
                account_row = cur.fetchone() or {}

                cur.execute("select count(*)::int as page_count from fb_pages")
                page_row = cur.fetchone() or {}

                cur.execute(
                    """
                    with known_users as (
                        select telegram_user_id from telegram_user_state
                        union
                        select created_by as telegram_user_id from fb_accounts where created_by is not null
                        union
                        select telegram_user_id from fb_post_jobs where telegram_user_id is not null
                    )
                    select count(*)::int as user_count from known_users
                    """
                )
                user_row = cur.fetchone() or {}

                cur.execute(
                    """
                    select status, count(*)::int as count
                    from fb_post_jobs
                    group by status
                    """
                )
                job_status_counts = {str(row["status"]): int(row["count"]) for row in cur.fetchall()}

                cur.execute(
                    """
                    select post_type, count(*)::int as count
                    from fb_post_jobs
                    group by post_type
                    """
                )
                post_type_counts = {str(row["post_type"]): int(row["count"]) for row in cur.fetchall()}

                cur.execute(
                    """
                    select account_id, last_cookie_used_at, locked_until, locked_by, updated_at
                    from fb_account_runtime
                    where locked_until is not null and locked_until > now()
                    order by locked_until desc
                    limit 12
                    """
                )
                active_locks = list(cur.fetchall())

                cur.execute(
                    """
                    select id::text, telegram_user_id, account_id, page_id_or_url, post_type,
                           status, error, created_at, completed_at
                    from fb_post_jobs
                    order by created_at desc
                    limit 12
                    """
                )
                recent_jobs = list(cur.fetchall())

        return {
            "total_accounts": int(account_row.get("total_accounts") or 0),
            "active_accounts": int(account_row.get("active_accounts") or 0),
            "inactive_accounts": int(account_row.get("inactive_accounts") or 0),
            "page_count": int(page_row.get("page_count") or 0),
            "user_count": int(user_row.get("user_count") or 0),
            "job_status_counts": job_status_counts,
            "post_type_counts": post_type_counts,
            "active_locks": active_locks,
            "recent_jobs": recent_jobs,
        }

    def admin_users(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    with known_users as (
                        select telegram_user_id from telegram_user_state
                        union
                        select created_by as telegram_user_id from fb_accounts where created_by is not null
                        union
                        select telegram_user_id from fb_post_jobs where telegram_user_id is not null
                    )
                    select
                        u.telegram_user_id,
                        s.active_account_id,
                        count(distinct a.account_id)::int as account_count,
                        count(distinct j.id)::int as job_count,
                        max(greatest(
                            coalesce(a.updated_at, 'epoch'::timestamptz),
                            coalesce(j.created_at, 'epoch'::timestamptz),
                            coalesce(s.updated_at, 'epoch'::timestamptz)
                        )) as last_seen
                    from known_users u
                    left join telegram_user_state s on s.telegram_user_id = u.telegram_user_id
                    left join fb_accounts a on a.created_by = u.telegram_user_id
                    left join fb_post_jobs j on j.telegram_user_id = u.telegram_user_id
                    group by u.telegram_user_id, s.active_account_id
                    order by last_seen desc nulls last
                    limit %s
                    """,
                    (int(limit),),
                )
                return list(cur.fetchall())

    def admin_accounts(self, limit: int = 30) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select
                        a.account_id,
                        a.label,
                        a.active,
                        a.created_by,
                        a.updated_at,
                        count(distinct p.page_id)::int as page_count,
                        count(distinct j.id)::int as job_count,
                        max(j.created_at) as last_job_at
                    from fb_accounts a
                    left join fb_pages p on p.account_id = a.account_id
                    left join fb_post_jobs j on j.account_id = a.account_id
                    group by a.account_id, a.label, a.active, a.created_by, a.updated_at
                    order by a.updated_at desc
                    limit %s
                    """,
                    (int(limit),),
                )
                return list(cur.fetchall())

    def list_restart_targets(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    with known_users as (
                        select telegram_user_id, last_chat_id, last_seen_at from telegram_user_state
                        union
                        select created_by as telegram_user_id, created_by as last_chat_id, updated_at as last_seen_at
                        from fb_accounts
                        where created_by is not null
                        union
                        select telegram_user_id, telegram_chat_id as last_chat_id, created_at as last_seen_at
                        from fb_post_jobs
                        where telegram_user_id is not null
                    )
                    select telegram_user_id,
                           coalesce(max(last_chat_id), telegram_user_id) as chat_id,
                           max(last_seen_at) as last_seen_at
                    from known_users
                    where telegram_user_id is not null
                    group by telegram_user_id
                    order by max(last_seen_at) desc nulls last
                    """
                )
                return list(cur.fetchall())

    def get_meta(self, key: str) -> str:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("select value from bot_meta where key=%s", (key,))
                row = cur.fetchone()
        return str((row or {}).get("value") or "")

    def set_meta(self, key: str, value: str) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into bot_meta (key, value, updated_at)
                    values (%s, %s, now())
                    on conflict (key) do update set
                        value = excluded.value,
                        updated_at = now()
                    """,
                    (key, value),
                )
            conn.commit()

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

    def create_post_jobs(self, jobs: List[Dict[str, Any]]) -> List[str]:
        if not jobs:
            return []
        job_ids: List[str] = []
        with self.connect() as conn:
            with conn.cursor() as cur:
                for job in jobs:
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
                            int(job["telegram_chat_id"]),
                            int(job["telegram_user_id"]),
                            str(job["account_id"]),
                            str(job["page_id_or_url"]),
                            str(job.get("page_name") or ""),
                            str(job["post_type"]),
                            str(job.get("caption") or ""),
                            str(job.get("media_path") or ""),
                        ),
                    )
                    job_ids.append(str(cur.fetchone()["id"]))
            conn.commit()
        return job_ids

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

    def mark_jobs_started(self, job_ids: List[str]) -> None:
        if not job_ids:
            return
        with self.connect() as conn:
            with conn.cursor() as cur:
                for job_id in job_ids:
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

    def mark_jobs_completed(self, completions: List[Dict[str, Any]]) -> None:
        if not completions:
            return
        with self.connect() as conn:
            with conn.cursor() as cur:
                for item in completions:
                    status = "success" if bool(item.get("success")) else "failed"
                    cur.execute(
                        """
                        update fb_post_jobs
                        set status=%s, result=%s::jsonb, error=%s, completed_at=now()
                        where id=%s
                        """,
                        (
                            status,
                            json.dumps(item.get("result") or {}, ensure_ascii=False),
                            str(item.get("error") or ""),
                            str(item["job_id"]),
                        ),
                    )
            conn.commit()
