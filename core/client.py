"""HTTP(S) client that pushes file changes to the central SyncCore server."""

from __future__ import annotations

import os
import ssl
from pathlib import Path
from urllib.parse import urljoin

import httpx

from utils.file_ops import compress, should_compress
from utils.logging import get_logger

log = get_logger("client")


def _make_ssl_ctx(cert_path: str, verify: bool = False):
    """Build an SSL context for optional certificate verification."""
    if not verify:
        return False
    if Path(cert_path).is_file():
        return ssl.create_default_context(cafile=cert_path)
    return True


class SyncClient:
    """Uploads and deletes files on a single configured server."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.target = settings.server_url.rstrip("/")
        self._client = httpx.Client(
            timeout=60.0,
            verify=_make_ssl_ctx(settings.ssl_cert, verify=settings.verify_tls),
        )

    def _auth_headers(self) -> dict[str, str]:
        return {"X-API-Key": self.settings.api_key}

    def upload_file(
        self,
        file_path: str,
        relative_path: str,
        base_hash: str | None = None,
    ) -> None:
        raw = Path(file_path).read_bytes()
        use_compression = should_compress(relative_path, len(raw))
        payload = compress(raw) if use_compression else raw

        data = {
            "path": relative_path,
            "origin": self.settings.node_id,
            "compressed": "true" if use_compression else "false",
        }
        if base_hash:
            data["base_hash"] = base_hash

        url = urljoin(f"{self.target}/", "upload")
        files = {"file": (os.path.basename(file_path), payload)}

        try:
            response = self._client.post(
                url,
                data=data,
                files=files,
                headers=self._auth_headers(),
            )
            response.raise_for_status()
            body = response.json()
            if body.get("status") == "conflict":
                log.warning(
                    "Conflict on %s at %s -> %s",
                    relative_path,
                    self.target,
                    body.get("conflict_file"),
                )
            else:
                log.info("Uploaded %s -> %s", relative_path, self.target)
        except httpx.HTTPStatusError as exc:
            log.error(
                "HTTP %d from %s for %s",
                exc.response.status_code,
                self.target,
                relative_path,
            )
            raise
        except httpx.RequestError as exc:
            log.error(
                "Network error uploading %s to %s: %s",
                relative_path,
                self.target,
                exc,
            )
            raise

    def delete_file(self, relative_path: str) -> None:
        url = urljoin(f"{self.target}/", "delete")
        try:
            response = self._client.delete(
                url,
                params={"path": relative_path},
                headers=self._auth_headers(),
            )
            response.raise_for_status()
            log.info("Deleted %s on %s", relative_path, self.target)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                log.debug("Already gone on %s: %s", self.target, relative_path)
            else:
                log.error(
                    "HTTP %d from %s deleting %s",
                    exc.response.status_code,
                    self.target,
                    relative_path,
                )
                raise
        except httpx.RequestError as exc:
            log.error(
                "Network error deleting %s on %s: %s",
                relative_path,
                self.target,
                exc,
            )
            raise

    def close(self) -> None:
        self._client.close()
