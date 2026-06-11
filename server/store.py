"""SQLite store: decision metadata + per-batch session media dirs.

Two separate concepts, two separate columns — their disagreement is the
audit signal and they are never merged:

- auto_status: what the pipeline concluded (Pass | Needs Review | Fail)
- disposition: the human's call (Approved | Rejected), with
  dispositioned_by / dispositioned_at / note

State machine (spec §4):

    Pending ─ processing ─► auto_status set
        Pass         → disposition Approved by "system" (stays editable)
        Needs Review → open, agent approves/rejects
        Fail         → open, agent approves/rejects (never auto-rejected)

A batch is complete when no record is open. SQLite holds decisions and
metadata only; uploaded PDFs and crops live in a per-batch media dir that
is purged on batch delete — the export is the durable artifact.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

OPEN = None  # disposition value while a record awaits the agent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',      -- pending|processing|done|error
    error TEXT,
    ttb_id TEXT,
    auto_status TEXT,                           -- Pass|Needs Review|Fail
    disposition TEXT,                           -- Approved|Rejected|NULL=open
    dispositioned_by TEXT,                      -- 'system' or agent name
    dispositioned_at TEXT,
    note TEXT,
    form_json TEXT,
    crops_json TEXT,
    verdicts_json TEXT,
    warning_json TEXT,
    escalation_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_batch ON records(batch_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: Path, media_root: Path):
        self.db_path = Path(db_path)
        self.media_root = Path(media_root)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.media_root.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        return c

    # --- batches ---------------------------------------------------------

    def create_batch(self, name: str) -> dict:
        batch_id = uuid.uuid4().hex[:12]
        with self._conn() as c:
            c.execute(
                "INSERT INTO batches (id, name, created_at) VALUES (?,?,?)",
                (batch_id, name, _now()),
            )
        self.batch_media_dir(batch_id).mkdir(parents=True, exist_ok=True)
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM batches WHERE id=?", (batch_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_batches(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM batches ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) | {"summary": self.batch_summary(r["id"])} for r in rows]

    def batch_media_dir(self, batch_id: str) -> Path:
        return self.media_root / batch_id

    def delete_batch(self, batch_id: str) -> None:
        """Purge: rows and the session media dir (PDFs + crops)."""
        with self._conn() as c:
            c.execute("DELETE FROM records WHERE batch_id=?", (batch_id,))
            c.execute("DELETE FROM batches WHERE id=?", (batch_id,))
        shutil.rmtree(self.batch_media_dir(batch_id), ignore_errors=True)

    def batch_summary(self, batch_id: str) -> dict:
        with self._conn() as c:
            rows = c.execute(
                "SELECT state, auto_status, disposition FROM records WHERE batch_id=?",
                (batch_id,),
            ).fetchall()
        processed = sum(1 for r in rows if r["state"] in ("done", "error"))
        return {
            "total": len(rows),
            "processed": processed,
            "failed": sum(1 for r in rows if r["auto_status"] == "Fail"),
            "needs_review": sum(
                1 for r in rows if r["auto_status"] == "Needs Review"
            ),
            "passed": sum(1 for r in rows if r["auto_status"] == "Pass"),
            "errors": sum(1 for r in rows if r["state"] == "error"),
            "open": sum(
                1
                for r in rows
                if r["state"] == "done" and r["disposition"] is OPEN
            ),
            "complete": len(rows) > 0
            and processed == len(rows)
            and all(
                r["disposition"] is not OPEN
                for r in rows
                if r["state"] == "done"
            ),
        }

    # --- records ---------------------------------------------------------

    def add_record(self, batch_id: str, filename: str) -> str:
        record_id = uuid.uuid4().hex[:12]
        with self._conn() as c:
            c.execute(
                "INSERT INTO records (id, batch_id, filename, created_at) "
                "VALUES (?,?,?,?)",
                (record_id, batch_id, filename, _now()),
            )
        return record_id

    def list_unfinished(self) -> list[dict]:
        """Records caught mid-flight by a restart: a new process holds no
        worker for anything still 'pending'/'processing'/'escalating', so
        without re-enqueueing they would sit there forever."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, batch_id, filename FROM records "
                "WHERE state IN ('pending','processing','escalating') "
                "ORDER BY created_at, id"
            ).fetchall()
        return [dict(r) for r in rows]

    def record_processing(self, record_id: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE records SET state='processing' WHERE id=?", (record_id,)
            )

    def record_escalating(self, record_id: str) -> None:
        """Tier A is done; the record is queued for the (slow) vision
        reader. A distinct state so the UI can say why it's waiting."""
        with self._conn() as c:
            c.execute(
                "UPDATE records SET state='escalating' WHERE id=?", (record_id,)
            )

    def record_done(
        self,
        record_id: str,
        *,
        ttb_id: str | None,
        auto_status: str,
        form: dict,
        crops: list[dict],
        verdicts: list[dict],
        warning: dict | None,
        escalation: list[str],
    ) -> None:
        """Pipeline finished: set auto-status and apply the state machine.

        Pass auto-approves by 'system' (editable); everything else stays
        open for the agent. The system never sets Rejected.
        """
        approved = auto_status == "Pass"
        with self._conn() as c:
            c.execute(
                """UPDATE records SET state='done', ttb_id=?, auto_status=?,
                   form_json=?, crops_json=?, verdicts_json=?, warning_json=?,
                   escalation_json=?,
                   disposition=?, dispositioned_by=?, dispositioned_at=?
                   WHERE id=?""",
                (
                    ttb_id,
                    auto_status,
                    json.dumps(form),
                    json.dumps(crops),
                    json.dumps(verdicts),
                    json.dumps(warning) if warning else None,
                    json.dumps(escalation),
                    "Approved" if approved else OPEN,
                    "system" if approved else None,
                    _now() if approved else None,
                    record_id,
                ),
            )

    def record_error(self, record_id: str, error: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE records SET state='error', error=? WHERE id=?",
                (error, record_id),
            )

    def set_disposition(
        self, record_id: str, disposition: str, by: str, note: str | None = None
    ) -> dict:
        """The agent's call. Always editable, including auto-approved
        passes; nothing is locked."""
        if disposition not in ("Approved", "Rejected"):
            raise ValueError(f"invalid disposition: {disposition}")
        if not by or by == "system":
            raise ValueError("disposition requires an agent identity")
        with self._conn() as c:
            cur = c.execute(
                """UPDATE records SET disposition=?, dispositioned_by=?,
                   dispositioned_at=?, note=? WHERE id=? AND state='done'""",
                (disposition, by, _now(), note, record_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"no completed record {record_id}")
        return self.get_record(record_id)

    def get_record(self, record_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM records WHERE id=?", (record_id,)
            ).fetchone()
        return self._inflate(row) if row else None

    def list_records(self, batch_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM records WHERE batch_id=? ORDER BY created_at, id",
                (batch_id,),
            ).fetchall()
        return [self._inflate(r) for r in rows]

    @staticmethod
    def _inflate(row: sqlite3.Row) -> dict:
        d = dict(row)
        for key in ("form", "crops", "verdicts", "warning", "escalation"):
            raw = d.pop(f"{key}_json", None)
            d[key] = json.loads(raw) if raw else None
        return d
