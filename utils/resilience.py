"""Resilience utilities: supervised threads, file locking, rename detection."""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from utils.logging import get_logger

log = get_logger("resilience")


# ---------------------------------------------------------------------------
# Supervised thread wrapper — auto-restarts on unhandled exceptions
# ---------------------------------------------------------------------------


class SupervisedThread:
    """Wraps a ``target`` callable in a daemon thread that automatically
    restarts when an unhandled exception is raised.

    *restart_delay* is the number of seconds to wait between restarts.
    *max_restarts* limits how many consecutive restarts are allowed
    before giving up (-1 = unlimited).
    """

    def __init__(
        self,
        target: Callable[[], None],
        name: str,
        restart_delay: float = 2.0,
        max_restarts: int = 10,
        on_failure: Callable[[str, Exception], None] | None = None,
    ) -> None:
        self._target = target
        self._name = name
        self._restart_delay = restart_delay
        self._max_restarts = max_restarts
        self._on_failure = on_failure
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._restart_count = 0

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._target()
                break  # clean exit
            except Exception as exc:
                self._restart_count += 1
                log.error(
                    "Thread '%s' crashed (restart %d/%s): %s",
                    self._name,
                    self._restart_count,
                    self._max_restarts if self._max_restarts >= 0 else "∞",
                    exc,
                    exc_info=True,
                )
                if self._on_failure:
                    try:
                        self._on_failure(self._name, exc)
                    except Exception:
                        pass

                if 0 <= self._max_restarts <= self._restart_count:
                    log.critical(
                        "Thread '%s' exceeded max restarts — giving up", self._name
                    )
                    return

                if not self._stop.is_set():
                    self._stop.wait(self._restart_delay)

    def start(self) -> None:
        self._stop.clear()
        self._restart_count = 0
        self._thread = threading.Thread(target=self._loop, daemon=True, name=self._name)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


# ---------------------------------------------------------------------------
# File locking — prevents reads of partially-written files
# ---------------------------------------------------------------------------


@contextmanager
def atomic_write(dest: Path, data: bytes, ttl_guard=None, guard_key: str | None = None):
    """Write *data* to *dest* atomically via a temporary file + rename.

    On Windows, ``os.replace`` is atomic at the filesystem level.
    If *ttl_guard* (a WriteGuard instance) and *guard_key* are given,
    the guard is marked before the rename.
    """
    tmp = dest.with_suffix(dest.suffix + ".synctmp")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)
        if ttl_guard and guard_key:
            ttl_guard.mark(guard_key)
        os.replace(str(tmp), str(dest))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        # Clean up stale tmp in case of partial failure
        tmp.unlink(missing_ok=True)
    yield dest


# ---------------------------------------------------------------------------
# Rename detection helper
# ---------------------------------------------------------------------------


class RenameDetector:
    """Detect file renames by tracking recent deletes and matching hashes.

    When a file is deleted, its hash is remembered for *window* seconds.
    If a new file appears with the same hash within that window, it is
    treated as a rename rather than a separate delete + create.
    """

    def __init__(self, window: float = 2.0) -> None:
        self._window = window
        self._lock = threading.Lock()
        # rel_path -> (hash, abs_path, timestamp)
        self._deleted: dict[str, tuple[str, str, float]] = {}

    def record_delete(self, rel_path: str, file_hash: str, abs_path: str = "") -> None:
        now = time.monotonic()
        with self._lock:
            self._deleted[rel_path] = (file_hash, abs_path, now)
            # Prune expired entries
            cutoff = now - self._window
            expired = [k for k, v in self._deleted.items() if v[2] < cutoff]
            for k in expired:
                del self._deleted[k]

    def check_create(self, file_hash: str) -> str | None:
        """If a recently deleted file matches *file_hash*, return its old
        relative path and remove the entry.  Otherwise return ``None``."""
        now = time.monotonic()
        with self._lock:
            for rel, (h, _, ts) in list(self._deleted.items()):
                if h == file_hash and (now - ts) < self._window:
                    del self._deleted[rel]
                    return rel
        return None
