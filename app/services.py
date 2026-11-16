"""Batch orchestration: conflict rules, mapping persistence, atomic publication."""
from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app

from . import dicom_engine
from .dicom_engine import DicomValidationError

# A process-wide lock so concurrent uploads can never derive two different
# publication outcomes.  SQLite's own write lock additionally guards against
# separate processes.
_BATCH_LOCK = threading.RLock()


def _staging_dir():
    return Path(current_app.config["STAGING_DIR"])


def _batch_staging_dir():
    """In-flight batch output directory; wiped on every application start."""
    return Path(current_app.config["STAGING_DIR"]) / "batches"


def _published_dir():
    return Path(current_app.config["PUBLISHED_DIR"])


class BatchConflictError(Exception):
    """Whole-batch conflict (same SOP UID, different bytes) -> HTTP 409."""


class UploadError(Exception):
    """Client-side upload problem -> HTTP 400."""


@dataclass
class UploadedFile:
    original_filename: str
    temp_path: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def cleanup_staging() -> list[str]:
    """Remove in-flight batch directories left behind by a crash/restart.

    Only ``staging/batches`` is purged: every directory there belongs to a
    batch that never committed.  The separate ``staging/uploads`` work area
    holds request-scoped files and is cleaned by the request handler.
    """
    removed: list[str] = []
    batch_root = _batch_staging_dir()
    if not batch_root.exists():
        return removed
    for child in batch_root.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
        removed.append(child.name)
    return removed


