"""Self-signed TLS certificate generation helpers."""

from __future__ import annotations

import datetime
import ipaddress
import os
import platform
import subprocess
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def _restrict_windows_acl(path: Path) -> None:
    """Restrict a file to the current user only via icacls on Windows."""
    try:
        username = os.environ.get("USERNAME", "")
        if not username:
            return
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{username}:(R)"],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass


def generate_self_signed_cert(
    cert_path: str | Path,
    key_path: str | Path,
    cn: str = "SyncCore",
    days: int = 365,
) -> None:
    """Create a 2048-bit RSA key and a self-signed certificate."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                    x509.IPAddress(ipaddress.IPv4Address("0.0.0.0")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    Path(key_path).write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    Path(cert_path).write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    if platform.system() == "Windows":
        _restrict_windows_acl(Path(key_path))
    else:
        os.chmod(key_path, 0o600)
        os.chmod(cert_path, 0o644)


def ensure_certs(cert_path: str | Path, key_path: str | Path) -> bool:
    """Generate certs if they do not already exist."""
    cert_path = Path(cert_path)
    key_path = Path(key_path)
    if cert_path.is_file() and key_path.is_file():
        return False
    generate_self_signed_cert(cert_path, key_path)
    return True
