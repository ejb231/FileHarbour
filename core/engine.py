"""Initial scan of the sync folder - diffs against the DB and queues work."""

from __future__ import annotations

from pathlib import Path

from utils.file_ops import calculate_hash
from utils.logging import get_logger

log = get_logger("engine")


class SyncEngine:
    """Performs a one-time scan on startup, detecting new/modified/deleted files."""

    def __init__(self, settings, db, ignore) -> None:
        self.settings = settings
        self.db = db
        self.ignore = ignore

    def initial_scan(self) -> int:
        """Walk sync_folder, compare against the DB, and queue changes."""
        sync_root = Path(self.settings.sync_folder)
        queued = 0

        known_paths: set[str] = {row["path"] for row in self.db.all_files()}
        seen: set[str] = set()

        for file in sync_root.rglob("*"):
            if not file.is_file():
                continue

            rel = file.relative_to(sync_root).as_posix()
            if self.ignore.is_ignored(rel):
                continue
            seen.add(rel)

            stat = file.stat()
            db_row = self.db.get_file(rel)

            if not db_row:
                file_hash = calculate_hash(file)
                self.db.upsert_file(
                    rel,
                    file_hash,
                    stat.st_mtime,
                    stat.st_size,
                    origin=self.settings.node_id,
                )
                self.db.push_task("upload", rel, str(file))
                queued += 1
                log.info("New file queued: %s", rel)
                continue

            if stat.st_mtime == db_row["mtime"] and stat.st_size == db_row["size"]:
                continue

            file_hash = calculate_hash(file)
            if file_hash == db_row["hash"]:
                continue

            self.db.upsert_file(
                rel,
                file_hash,
                stat.st_mtime,
                stat.st_size,
                origin=self.settings.node_id,
                version=db_row["version"] + 1,
            )
            self.db.push_task("upload", rel, str(file))
            queued += 1
            log.info("Changed file queued: %s", rel)

        for rel in known_paths - seen:
            self.db.delete_file(rel)
            self.db.push_task("delete", rel)
            queued += 1
            log.info("Deleted file queued: %s", rel)

        log.info("Initial scan complete - %d tasks queued", queued)
        return queued