def _get_or_create_hospital(conn: sqlite3.Connection, code: str) -> int:
    row = conn.execute(
        "SELECT id FROM hospital WHERE code = ?", (code,)
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO hospital(code) VALUES (?)", (code,))
    return int(cur.lastrowid)


def _ensure_mapping(conn: sqlite3.Connection, hospital_id: int,
                    old_uid: str, new_uid: str, kind: str) -> None:
    """Idempotent mapping insert; verifies determinism if it already exists."""
    while True:
        try:
            conn.execute(
                "INSERT INTO uid_mapping(hospital_id, old_uid, new_uid, uid_kind) "
                "VALUES (?, ?, ?, ?)",
                (hospital_id, old_uid, new_uid, kind),
            )
            return
        except sqlite3.IntegrityError as exc:
            row = conn.execute(
                "SELECT new_uid FROM uid_mapping WHERE hospital_id = ? AND old_uid = ?",
                (hospital_id, old_uid),
            ).fetchone()
            if row is not None:
                if row["new_uid"] != new_uid:
                    raise RuntimeError(
                        "non-deterministic UID mapping detected"
                    ) from exc
                return
            # new_uid collided (astronomically unlikely for a 128-bit UUID) or
            # another writer raced us; retry to let the unique check settle.
            existing = conn.execute(
                "SELECT hospital_id, old_uid FROM uid_mapping WHERE new_uid = ?",
                (new_uid,),
            ).fetchone()
            if existing is not None:
                raise RuntimeError("new UID collision detected") from exc


def process_batch(conn: sqlite3.Connection, hospital_code: str,
                  files: list[UploadedFile]) -> dict:
    """Validate, convert and atomically publish one upload batch.

    Returns a dict describing the published (or reused) batch.  Raises
    ``UploadError`` / ``DicomValidationError`` / ``BatchConflictError`` on
    failure; nothing is published in those cases.
    """
    if not files:
        raise UploadError("no files uploaded")

    with _BATCH_LOCK:
        # Phase 1: read and validate every input, build per-file plans.
        plans: list[dicom_engine.FilePlan] = []
        datasets: list = []
        seen_sop: dict[str, int] = {}
        try:
            for ordinal, uploaded in enumerate(files, start=1):
                ds, plan = dicom_engine.load_and_plan(
                    uploaded.temp_path,
                    hospital_code,
                    ordinal=ordinal,
                    original_filename=uploaded.original_filename,
                )
                if plan.sop_uid in seen_sop:
                    raise UploadError(
                        f"duplicate SOPInstanceUID {plan.sop_uid} inside this batch "
                        f"(files {seen_sop[plan.sop_uid]} and {ordinal})"
                    )
                seen_sop[plan.sop_uid] = ordinal
                plans.append(plan)
                datasets.append(ds)

            # Phase 2: conflict check against published registry (no writes yet).
            conflicts, reused = [], []
            for plan in plans:
                row = conn.execute(
                    "SELECT bf.file_sha256, b.batch_code, bf.id "
                    "FROM batch_file bf JOIN batch b ON b.id = bf.created_batch_id "
                    "JOIN hospital h ON h.id = bf.hospital_id "
                    "WHERE h.code = ? AND bf.sop_uid_old = ?",
                    (hospital_code, plan.sop_uid),
                ).fetchone()
                if row is None:
                    continue
                if row["file_sha256"] == plan.sha256:
                    reused.append((plan, row))
                else:
                    conflicts.append((plan, row))

            if conflicts:
                detail = "; ".join(
                    f"{p.original_filename} (SOP {p.sop_uid}) already stored with "
                    f"different bytes in batch {r['batch_code']}"
                    for p, r in conflicts
                )
                raise BatchConflictError(
                    "whole batch rejected due to SOP UID byte conflict: " + detail
                )

            # Phase 3: one transaction, staging directory first.
            batch_code = "B" + uuid.uuid4().hex
            staging_root = _batch_staging_dir() / batch_code
            staging_root.parent.mkdir(parents=True, exist_ok=True)
            staging_root.mkdir(parents=True, exist_ok=False)

            try:
                conn.execute("BEGIN IMMEDIATE")
                hospital_id = _get_or_create_hospital(conn, hospital_code)
                cur = conn.execute(
                    "INSERT INTO batch(batch_code, hospital_id, status, file_count) "
                    "VALUES (?, ?, 'processing', ?)",
                    (batch_code, hospital_id, len(plans)),
                )
                batch_id = int(cur.lastrowid)

                # Persist / verify every mapping (includes referenced-but-absent
                # UIDs because plans cover every UI element found in the files).
                for plan in plans:
                    for old_uid, (new_uid, kind) in plan.mappings.items():
                        _ensure_mapping(conn, hospital_id, old_uid, new_uid, kind)

                batch_file_rows: list[tuple[dicom_engine.FilePlan, sqlite3.Row | None]] = []
                for plan in plans:
                    existing = next(
                        (row for p, row in reused if p.sop_uid == plan.sop_uid),
                        None,
                    )
                    if existing is not None:
                        batch_file_rows.append((plan, existing))
                        continue

                    stored_name = f"{plan.sop_uid}.dcm"
                    out_path = staging_root / stored_name
                    dicom_engine.apply_mapping(datasets[plan.ordinal - 1], plan)
                    dicom_engine.write_converted(datasets[plan.ordinal - 1], out_path)
                    # Re-read from disk to prove pixels/TS/class are unchanged.
                    dicom_engine.verify_converted(
                        files[plan.ordinal - 1].temp_path, out_path
                    )
                    cur = conn.execute(
                        "INSERT INTO batch_file(hospital_id, sop_uid_old, sop_uid_new, "
                        "file_sha256, stored_name, original_filename, created_batch_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            hospital_id,
                            plan.sop_uid,
                            plan.mappings[plan.sop_uid][0],
                            plan.sha256,
                            stored_name,
                            plan.original_filename,
                            batch_id,
                        ),
                    )
                    batch_file_rows.append((plan, {"id": int(cur.lastrowid)}))

                for plan, row in batch_file_rows:
                    conn.execute(
                        "INSERT INTO batch_item(batch_id, batch_file_id, ordinal, "
                        "original_filename, file_sha256) VALUES (?, ?, ?, ?, ?)",
                        (
                            batch_id,
                            row["id"],
                            plan.ordinal,
                            plan.original_filename,
                            plan.sha256,
                        ),
                    )

                conn.execute(
                    "UPDATE batch SET status = 'published', published_at = ? WHERE id = ?",
                    (_utc_now(), batch_id),
                )

                # Phase 4: atomic publication move, then commit.  On restart any
                # directory still under staging/ is discarded; only rows that
                # committed have files under published/.
                published_root = _published_dir() / batch_code
                staging_root.rename(published_root)
                conn.commit()
            except BaseException:
                conn.rollback()
                shutil.rmtree(staging_root, ignore_errors=True)
                raise

            return {
                "batch_code": batch_code,
                "batch_id": batch_id,
                "reused_sops": [p.sop_uid for p, _ in reused],
                "plans": plans,
            }
        finally:
            # Only remove the individual input temp files; the batch staging
            # directory is a sibling and must survive until publication.
            for uploaded in files:
                try:
                    os.unlink(uploaded.temp_path)
                except FileNotFoundError:
                    pass


