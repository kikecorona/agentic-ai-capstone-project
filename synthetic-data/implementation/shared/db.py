"""Shared SQLite helpers.

Every stateful service owns exactly one SQLite file under
`synthetic-data/data/<service>.db`. This module is the only place where
that path is computed and where the connection options (WAL,
foreign keys, row factory) are set.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def connect(service_name: str) -> sqlite3.Connection:
    """Open (or create) the SQLite file for `service_name`.

    Connections are opened in autocommit mode (`isolation_level=None`)
    because every service in this demo runs short, idempotent statements
    and pays no price for not batching them. WAL is on for read
    concurrency; `check_same_thread=False` lets Flask reuse the
    connection across worker threads.
    """
    db_path = DATA_DIR / f"{service_name}.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_schema(conn: sqlite3.Connection, ddl: str) -> None:
    """Apply schema. The DDL is expected to be idempotent
    (`CREATE TABLE IF NOT EXISTS …`)."""
    conn.executescript(ddl)
