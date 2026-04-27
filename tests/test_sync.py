from __future__ import annotations

import time
from pathlib import Path

import pytest

from config import Settings
from core.engine import SyncEngine
from utils.certs import generate_self_signed_cert
from utils.conflict import make_conflict_name, resolve_conflict
from utils.file_index import Database
from utils.file_ops import (
    calculate_hash,
    compress,
    decompress,
    hash_bytes,
    should_compress,
)
from utils.filters import SyncIgnore


@pytest.fixture()
def tmp(tmp_path):
    return tmp_path


@pytest.fixture()
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


@pytest.fixture()
def settings(tmp_path):
    sync = tmp_path / "sync"
    sync.mkdir()
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    generate_self_signed_cert(cert_path, key_path)
    return Settings(
        sync_folder=str(sync),
        server_url="https://localhost:8443",
        db_path=str(tmp_path / "test.db"),
        ssl_cert=str(cert_path),
        ssl_key=str(key_path),
        api_key="test-key",
        node_id="test-node",
        port=19876,
    )


class TestFileOps:
    def test_calculate_hash_deterministic(self, tmp):
        file = tmp / "hello.txt"
        file.write_text("hello world")
        assert calculate_hash(file) == calculate_hash(file)

    def test_hash_bytes(self):
        data = b"test data"
        assert hash_bytes(data) == hash_bytes(data)
        assert len(hash_bytes(data)) == 64

    def test_compress_decompress_roundtrip(self):
        original = b"a" * 10_000
        compressed = compress(original)
        assert len(compressed) < len(original)
        assert decompress(compressed) == original

    def test_should_compress(self):
        assert should_compress("readme.md", 2048) is True
        assert should_compress("photo.jpg", 2048) is False
        assert should_compress("tiny.txt", 100) is False


class TestSyncIgnore:
    def test_ignores_matching_pattern(self, tmp):
        ignore_file = tmp / ".syncignore"
        ignore_file.write_text("*.tmp\n__pycache__/\n")
        syncignore = SyncIgnore(ignore_file)
        assert syncignore.is_ignored("data.tmp") is True
        assert syncignore.is_ignored("src/__pycache__/mod.pyc") is True
        assert syncignore.is_ignored("readme.md") is False

    def test_empty_file(self, tmp):
        ignore_file = tmp / ".syncignore"
        ignore_file.write_text("")
        syncignore = SyncIgnore(ignore_file)
        assert syncignore.is_ignored("anything.py") is False

    def test_missing_file(self, tmp):
        syncignore = SyncIgnore(tmp / "missing")
        assert syncignore.is_ignored("file.txt") is False


class TestDatabase:
    def test_upsert_and_get(self, db):
        db.upsert_file("a/b.txt", "abc123", 1000.0, 42, origin="node-1")
        row = db.get_file("a/b.txt")
        assert row is not None
        assert row["hash"] == "abc123"
        assert row["origin"] == "node-1"

    def test_delete(self, db):
        db.upsert_file("x.txt", "h", 1.0, 1)
        db.delete_file("x.txt")
        assert db.get_file("x.txt") is None

    def test_queue_push_deduplicates(self, db):
        id1 = db.push_task("upload", "f.txt", "/abs/f.txt")
        id2 = db.push_task("upload", "f.txt", "/abs/f.txt")
        assert id1 == id2

    def test_queue_pop_and_complete(self, db):
        db.push_task("delete", "old.txt")
        task = db.pop_task(time.time() + 1)
        assert task is not None
        assert task["action"] == "delete"
        db.complete_task(task["id"])
        assert db.pending_count() == 0

    def test_queue_fail_and_retry(self, db):
        db.push_task("upload", "retry.txt", "/abs/retry.txt")
        task = db.pop_task(time.time() + 1)
        db.fail_task(task["id"], time.time() + 100)
        assert db.pop_task(time.time()) is None
        task2 = db.pop_task(time.time() + 200)
        assert task2 is not None

    def test_retry_task(self, db):
        db.push_task("upload", "rt.txt", "/rt")
        task = db.pop_task(time.time() + 1)
        db.fail_task(task["id"], time.time() + 9999)
        assert db.retry_task(task["id"]) is True
        retried = db.pop_task(time.time() + 1)
        assert retried is not None
        assert retried["attempts"] == 0


