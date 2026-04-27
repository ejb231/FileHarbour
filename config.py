"""Application settings and .env file management."""

from __future__ import annotations

import os
import secrets
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__version__ = "1.1.0"


def get_app_dir() -> Path:
    """Return the base directory for config, certs, and data."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


_BASE_DIR = get_app_dir()

load_dotenv(_BASE_DIR / ".env", override=True)


def write_env(updates: dict, env_path: str | None = None) -> None:
    """Merge *updates* into the .env file, preserving comments and ordering."""
    path = Path(env_path) if env_path else _BASE_DIR / ".env"
    lines: list[str] = []
    existing_keys: set[str] = set()

    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if "=" in stripped and not stripped.startswith("#"):
                key = stripped.split("=", 1)[0].strip().upper()
                if key in {k.upper() for k in updates}:
                    existing_keys.add(key)
                    new_val = next(v for k, v in updates.items() if k.upper() == key)
                    lines.append(f"{key}={new_val}")
                    continue
            lines.append(line)

    for key, value in updates.items():
        if key.upper() not in existing_keys:
            lines.append(f"{key.upper()}={value}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def bootstrap_env() -> bool:
    """Create a .env with sensible defaults on first run."""
    env_path = _BASE_DIR / ".env"
    if env_path.is_file():
        return False

    defaults = {
        "SYNC_FOLDER": str(_BASE_DIR / "data" / "sync_folder"),
        "SERVER_URL": "https://localhost:8443",
        "PORT": "8443",
        "API_KEY": secrets.token_urlsafe(32),
        "NODE_ID": str(uuid.uuid4())[:8],
        "SSL_CERT": str(_BASE_DIR / "cert.pem"),
        "SSL_KEY": str(_BASE_DIR / "key.pem"),
        "DB_PATH": str(_BASE_DIR / "data" / "sync.db"),
        "LOG_LEVEL": "INFO",
        "MAX_UPLOAD_MB": "500",
        "VERIFY_TLS": "false",
        "DEBUG": "false",
    }

    header = (
        "# SyncCore configuration - auto-generated on first run.\n"
        "# SERVER_URL is the central server clients upload to.\n\n"
    )
    body = "\n".join(f"{key}={value}" for key, value in defaults.items()) + "\n"
    env_path.write_text(header + body, encoding="utf-8")

    if os.name == "nt":
        try:
            username = os.environ.get("USERNAME", "")
            if username:
                import subprocess

                subprocess.run(
                    [
                        "icacls",
                        str(env_path),
                        "/inheritance:r",
                        "/grant:r",
                        f"{username}:(F)",
                    ],
                    capture_output=True,
                    timeout=10,
                )
        except Exception:
            pass
    else:
        os.chmod(str(env_path), 0o600)

    load_dotenv(env_path, override=True)
    return True


class Settings(BaseSettings):
    """Typed settings loaded from environment variables and .env."""

    sync_folder: str = str(_BASE_DIR / "data" / "sync_folder")
    server_url: str = "https://localhost:8443"
    port: int = 8443
    api_key: str = "change-me"
    node_id: str = str(uuid.uuid4())[:8]
    ssl_cert: str = str(_BASE_DIR / "cert.pem")
    ssl_key: str = str(_BASE_DIR / "key.pem")
    db_path: str = str(_BASE_DIR / "data" / "sync.db")
    log_level: str = "INFO"
    syncignore_path: str = str(_BASE_DIR / ".syncignore")
    debug: bool = False
    max_upload_mb: int = 500
    verify_tls: bool = False

    @field_validator("port")
    @classmethod
    def _port_range(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("port must be 1-65535")
        return value

    @model_validator(mode="after")
    def _validate_folders(self) -> Settings:
        from utils.paths import validate_folder_path

        try:
            validate_folder_path(self.sync_folder, label="sync_folder")
        except ValueError:
            import logging

            logging.getLogger("sync.config").warning(
                "Potentially unsafe sync_folder: %s", self.sync_folder
            )
        return self

    def ensure_folders(self) -> None:
        Path(self.sync_folder).mkdir(parents=True, exist_ok=True)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def reload(cls) -> Settings:
        load_dotenv(_BASE_DIR / ".env", override=True)
        return cls()

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
