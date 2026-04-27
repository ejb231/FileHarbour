"""Background thread that drains the sync queue and dispatches to the server."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from utils.logging import get_logger

log = get_logger("queue_worker")

MIN_BACKOFF = 1.0
MAX_BACKOFF = 300.0
POLL_INTERVAL = 0.5


def _backoff(attempts: int) -> float:
    return min(MIN_BACKOFF * (2**attempts), MAX_BACKOFF)


class QueueWorker:
    """Continuously pops tasks from the database queue and syncs them."""

    def __init__(self, db, client, settings) -> None:
        self.db = db
        self.client = client
        self.settings = settings
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def is_paused(self) -> bool:
        return self._pause.is_set()

    def pause(self) -> None:
        self._pause.set()
        log.info("Queue worker paused")

    def resume(self) -> None:
        self._pause.clear()
        log.info("Queue worker resumed")

    def clear_all(self) -> int:
        count = self.db.clear_pending_tasks()
        log.info("Cleared %d pending tasks", count)
        return count

    def retry_task(self, task_id: int) -> bool:
        ok = self.db.retry_task(task_id)
        if ok:
            log.info("Task %d reset for retry", task_id)
        return ok

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="queue-worker"
        )
        self._thread.start()
        log.info("Queue worker started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        log.info("Queue worker stopped")

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._pause.is_set():
                self._stop.wait(POLL_INTERVAL)
                continue
            task = self.db.pop_task(time.time())
            if task is None:
                self._stop.wait(POLL_INTERVAL)
                continue
            self._process(task)

    def _process(self, task) -> None:
        action = task["action"]
        rel_path = task["path"]
        abs_path = task["abs_path"]
        task_id = task["id"]
        attempts = task["attempts"]

        try:
            if action == "upload":
                if abs_path and Path(abs_path).is_file():
                    db_row = self.db.get_file(rel_path)
                    # base_hash is only useful when this client previously
                    # indexed a server-originated version of the same file.
                    base_hash = None
                    if db_row and db_row["origin"] != self.settings.node_id:
                        base_hash = db_row["hash"]
                    self.client.upload_file(abs_path, rel_path, base_hash=base_hash)
                else:
                    log.warning("File gone before upload: %s - dropping task", rel_path)
                    self.db.complete_task(task_id)
                    return
            elif action == "delete":
                self.client.delete_file(rel_path)

            self.db.complete_task(task_id)
            log.debug("Task %d completed: %s %s", task_id, action, rel_path)

        except Exception as exc:
            next_attempts = attempts + 1
            if next_attempts >= task["max_retries"]:
                log.error(
                    "Task %d permanently failed after %d attempts: %s %s — %s",
                    task_id,
                    next_attempts,
                    action,
                    rel_path,
                    exc,
                )
                self.db.mark_task_failed(task_id)
            else:
                wait = _backoff(attempts)
                log.warning(
                    "Task %d failed (attempt %d/%d): %s - retrying in %.1fs",
                    task_id,
                    next_attempts,
                    task["max_retries"],
                    exc,
                    wait,
                )
                self.db.fail_task(task_id, time.time() + wait)
