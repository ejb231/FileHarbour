"""Component lifecycle manager for the server-side sync pipeline."""

from __future__ import annotations

import threading

from utils.logging import get_logger

log = get_logger("orchestrator")


class Orchestrator:
    """Coordinates the watcher, queue worker, and sync client lifecycle."""

    def __init__(self, settings, db, ignore, watcher, queue_worker, client) -> None:
        self.settings = settings
        self.db = db
        self.ignore = ignore
        self.watcher = watcher
        self.queue_worker = queue_worker
        self.client = client
        self._lock = threading.Lock()

    def start_all(self) -> None:
        with self._lock:
            if self.queue_worker:
                self.queue_worker.start()
            if self.watcher:
                self.watcher.start()
            log.info("All components started")

    def stop_all(self) -> None:
        with self._lock:
            for name in ("watcher", "queue_worker"):
                comp = getattr(self, name, None)
                if comp and hasattr(comp, "stop"):
                    try:
                        comp.stop()
                    except Exception as exc:
                        log.warning("Error stopping %s: %s", name, exc)

            if self.client:
                try:
                    self.client.close()
                except Exception as exc:
                    log.warning("Error closing client: %s", exc)

            log.info("All components stopped")

    def restart_component(self, name: str) -> None:
        with self._lock:
            comp = getattr(self, name, None)
            if comp is None:
                raise ValueError(f"Unknown component: {name}")
            if hasattr(comp, "stop"):
                comp.stop()
            if hasattr(comp, "start"):
                comp.start()
            log.info("Restarted component: %s", name)

    def reconfigure(self, new_settings) -> None:
        """Stop all components, apply new settings, then restart."""
        with self._lock:
            for name in ("watcher", "queue_worker"):
                comp = getattr(self, name, None)
                if comp and hasattr(comp, "stop"):
                    comp.stop()

            self.settings = new_settings

            for comp in (self.queue_worker, self.watcher, self.client):
                if comp is not None:
                    comp.settings = new_settings

            for name in ("queue_worker", "watcher"):
                comp = getattr(self, name, None)
                if comp and hasattr(comp, "start"):
                    comp.start()

            log.info("Reconfigured and restarted all components")
