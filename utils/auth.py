"""Authentication helpers for the one-way sync API."""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Request


def _safe_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


async def require_api_key(request: Request, x_api_key: str = Header(None)) -> str:
    """Reject requests without a valid ``X-API-Key`` header."""
    expected = request.app.state.settings.api_key
    if not x_api_key or not _safe_compare(x_api_key, expected):
        raise HTTPException(status_code=403, detail="Invalid or missing API key")
    return x_api_key
