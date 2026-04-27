"""FastAPI application for the one-way SyncCore server."""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile

from utils.auth import require_api_key
from utils.conflict import resolve_conflict
from utils.file_ops import calculate_hash, decompress, hash_bytes
from utils.logging import get_logger

log = get_logger("server")

app = FastAPI(title="SyncCore Server")


class RateLimiter:
    """Sliding-window rate limiter keyed by IP address."""

    def __init__(self, window: float = 60.0, limit: int = 60) -> None:
        self._window = window
        self._limit = limit
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            timestamps = self._hits[key]
            timestamps[:] = [stamp for stamp in timestamps if now - stamp < self._window]
            if len(timestamps) >= self._limit:
                return False
            timestamps.append(now)
            return True


_upload_limiter = RateLimiter(window=60.0, limit=60)


class _WriteGuard:
    """Thread-safe set of paths recently written by the server."""

    def __init__(self, ttl: float = 5.0) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, float] = {}
        self._ttl = ttl

    @staticmethod
    def _normalize(path: str) -> str:
        return path.replace("\\", "/")

    def mark(self, path: str) -> None:
        key = self._normalize(path)
        with self._lock:
            self._entries[key] = time.monotonic() + self._ttl

    def consume(self, path: str) -> bool:
        key = self._normalize(path)
        now = time.monotonic()
        with self._lock:
            expiry = self._entries.get(key)
            if expiry is not None and now < expiry:
                del self._entries[key]
                return True
            self._entries.pop(key, None)
            return False


_write_guard = _WriteGuard()


def mark_server_write(path: str) -> None:
    _write_guard.mark(path)


def consume_server_write(path: str) -> bool:
    return _write_guard.consume(path)


def _resolve_sync_path(sync_root: Path, relative_path: str) -> Path:
    dest = (sync_root / relative_path).resolve()
    try:
        dest.relative_to(sync_root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Path traversal blocked") from exc
    return dest


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/index", dependencies=[Depends(require_api_key)])
async def get_index(request: Request):
    db = request.app.state.db
    return [dict(row) for row in db.all_files()]


@app.post("/upload", dependencies=[Depends(require_api_key)])
async def upload_file(
    request: Request,
    path: str = Form(...),
    base_hash: str | None = Form(None),
    origin: str = Form("client"),
    compressed: str = Form("false"),
    file: UploadFile = File(...),
):
    client_ip = request.client.host if request.client else "unknown"
    if not _upload_limiter.allow(client_ip):
        raise HTTPException(status_code=429, detail="Upload rate limit exceeded")

    settings = request.app.state.settings
    db = request.app.state.db
    sync_root = Path(settings.sync_folder).resolve()
    dest = _resolve_sync_path(sync_root, path)

    try:
        data = await file.read()
        max_bytes = getattr(settings, "max_upload_mb", 500) * 1_048_576
        if len(data) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File exceeds {settings.max_upload_mb} MB limit",
            )

        if compressed == "true":
            try:
                data = decompress(data)
            except ValueError as exc:
                raise HTTPException(status_code=413, detail=str(exc))

        incoming_hash = hash_bytes(data)

        if dest.is_file() and base_hash:
            local_hash = calculate_hash(dest)
            if local_hash != base_hash and local_hash != incoming_hash:
                conflict_path = resolve_conflict(
                    dest, data, incoming_hash, origin, db=db
                )
                log.warning("Conflict on %s -> saved as %s", path, conflict_path.name)
                return {
                    "status": "conflict",
                    "path": path,
                    "conflict_file": conflict_path.name,
                }

        dest.parent.mkdir(parents=True, exist_ok=True)
        mark_server_write(path)

        tmp = dest.with_suffix(dest.suffix + ".synctmp")
        try:
            tmp.write_bytes(data)
            os.replace(str(tmp), str(dest))
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

        stat = dest.stat()
        db.upsert_file(path, incoming_hash, stat.st_mtime, stat.st_size, origin=origin)

        log.info("Received: %s (%d bytes)", path, len(data))
        return {"status": "success", "path": path}
    except OSError as exc:
        log.error("Disk error writing %s: %s", path, exc)
        raise HTTPException(status_code=500, detail=f"Disk error: {exc}")
    finally:
        await file.close()


@app.delete("/delete", dependencies=[Depends(require_api_key)])
async def delete_file(request: Request, path: str):
    settings = request.app.state.settings
    db = request.app.state.db
    sync_root = Path(settings.sync_folder).resolve()
    dest = _resolve_sync_path(sync_root, path)
    if not dest.exists():
        raise HTTPException(status_code=404, detail="File not found")

    try:
        mark_server_write(path)
        dest.unlink()
        db.delete_file(path)
        log.info("Deleted: %s", path)
        return {"status": "deleted", "path": path}
    except Exception as exc:
        log.error("Error deleting %s: %s", path, exc)
        raise HTTPException(status_code=500, detail="Delete failed")
