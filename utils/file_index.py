"""SQLite database for file metadata, sync queue, and conflict records."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Optional


class Database:
    """Thread-safe SQLite wrapper using WAL mode for concurrent reads.

    Each thread gets its own connection via ``threading.local()``.
    """

    def __init__(self, db_path: str) -> None:
        self._path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        try:
            self._init_schema(self._conn)
        except sqlite3.DatabaseError as exc:
            import logging

            logging.getLogger("sync.db").error(
                "Database corrupt (%s) - recreating: %s", db_path, exc
            )
            # Close the corrupt connection before unlinking the file
            corrupt_conn = getattr(self._local, "conn", None)
            if corrupt_conn:
                try:
                    corrupt_conn.close()
                except Exception:
                    pass
            self._local = threading.local()
            Path(db_path).unlink(missing_ok=True)
            self._init_schema(self._conn)

    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS file_index (
                path        TEXT PRIMARY KEY,
                hash        TEXT NOT NULL,
                mtime       REAL NOT NULL,
                size        INTEGER NOT NULL,
                origin      TEXT NOT NULL DEFAULT 'local',
                version     INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS sync_queue (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                action      TEXT NOT NULL CHECK(action IN ('upload','delete')),
                path        TEXT NOT NULL,
                abs_path    TEXT,
                attempts    INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 10,
                next_retry  REAL NOT NULL DEFAULT 0,
                status      TEXT NOT NULL DEFAULT 'pending'
                             CHECK(status IN ('pending','processing','failed','done')),
                created_at  REAL NOT NULL DEFAULT (unixepoch('now','subsec')),
                updated_at  REAL NOT NULL DEFAULT (unixepoch('now','subsec'))
            );

            CREATE INDEX IF NOT EXISTS idx_queue_status
                ON sync_queue(status, next_retry);

            CREATE TABLE IF NOT EXISTS conflicts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                path        TEXT NOT NULL,
                conflict_file TEXT NOT NULL,
                origin      TEXT NOT NULL,
                detected_at REAL NOT NULL,
                resolved    INTEGER NOT NULL DEFAULT 0
            );
            """
        )

    # -- file_index ----------------------------------------------------------

    def get_file(self, path: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM file_index WHERE path = ?", (path,)
        ).fetchone()

    def upsert_file(
        self,
        path: str,
        hash: str,
        mtime: float,
        size: int,
        origin: str = "local",
        version: int = 1,
    ) -> None:
        self._conn.execute(
            """INSERT INTO file_index (path, hash, mtime, size, origin, version)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                   hash=excluded.hash, mtime=excluded.mtime,
                   size=excluded.size, origin=excluded.origin,
                   version=excluded.version""",
            (path, hash, mtime, size, origin, version),
        )
        self._conn.commit()

    def delete_file(self, path: str) -> None:
        self._conn.execute("DELETE FROM file_index WHERE path = ?", (path,))
        self._conn.commit()

    def all_files(self) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM file_index").fetchall()

    def file_count(self) -> int:
        row = self._conn.execute("SELECT count(*) AS c FROM file_index").fetchone()
        return row["c"] if row else 0

    def search_files(self, query: str) -> list[sqlite3.Row]:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return self._conn.execute(
            "SELECT * FROM file_index WHERE path LIKE ? ESCAPE '\\'",
            (f"%{escaped}%",),
        ).fetchall()

    # -- sync_queue ----------------------------------------------------------

    def push_task(self, action: str, path: str, abs_path: str | None = None) -> int:
        """Enqueue a sync task, deduplicating against pending tasks for the same path."""
        existing = self._conn.execute(
            "SELECT id FROM sync_queue WHERE path = ? AND action = ? AND status = 'pending'",
            (path, action),
        ).fetchone()
        if existing:
            self._conn.execute(
                "UPDATE sync_queue SET updated_at = unixepoch('now','subsec') WHERE id = ?",
                (existing["id"],),
            )
            self._conn.commit()
            return existing["id"]

        cur = self._conn.execute(
            "INSERT INTO sync_queue (action, path, abs_path) VALUES (?, ?, ?)",
            (action, path, abs_path),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def pop_task(self, now: float) -> Optional[sqlite3.Row]:
        """Atomically claim the oldest ready task (status -> processing)."""
        row = self._conn.execute(
            """SELECT * FROM sync_queue
               WHERE status = 'pending' AND next_retry <= ?
               ORDER BY created_at LIMIT 1""",
            (now,),
        ).fetchone()
        if row:
            self._conn.execute(
                "UPDATE sync_queue SET status='processing' WHERE id = ?",
                (row["id"],),
            )
            self._conn.commit()
        return row

    def complete_task(self, task_id: int) -> None:
        self._conn.execute("DELETE FROM sync_queue WHERE id = ?", (task_id,))
        self._conn.commit()

    def fail_task(self, task_id: int, next_retry: float) -> None:
        self._conn.execute(
            """UPDATE sync_queue SET
                   status='pending', attempts=attempts+1,
                   next_retry=?, updated_at=unixepoch('now','subsec')
               WHERE id = ?""",
            (next_retry, task_id),
        )
        self._conn.commit()

    def drop_stale(self, path: str, action: str) -> None:
        """Remove pending/processing tasks for a path (e.g. file was deleted before upload)."""
        self._conn.execute(
            "DELETE FROM sync_queue WHERE path = ? AND action = ? AND status IN ('pending','processing')",
            (path, action),
        )
        self._conn.commit()

    def pending_count(self) -> int:
        row = self._conn.execute(
            "SELECT count(*) AS c FROM sync_queue WHERE status = 'pending'"
        ).fetchone()
        return row["c"] if row else 0

    def all_tasks(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM sync_queue WHERE status IN ('pending','failed','processing') ORDER BY created_at"
        ).fetchall()

    def clear_pending_tasks(self) -> int:
        cur = self._conn.execute("DELETE FROM sync_queue WHERE status = 'pending'")
        self._conn.commit()
        return cur.rowcount

    def retry_task(self, task_id: int) -> bool:
        cur = self._conn.execute(
            "UPDATE sync_queue SET status='pending', next_retry=0, attempts=0 WHERE id = ?",
            (task_id,),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def mark_task_failed(self, task_id: int) -> None:
        """Permanently mark a task as failed (no more retries)."""
        self._conn.execute(
            """UPDATE sync_queue SET
                   status='failed', updated_at=unixepoch('now','subsec')
               WHERE id = ?""",
            (task_id,),
        )
        self._conn.commit()

    # -- conflicts -----------------------------------------------------------

    def record_conflict(self, path: str, conflict_file: str, origin: str) -> int:
        import time

        cur = self._conn.execute(
            "INSERT INTO conflicts (path, conflict_file, origin, detected_at) VALUES (?, ?, ?, ?)",
            (path, conflict_file, origin, time.time()),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def list_conflicts(self, resolved: bool = False) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM conflicts WHERE resolved = ? ORDER BY detected_at DESC",
            (int(resolved),),
        ).fetchall()

    def resolve_conflict_record(self, conflict_id: int) -> bool:
        cur = self._conn.execute(
            "UPDATE conflicts SET resolved = 1 WHERE id = ?", (conflict_id,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        """Close the connection for the calling thread (if any)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None
