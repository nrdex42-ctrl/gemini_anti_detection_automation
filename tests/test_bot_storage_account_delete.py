import bot_storage
import pytest
from cryptography.fernet import Fernet


class FakeCursor:
    def __init__(self, rowcounts, *, fetchone_rows=None, fetchall_rows=None):
        self.rowcounts = list(rowcounts)
        self.fetchone_rows = list(fetchone_rows or [])
        self.fetchall_rows = list(fetchall_rows or [])
        self.executed = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params=None):
        self.executed.append((" ".join(str(query).split()), params))
        self.rowcount = self.rowcounts.pop(0) if self.rowcounts else 0

    def fetchone(self):
        return self.fetchone_rows.pop(0) if self.fetchone_rows else {}

    def fetchall(self):
        return self.fetchall_rows.pop(0) if self.fetchall_rows else []


class FakeConnection:
    def __init__(self, cursor):
        self.cursor_obj = cursor
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True


def storage_with_fake_connection(rowcounts, *, fetchone_rows=None, fetchall_rows=None):
    cursor = FakeCursor(rowcounts, fetchone_rows=fetchone_rows, fetchall_rows=fetchall_rows)
    connection = FakeConnection(cursor)
    storage = bot_storage.BotStorage("postgresql://example", cipher=object())
    storage.connect = lambda: connection
    return storage, cursor, connection


def test_deactivate_account_deletes_related_pages_when_account_matches_owner():
    storage, cursor, connection = storage_with_fake_connection([1, 2])

    changed = storage.deactivate_account("acct_1", owner_id=99)

    assert changed is True
    assert connection.committed is True
    assert len(cursor.executed) == 2
    assert "update fb_accounts" in cursor.executed[0][0]
    assert cursor.executed[0][1] == ("acct_1", 99)
    assert cursor.executed[1] == ("delete from fb_pages where account_id=%s", ("acct_1",))


def test_deactivate_account_keeps_pages_when_account_does_not_match_owner():
    storage, cursor, connection = storage_with_fake_connection([0])

    changed = storage.deactivate_account("acct_1", owner_id=99)

    assert changed is False
    assert connection.committed is True
    assert len(cursor.executed) == 1
    assert "delete from fb_pages" not in cursor.executed[0][0]


def test_delete_account_removes_pages_before_owner_scoped_account_delete():
    storage, cursor, connection = storage_with_fake_connection([2, 1])

    changed = storage.delete_account("acct_1", owner_id=99)

    assert changed is True
    assert connection.committed is True
    assert len(cursor.executed) == 2
    assert cursor.executed[0][0].startswith("delete from fb_pages p")
    assert "a.created_by=%s" in cursor.executed[0][0]
    assert cursor.executed[0][1] == ("acct_1", 99)
    assert cursor.executed[1] == (
        "delete from fb_accounts where account_id=%s and created_by=%s",
        ("acct_1", 99),
    )


def test_admin_delete_users_removes_pages_before_accounts():
    storage, cursor, connection = storage_with_fake_connection([3, 4, 2, 1])

    result = storage.admin_delete_users([111])

    assert result == {"users": 1, "accounts": 2, "jobs": 3}
    assert connection.committed is True
    assert len(cursor.executed) == 4
    assert cursor.executed[0][0].startswith("delete from fb_post_jobs")
    assert cursor.executed[1][0].startswith("delete from fb_pages p using fb_accounts a")
    assert cursor.executed[2][0].startswith("delete from fb_accounts")


def test_purge_removed_account_pages_deletes_inactive_and_orphaned_page_rows():
    storage, cursor, connection = storage_with_fake_connection([3])

    deleted = storage.purge_removed_account_pages()

    assert deleted == 3
    assert connection.committed is True
    assert cursor.executed[0][0].startswith("delete from fb_pages p where not exists")
    assert "a.account_id = p.account_id" in cursor.executed[0][0]
    assert "a.active = true" in cursor.executed[0][0]


def test_dashboard_summary_counts_pages_only_for_active_accounts():
    storage, cursor, _connection = storage_with_fake_connection(
        [3],
        fetchone_rows=[{"page_count": 4}],
        fetchall_rows=[
            [{"account_id": "acct_active", "count": 2}],
            [],
            [],
            [],
        ],
    )

    summary = storage.dashboard_summary(owner_id=99)

    assert summary["page_count"] == 2
    assert summary["page_counts_by_account"] == {"acct_active": 2}
    assert cursor.executed[0][0].startswith("delete from fb_pages p where not exists")
    assert "join fb_accounts a on a.account_id = p.account_id" in cursor.executed[1][0]
    assert "where a.created_by=%s and a.active = true" in cursor.executed[1][0]
    assert "where a.created_by=%s and a.active = true" in cursor.executed[2][0]


def test_secret_cipher_requires_encryption_key_for_cookie_storage(monkeypatch):
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    cipher = bot_storage.SecretCipher()

    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY is required before storing Facebook cookies"):
        cipher.encrypt("c_user=123; xs=session")

    assert cipher.decrypt("c_user=123; xs=session") == "c_user=123; xs=session"


def test_secret_cipher_encrypts_and_decrypts_with_key():
    cipher = bot_storage.SecretCipher(Fernet.generate_key().decode("utf-8"))

    encrypted = cipher.encrypt("c_user=123; xs=session")

    assert encrypted.startswith(bot_storage.TOKEN_PREFIX)
    assert encrypted != "c_user=123; xs=session"
    assert cipher.decrypt(encrypted) == "c_user=123; xs=session"


def test_upsert_pages_replaces_account_page_snapshot_and_normalizes_page_fields():
    storage, cursor, connection = storage_with_fake_connection([2, 1, 1])

    storage.upsert_pages(
        "acct_1",
        [
            {"page_id": "p1", "page_name": "Insan", "page_url": "https://facebook.com/insan"},
            {"id": "p1", "name": "Duplicate Insan", "url": "https://facebook.com/insan"},
            {"page_name": "Oppo", "follower_count": "1.2K"},
        ],
    )

    assert connection.committed is True
    assert cursor.executed[0] == ("delete from fb_pages where account_id=%s", ("acct_1",))
    assert len(cursor.executed) == 3
    assert cursor.executed[1][1] == ("acct_1", "p1", "Insan", "https://facebook.com/insan", "")
    account_id, generated_page_id, page_name, page_url, follower_count = cursor.executed[2][1]
    assert account_id == "acct_1"
    assert len(generated_page_id) == 24
    assert page_name == "Oppo"
    assert page_url == ""
    assert follower_count == "1.2K"
