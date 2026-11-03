"""Tiny SQLite layer with forward-only migrations.

Kept deliberately small: one connection guarded by a lock is plenty for a
single-user homelab app, and it keeps the dependency list short. JSON columns
are stored as TEXT and decoded by the helpers in `rows()`.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

JSON_COLUMNS = {
    "authors", "narrators", "series", "book", "candidates", "selected",
    "attempts", "transfers", "log", "genres",
}

MIGRATIONS: list[str] = [
    # 1 — initial schema
    """
    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE jobs (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        kind         TEXT NOT NULL DEFAULT 'download',   -- download | import
        asin         TEXT,
        region       TEXT,
        title        TEXT NOT NULL,
        subtitle     TEXT,
        authors      TEXT NOT NULL DEFAULT '[]',
        narrators    TEXT NOT NULL DEFAULT '[]',
        series       TEXT NOT NULL DEFAULT '[]',
        runtime_min  INTEGER,
        cover_url    TEXT,
        book         TEXT NOT NULL DEFAULT '{}',          -- full normalised metadata
        status       TEXT NOT NULL DEFAULT 'queued',
        detail       TEXT,
        progress     REAL NOT NULL DEFAULT 0,
        candidates   TEXT NOT NULL DEFAULT '[]',
        selected     TEXT,
        attempts     TEXT NOT NULL DEFAULT '[]',          -- search query log
        transfers    TEXT NOT NULL DEFAULT '[]',          -- per-file download state
        log          TEXT NOT NULL DEFAULT '[]',
        staging_path TEXT,
        output_path  TEXT,
        import_mode  TEXT,                                -- move | copy (manual import)
        error        TEXT,
        requested_by TEXT,                                -- user id once auth exists
        created_at   REAL NOT NULL,
        updated_at   REAL NOT NULL
    );
    CREATE INDEX jobs_status ON jobs(status);
    CREATE INDEX jobs_asin ON jobs(asin);
    CREATE TABLE library (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id      INTEGER,
        asin        TEXT,
        title       TEXT NOT NULL,
        subtitle    TEXT,
        authors     TEXT NOT NULL DEFAULT '[]',
        narrators   TEXT NOT NULL DEFAULT '[]',
        series      TEXT NOT NULL DEFAULT '[]',
        path        TEXT NOT NULL UNIQUE,
        cover_path  TEXT,
        file_count  INTEGER,
        size_bytes  INTEGER,
        duration_s  REAL,
        book        TEXT NOT NULL DEFAULT '{}',
        source      TEXT,                                 -- download | import | scan
        abs_status  TEXT,                                 -- reserved for ABS sync
        added_at    REAL NOT NULL
    );
    CREATE INDEX library_asin ON library(asin);
    """,
    # 2 — Audiobookshelf integration
    """
    ALTER TABLE jobs ADD COLUMN abs_status TEXT;          -- pending | imported | failed | NULL
    ALTER TABLE jobs ADD COLUMN abs_item_id TEXT;
    ALTER TABLE jobs ADD COLUMN abs_detail TEXT;
    ALTER TABLE library ADD COLUMN abs_item_id TEXT;
    CREATE TABLE abs_items (
        id          TEXT PRIMARY KEY,
        library_id  TEXT NOT NULL,
        asin        TEXT,
        match_key   TEXT,                                  -- normalised title|author surname
        title       TEXT,
        subtitle    TEXT,
        authors     TEXT,
        narrators   TEXT,
        series      TEXT,
        duration_s  REAL,
        num_tracks  INTEGER,
        size_bytes  INTEGER,
        path        TEXT,
        has_cover   INTEGER,
        added_at    REAL,
        via_narratarr INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE abs_links (                               -- ABS items Narratarr imported
        item_id   TEXT PRIMARY KEY,
        job_id    INTEGER,
        asin      TEXT,
        linked_at REAL NOT NULL
    );
    CREATE INDEX abs_items_asin ON abs_items(asin);
    CREATE INDEX abs_items_key ON abs_items(match_key);
    """,
    # 3 — ebooks alongside audiobooks
    """
    ALTER TABLE jobs ADD COLUMN media TEXT NOT NULL DEFAULT 'audiobook';    -- audiobook | ebook
    ALTER TABLE library ADD COLUMN media TEXT NOT NULL DEFAULT 'audiobook';
    ALTER TABLE abs_items ADD COLUMN media TEXT NOT NULL DEFAULT 'audiobook';
    """,
]


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            self.conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            row = self.conn.execute("SELECT version FROM schema_version").fetchone()
            current = row["version"] if row else 0
            if row is None:
                self.conn.execute("INSERT INTO schema_version(version) VALUES (0)")
            for i, sql in enumerate(MIGRATIONS[current:], start=current + 1):
                self.conn.executescript("BEGIN;" + sql + f"; UPDATE schema_version SET version={i}; COMMIT;")

    # -- helpers ---------------------------------------------------------------
    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        d = dict(row)
        for k in JSON_COLUMNS & d.keys():
            if isinstance(d[k], str):
                try:
                    d[k] = json.loads(d[k])
                except json.JSONDecodeError:
                    pass
        return d

    @staticmethod
    def _encode(values: dict) -> dict:
        return {k: (json.dumps(v) if k in JSON_COLUMNS and not isinstance(v, str) and v is not None else v)
                for k, v in values.items()}

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [self._decode(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            return cur.lastrowid

    def insert(self, table: str, values: dict) -> int:
        values = self._encode(values)
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        return self.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", values.values())

    def update(self, table: str, row_id: int, values: dict) -> None:
        if table == "jobs":
            values = {**values, "updated_at": time.time()}
        values = self._encode(values)
        sets = ", ".join(f"{k}=?" for k in values)
        self.execute(f"UPDATE {table} SET {sets} WHERE id=?", [*values.values(), row_id])
