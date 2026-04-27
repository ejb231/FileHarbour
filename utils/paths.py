"""Path validation utilities to prevent unsafe sync folder choices."""

from __future__ import annotations

import platform
from pathlib import Path

# Directories that must never be used as a sync folder.
_WINDOWS_BLOCKED = {
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "recovery",
    "system volume information",
    "$recycle.bin",
    "boot",
}

_UNIX_BLOCKED = {
    "/",
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib64",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/sys",
    "/usr",
    "/var",
}


def _is_drive_root(p: Path) -> bool:
    return p == p.anchor or str(p) in (p.drive + "\\", p.drive + "/", "/")


def validate_folder_path(path: str, label: str = "folder") -> str:
    """Validate that *path* is safe for use as a sync folder.

    Returns the resolved path string on success.
    Raises ``ValueError`` with a human-readable message on failure.
    """
    if not path or not path.strip():
        raise ValueError(f"{label} path must not be empty")

    resolved = Path(path).resolve()

    if not resolved.is_absolute():
        raise ValueError(f"{label} path must be absolute")

    if _is_drive_root(resolved):
        raise ValueError(f"{label} must not be a filesystem root ({resolved})")

    if platform.system() == "Windows":
        parts_lower = [p.lower() for p in resolved.parts]
        for blocked in _WINDOWS_BLOCKED:
            if len(parts_lower) >= 2 and parts_lower[1] == blocked:
                raise ValueError(
                    f"{label} must not be inside a system directory "
                    f"({resolved.parts[0]}\\{resolved.parts[1]})"
                )
    else:
        for blocked in _UNIX_BLOCKED:
            blocked_path = Path(blocked)
            if resolved == blocked_path:
                raise ValueError(f"{label} must not be a system directory ({blocked})")
            if (
                blocked != "/"
                and str(resolved).startswith(blocked + "/")
                and len(resolved.parts) <= len(blocked_path.parts) + 1
            ):
                raise ValueError(
                    f"{label} must not be inside a system directory ({blocked})"
                )

    return str(resolved)
