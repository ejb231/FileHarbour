"""Conflict resolution - keep both versions when a file diverges."""

from __future__ import annotations

import datetime
from pathlib import Path


def make_conflict_name(path: str, node_id: str) -> str:
    """Build a conflict filename, e.g. ``report (Conflict node-2 2025-02-26_143012).txt``."""
    p = Path(path)
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    return f"{p.stem} (Conflict {node_id} {ts}){p.suffix}"


def resolve_conflict(
    existing_path: Path,
    incoming_data: bytes,
    incoming_hash: str,
    node_id: str,
    db=None,
) -> Path:
    """Write the incoming version alongside the existing one as a conflict copy."""
    conflict_name = make_conflict_name(existing_path.name, node_id)
    conflict_path = existing_path.with_name(conflict_name)
    conflict_path.write_bytes(incoming_data)
    if db is not None:
        db.record_conflict(existing_path.name, conflict_name, node_id)
    return conflict_path
