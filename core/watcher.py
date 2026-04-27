"""Real-time file system monitoring via watchdog."""

from __future__ import annotations

from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from core.server import consume_server_write
from utils.file_ops import calculate_hash
from utils.logging import get_logger
from utils.resilience import RenameDetector

log = get_logger("watcher")

# Module-level rename detector shared across handler instances
_rename_detector = RenameDetector(window=2.0)


class SyncHandler(FileSystemEventHandler):
    """Reacts to local filesystem events and pushes tasks into the sync queue."""

    def __init__(self, settings, db, ignore) -> None:
        self.settings = settings
        self.db = db
        self.ignore = ignore
        self.sync_root = self._normalize(Path(settings.sync_folder).resolve())

    @staticmethod
    def _normalize(p: Path) -> Path:
        """Strip the Windows ``\\\\?\\`` extended-length prefix if present.

        ``Path.resolve()`` on modern Python/Windows can return paths with
        this prefix, which then fail ``relative_to()`` against a root that
        was resolved without it.
        """
        s = str(p)
        if s.startswith("\\\\?\\"):
            return Path(s[4:])
        return p

    def _rel(self, absolute_path: str) -> str:
        resolved = self._normalize(Path(absolute_path).resolve())
        return resolved.relative_to(self.sync_root).as_posix()

    def _should_skip(self, event) -> bool:
        if event.is_directory:
            return True
        # Ignore temporary files created during atomic writes
        if event.src_path.endswith(".synctmp"):
            return True
        rel = self._rel(event.src_path)
        return self.ignore.is_ignored(rel) or consume_server_write(rel)

    def on_created(self, event) -> None:
        if self._should_skip(event):
            return
        rel = self._rel(event.src_path)
        abs_path = str(Path(event.src_path).resolve())
        try:
            h = calculate_hash(abs_path)
            stat = Path(abs_path).stat()

            # Check if this is actually a rename (same hash as a recent delete)
            old_rel = _rename_detector.check_create(h)
            if old_rel and old_rel != rel:
                log.info("Rename detected: %s -> %s", old_rel, rel)
                # The delete task was already queued; just record the new file
                # and queue an upload for it (avoids re-downloading content)
                self.db.upsert_file(
                    rel,
                    h,
                    stat.st_mtime,
                    stat.st_size,
                    origin=self.settings.node_id,
                )
                self.db.push_task("upload", rel, abs_path)
                return

            self.db.upsert_file(
                rel, h, stat.st_mtime, stat.st_size, origin=self.settings.node_id
            )
            self.db.push_task("upload", rel, abs_path)
            log.info("Created -> queued: %s", rel)
        except (OSError, PermissionError) as exc:
            log.warning("Skipped create %s: %s", rel, exc)

    def on_modified(self, event) -> None:
        if self._should_skip(event):
            return
        rel = self._rel(event.src_path)
        abs_path = str(Path(event.src_path).resolve())
        try:
            h = calculate_hash(abs_path)
            stat = Path(abs_path).stat()
            db_row = self.db.get_file(rel)
            if db_row and db_row["hash"] == h:
                return
            version = (db_row["version"] + 1) if db_row else 1
            self.db.upsert_file(
                rel,
                h,
                stat.st_mtime,
                stat.st_size,
                origin=self.settings.node_id,
                version=version,
            )
            self.db.push_task("upload", rel, abs_path)
            log.info("Modified -> queued: %s", rel)
        except (OSError, PermissionError) as exc:
            log.warning("Skipped modify %s: %s", rel, exc)

    def on_deleted(self, event) -> None:
        if self._should_skip(event):
            return
        rel = self._rel(event.src_path)

        # Record the hash of the deleted file for rename detection
        db_row = self.db.get_file(rel)
        if db_row:
            _rename_detector.record_delete(rel, db_row["hash"])

        self.db.delete_file(rel)
        self.db.drop_stale(rel, "upload")
        self.db.push_task("delete", rel)
        log.info("Deleted -> queued: %s", rel)


class FileWatcher:
    """Wraps watchdog's Observer for the sync folder with auto-restart."""

    def __init__(self, settings, db, ignore) -> None:
        self.settings = settings
        self.db = db
        self.ignore = ignore
        self._observer: Observer | None = None

    def start(self) -> None:
        """Create a fresh Observer each time (threads cannot be restarted)."""
        self._observer = Observer()
        handler = SyncHandler(self.settings, self.db, self.ignore)
        self._observer.schedule(handler, self.settings.sync_folder, recursive=True)
        self._observer.start()
        log.info("Watcher started on %s", self.settings.sync_folder)

    def stop(self) -> None:
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=5)
            except Exception as exc:
                log.warning("Error stopping watcher: %s", exc)
            self._observer = None
        log.info("Watcher stopped")
