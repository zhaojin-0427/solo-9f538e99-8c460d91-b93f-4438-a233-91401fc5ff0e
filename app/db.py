"""SQLite persistence layer.

Schema overview
---------------
hospital        : one row per hospital code (mapping namespaces are per hospital)
uid_mapping     : deterministic old-UID -> new-UID mapping, namespaced by hospital
batch           : upload batch registry; only ``published`` batches are usable
batch_file      : per-(hospital, SOP UID) canonical converted product record
batch_item      : one row per file that made up a specific batch
"""
from __future__ import annotations

import sqlite3
from flask import current_app, g

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS hospital (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    code         TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS uid_mapping (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    hospital_id   INTEGER NOT NULL REFERENCES hospital(id),
    old_uid       TEXT NOT NULL,
    new_uid       TEXT NOT NULL UNIQUE,
    uid_kind      TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (hospital_id, old_uid)
);

CREATE TABLE IF NOT EXISTS batch (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_code    TEXT NOT NULL UNIQUE,
    hospital_id   INTEGER NOT NULL REFERENCES hospital(id),
    status        TEXT NOT NULL CHECK (status IN ('processing', 'published')),
    file_count    INTEGER NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    published_at  TEXT
);

CREATE TABLE IF NOT EXISTS batch_file (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    hospital_id       INTEGER NOT NULL REFERENCES hospital(id),
    sop_uid_old       TEXT NOT NULL,
    sop_uid_new       TEXT NOT NULL,
    file_sha256       TEXT NOT NULL,
    stored_name       TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    created_batch_id  INTEGER NOT NULL REFERENCES batch(id),
    UNIQUE (hospital_id, sop_uid_old)
);

CREATE TABLE IF NOT EXISTS batch_item (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id          INTEGER NOT NULL REFERENCES batch(id),
    batch_file_id     INTEGER NOT NULL REFERENCES batch_file(id),
    ordinal           INTEGER NOT NULL,
    original_filename TEXT NOT NULL,
    file_sha256       TEXT NOT NULL,
    UNIQUE (batch_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_mapping_hospital ON uid_mapping(hospital_id);
CREATE INDEX IF NOT EXISTS idx_batch_hospital   ON batch(hospital_id);
CREATE INDEX IF NOT EXISTS idx_item_batch       ON batch_item(batch_id);
"""


def get_db() -> sqlite3.Connection:
    """Return a request-scoped SQLite connection."""
    if "db" not in g:
        conn = sqlite3.connect(
            current_app.config["DB_PATH"],
            detect_types=sqlite3.PARSE_DECLTYPES,
            check_same_thread=False,
            timeout=30,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        g.db = conn
    return g.db


def close_db(_exc: BaseException | None = None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(db_path: str) -> None:
    """Create the schema (idempotent). Used at startup and in tests."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def init_app(app) -> None:
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db(app.config["DB_PATH"])
