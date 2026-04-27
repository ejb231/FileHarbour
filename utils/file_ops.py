"""Hashing and gzip compression utilities for file transfers."""

from __future__ import annotations

import gzip
import hashlib
from pathlib import Path

BUF_SIZE = 65_536

# Text-like extensions that benefit from gzip compression during transfer.
COMPRESSIBLE_EXTENSIONS = {
    ".txt",
    ".py",
    ".json",
    ".xml",
    ".csv",
    ".md",
    ".html",
    ".css",
    ".js",
    ".ts",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".log",
    ".svg",
    ".sql",
    ".sh",
    ".bat",
    ".ps1",
}


def calculate_hash(file_path: str | Path) -> str:
    """Return the SHA-256 hex digest of a file, read in 64 KiB chunks."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(BUF_SIZE):
            h.update(chunk)
    return h.hexdigest()


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def should_compress(path: str, size: int, threshold: int = 1024) -> bool:
    return size >= threshold and Path(path).suffix.lower() in COMPRESSIBLE_EXTENSIONS


def compress(data: bytes) -> bytes:
    return gzip.compress(data)


# Maximum decompressed size — matches default max_upload_mb of 500 MB.
MAX_DECOMPRESSED_BYTES = 500 * 1024 * 1024


def decompress(data: bytes, max_size: int = MAX_DECOMPRESSED_BYTES) -> bytes:
    """Decompress gzip data with a size limit to prevent gzip bombs."""
    import io

    with gzip.GzipFile(fileobj=io.BytesIO(data)) as f:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = f.read(BUF_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > max_size:
                raise ValueError(
                    f"Decompressed data exceeds {max_size // (1024 * 1024)} MB limit"
                )
            chunks.append(chunk)
    return b"".join(chunks)
