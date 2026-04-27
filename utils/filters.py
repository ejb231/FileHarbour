"""Glob-based ignore filtering (.syncignore support)."""

from __future__ import annotations

import fnmatch
from pathlib import Path


class SyncIgnore:
    """Loads glob patterns from a .syncignore file and tests paths against them."""

    def __init__(self, ignore_path: str | Path) -> None:
        self.patterns: list[str] = []
        path = Path(ignore_path)
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    self.patterns.append(stripped)

    def is_ignored(self, relative_path: str) -> bool:
        """Return True if *relative_path* matches any ignore pattern."""
        parts = Path(relative_path).parts
        for pattern in self.patterns:
            clean = pattern.rstrip("/")
            if fnmatch.fnmatch(relative_path, clean):
                return True
            if any(fnmatch.fnmatch(part, clean) for part in parts):
                return True
        return False
