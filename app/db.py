"""Persistence for verification records and the audit trail.

Deliberately small: two tables and a JSON blob for the full assessment, so a
record can be replayed in an audit exactly as it was produced.

The audit trail stores a *hash* of any personal identifier, never the raw value
— the same privacy posture the NIPOST widget uses for NIN/email.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS verifications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id     TEXT NOT NULL,
    client_name   TEXT NOT NULL,
    sector        TEXT,
    postcode      TEXT,
    score         INTEGER NOT NULL,
    tier          TEXT NOT NULL,
    decision      TEXT NOT NULL,
    mode          TEXT NOT NULL,
    assessment    TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verifications_client ON verifications(client_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    actor         TEXT NOT NULL,
    action        TEXT NOT NULL,
    subject       TEXT,
    subject_hash  TEXT,
    detail        TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_subject_hash ON audit_log(subject_hash);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_identifier(value: str) -> str:
    """Stable, salted-by-nothing SHA-256 of an identifier.

    Salt should come from configuration in a real deployment; the point here is
    that the raw NIN/email never lands in the database or the logs.
    """
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- verifications ------------------------------------------------------ #

    def save_verification(self, assessment: dict[str, Any], mode: str) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO verifications
                   (client_id, client_name, sector, postcode, score, tier, decision, mode, assessment, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    assessment["client_id"],
                    assessment["client_name"],
                    assessment.get("sector"),
                    assessment.get("postcode"),
                    assessment["score"],
                    assessment["tier"],
                    assessment["decision"],
                    mode,
                    json.dumps(assessment),
                    utcnow(),
                ),
            )
            return int(cur.lastrowid)

    def recent_verifications(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM verifications ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["assessment"] = json.loads(d["assessment"])
            out.append(d)
        return out

    def latest_for_client(self, client_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM verifications WHERE client_id = ? ORDER BY id DESC LIMIT 1",
                (client_id,),
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["assessment"] = json.loads(d["assessment"])
        return d

    # -- audit -------------------------------------------------------------- #

    def audit(self, actor: str, action: str, subject: str | None = None,
              detail: dict[str, Any] | None = None, hash_subject: bool = False) -> int:
        subject_hash = hash_identifier(subject) if (subject and hash_subject) else None
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO audit_log (actor, action, subject, subject_hash, detail, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    actor,
                    action,
                    None if hash_subject else subject,
                    subject_hash,
                    json.dumps(detail) if detail else None,
                    utcnow(),
                ),
            )
            return int(cur.lastrowid)

    def audit_entries(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("detail"):
                d["detail"] = json.loads(d["detail"])
            out.append(d)
        return out
