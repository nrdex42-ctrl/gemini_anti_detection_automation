#!/usr/bin/env python3
"""Apply the Supabase/Postgres schema used by the Telegram bot."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg


def main() -> int:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required to initialize Supabase/Postgres schema")

    schema_path = Path(__file__).resolve().parents[1] / "supabase" / "schema.sql"
    sql = schema_path.read_text(encoding="utf-8")
    with psycopg.connect(database_url, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    print(f"Applied schema: {schema_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