# ---------------------------------------------------------------------------
# Read-side queries
# ---------------------------------------------------------------------------

def list_batches(conn: sqlite3.Connection, hospital_code: str | None = None) -> list[sqlite3.Row]:
    sql = (
        "SELECT b.*, h.code AS hospital_code FROM batch b "
        "JOIN hospital h ON h.id = b.hospital_id "
        "WHERE b.status = 'published'"
    )
    params: tuple = ()
    if hospital_code:
        sql += " AND h.code = ?"
        params = (hospital_code,)
    sql += " ORDER BY b.id DESC"
    return conn.execute(sql, params).fetchall()


def get_batch(conn: sqlite3.Connection, batch_code: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT b.*, h.code AS hospital_code FROM batch b "
        "JOIN hospital h ON h.id = b.hospital_id "
        "WHERE b.batch_code = ? AND b.status = 'published'",
        (batch_code,),
    ).fetchone()


def list_batch_items(conn: sqlite3.Connection, batch_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT bi.*, bf.sop_uid_old, bf.sop_uid_new, bf.stored_name "
        "FROM batch_item bi JOIN batch_file bf ON bf.id = bi.batch_file_id "
        "WHERE bi.batch_id = ? ORDER BY bi.ordinal",
        (batch_id,),
    ).fetchall()


def list_mappings_for_hospital(conn: sqlite3.Connection, hospital_code: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT m.old_uid, m.new_uid, m.uid_kind, m.created_at "
        "FROM uid_mapping m JOIN hospital h ON h.id = m.hospital_id "
        "WHERE h.code = ? ORDER BY m.uid_kind, m.old_uid",
        (hospital_code,),
    ).fetchall()


def list_mappings_for_batch(conn: sqlite3.Connection, batch_id: int) -> list[sqlite3.Row]:
    """All mappings that participate in a batch (deduplicated)."""
    return conn.execute(
        "SELECT DISTINCT m.old_uid, m.new_uid, m.uid_kind "
        "FROM uid_mapping m "
        "JOIN hospital h ON h.id = m.hospital_id "
        "JOIN batch b ON b.hospital_id = h.id "
        "WHERE b.id = ? ORDER BY m.uid_kind, m.old_uid",
        (batch_id,),
    ).fetchall()


def list_hospitals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT h.*, COUNT(b.id) AS batch_count "
        "FROM hospital h LEFT JOIN batch b ON b.hospital_id = h.id "
        "AND b.status = 'published' GROUP BY h.id ORDER BY h.code"
    ).fetchall()


def published_path(batch_code: str, stored_name: str) -> str:
    # Guard against path traversal in stored names.
    safe_name = stored_name.replace("/", "_").replace("\\", "_").lstrip(".")
    return str(_published_dir() / batch_code / safe_name)