class TestConflictResolver:
    def test_conflict_name_format(self):
        name = make_conflict_name("report.txt", "node-2")
        assert "Conflict" in name
        assert "node-2" in name
        assert name.endswith(".txt")

    def test_resolve_conflict_creates_file(self, tmp):
        original = tmp / "doc.txt"
        original.write_text("version A")
        incoming = b"version B"
        result = resolve_conflict(original, incoming, hash_bytes(incoming), "client-1")
        assert result.exists()
        assert result.read_bytes() == incoming
        assert original.read_text() == "version A"

    def test_resolve_conflict_with_db(self, tmp, db):
        original = tmp / "doc.txt"
        original.write_text("version A")
        incoming = b"version B"
        resolve_conflict(original, incoming, hash_bytes(incoming), "client-1", db=db)
        conflicts = db.list_conflicts(resolved=False)
        assert len(conflicts) == 1
        assert conflicts[0]["origin"] == "client-1"


class TestEngine:
    def test_initial_scan_queues_new_and_deleted_files(self, settings, db, tmp):
        ignore_file = tmp / ".syncignore"
        ignore_file.write_text("")
        ignore = SyncIgnore(ignore_file)

        current = Path(settings.sync_folder) / "current.txt"
        current.write_text("hello")
        db.upsert_file("gone.txt", "hash", 1.0, 1, origin=settings.node_id)

        engine = SyncEngine(settings, db, ignore)
        queued = engine.initial_scan()

        assert queued == 2
        tasks = db.all_tasks()
        assert {task["path"] for task in tasks} == {"current.txt", "gone.txt"}


class TestServerEndpoints:
    @pytest.fixture(autouse=True)
    def setup_app(self, settings, db):
        from fastapi.testclient import TestClient
        from core.server import app

        app.state.settings = settings
        app.state.db = db
        self.client = TestClient(app)
        self.headers = {"x-api-key": "test-key"}
        self.settings = settings

    def test_health(self):
        response = self.client.get("/health")
        assert response.status_code == 200

    def test_upload_success(self):
        response = self.client.post(
            "/upload",
            data={"path": "hello.txt", "origin": "test-node", "compressed": "false"},
            files={"file": ("hello.txt", b"hello world")},
            headers=self.headers,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "success"
        dest = Path(self.settings.sync_folder) / "hello.txt"
        assert dest.read_bytes() == b"hello world"

    def test_upload_compressed(self):
        payload = compress(b"compressed payload")
        response = self.client.post(
            "/upload",
            data={"path": "comp.txt", "origin": "node-1", "compressed": "true"},
            files={"file": ("comp.txt", payload)},
            headers=self.headers,
        )
        assert response.status_code == 200
        dest = Path(self.settings.sync_folder) / "comp.txt"
        assert dest.read_bytes() == b"compressed payload"

    def test_upload_rejects_bad_key(self):
        response = self.client.post(
            "/upload",
            data={"path": "x.txt", "origin": "node-1", "compressed": "false"},
            files={"file": ("x.txt", b"data")},
            headers={"x-api-key": "wrong"},
        )
        assert response.status_code == 403

    def test_upload_conflict_detection(self):
        dest = Path(self.settings.sync_folder) / "conflict.txt"
        dest.write_text("local version")
        response = self.client.post(
            "/upload",
            data={
                "path": "conflict.txt",
                "origin": "client-2",
                "compressed": "false",
                "base_hash": "stale-hash",
            },
            files={"file": ("conflict.txt", b"remote version")},
            headers=self.headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "conflict"
        assert "conflict_file" in body

    def test_delete_success(self):
        dest = Path(self.settings.sync_folder) / "bye.txt"
        dest.write_text("gone soon")
        response = self.client.delete(
            "/delete", params={"path": "bye.txt"}, headers=self.headers
        )
        assert response.status_code == 200
        assert not dest.exists()

    def test_delete_not_found(self):
        response = self.client.delete(
            "/delete", params={"path": "nope.txt"}, headers=self.headers
        )
        assert response.status_code == 404

    def test_path_traversal_blocked(self):
        response = self.client.post(
            "/upload",
            data={"path": "../../etc/passwd", "origin": "node-1", "compressed": "false"},
            files={"file": ("passwd", b"bad")},
            headers=self.headers,
        )
        assert response.status_code == 403

    def test_index_returns_files(self, db):
        db.upsert_file("indexed.txt", "abc", 1.0, 5)
        response = self.client.get("/index", headers=self.headers)
        assert response.status_code == 200
        data = response.json()
        assert any(file["path"] == "indexed.txt" for file in data)
