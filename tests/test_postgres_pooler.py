import importlib.util
from pathlib import Path

import bot_storage


def test_bot_storage_disables_psycopg_auto_prepare(monkeypatch):
    captured = {}
    sentinel = object()

    def connect(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr(bot_storage.psycopg, "connect", connect)

    storage = bot_storage.BotStorage("postgresql://example", cipher=object())

    assert storage.connect() is sentinel
    assert captured["args"] == ("postgresql://example",)
    assert captured["kwargs"]["connect_timeout"] == 15
    assert captured["kwargs"]["row_factory"] is bot_storage.dict_row
    assert captured["kwargs"]["prepare_threshold"] is None


def test_postgres_account_lock_disables_psycopg_auto_prepare(monkeypatch):
    engine_path = Path(__file__).resolve().parents[1] / "playwright_engine.py"
    spec = importlib.util.spec_from_file_location("pooler_safe_playwright_engine", engine_path)
    assert spec and spec.loader
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)

    captured = {}

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params):
            captured["query"] = query
            captured["params"] = params

        def fetchone(self):
            return (True,)

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            captured["closed"] = True

    class Psycopg:
        def connect(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return Connection()

    monkeypatch.setattr(engine, "DATABASE_URL", "postgresql://example")
    monkeypatch.setattr(engine, "psycopg", Psycopg())

    conn = engine._acquire_postgres_account_lock("account-1", wait_seconds=1)

    assert isinstance(conn, Connection)
    assert captured["args"] == ("postgresql://example",)
    assert captured["kwargs"]["connect_timeout"] == 10
    assert captured["kwargs"]["prepare_threshold"] is None
