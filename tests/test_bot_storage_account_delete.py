import bot_storage


class FakeCursor:
    def __init__(self, rowcounts):
        self.rowcounts = list(rowcounts)
        self.executed = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params=None):
        self.executed.append((" ".join(str(query).split()), params))
        self.rowcount = self.rowcounts.pop(0) if self.rowcounts else 0


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


def storage_with_fake_connection(rowcounts):
    cursor = FakeCursor(rowcounts)
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
