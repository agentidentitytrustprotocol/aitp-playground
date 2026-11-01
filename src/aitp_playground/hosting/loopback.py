"""How this service and its scenario agents reach each other on loopback.

A Manifest's ``handshake_endpoint`` must be ``https://`` (RFC-AITP-0003 §5 step
2), and scenario agents live on ``localhost`` inside one container. So they
serve TLS there, under one self-signed certificate minted per service process.

That certificate is the only root these clients trust, and only for loopback
URLs: ``mounts()`` routes ``localhost`` / ``127.0.0.1`` to it and leaves every
other host on the client's default trust. It is ``CA:FALSE``, so its key can
issue nothing, and it names only loopback. Nothing here touches
``SSL_CERT_FILE`` or any other process-wide setting.

``INSECURE_LOCALHOST_DANGEROUS`` (local/test config, refused on Railway; see
``config.py``) replaces all of this with plain ``http://localhost``.
"""
from __future__ import annotations

import atexit
import datetime as dt
import ipaddress
import os
import shutil
import ssl
import tempfile
from functools import cached_property
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from ..config import Settings


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def mint_loopback_cert(directory: Path) -> tuple[Path, Path]:
    """Write a self-signed loopback certificate and its key; return their paths."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "aitp-playground loopback")])
    now = dt.datetime.now(dt.timezone.utc)
    public_key = key.public_key()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(public_key), critical=False
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    certfile, keyfile = directory / "loopback.crt", directory / "loopback.key"
    _write_private(certfile, cert.public_bytes(serialization.Encoding.PEM))
    _write_private(
        keyfile,
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    return certfile, keyfile


class AgentLoopback:
    """Origin and client trust for agents listening on this machine's loopback."""

    def __init__(self, settings: Settings) -> None:
        self.insecure = settings.insecure_localhost_dangerous

    @property
    def scheme(self) -> str:
        return "http" if self.insecure else "https"

    def origin(self, port: int) -> str:
        return f"{self.scheme}://localhost:{port}"

    @cached_property
    def _cert_paths(self) -> tuple[Path, Path]:
        # mkdtemp makes the directory 0700. Lazy: only a process that launches
        # a scenario agent or dials one needs a key on disk.
        directory = Path(tempfile.mkdtemp(prefix="aitp-loopback-"))
        atexit.register(shutil.rmtree, directory, ignore_errors=True)
        return mint_loopback_cert(directory)

    @cached_property
    def _trust(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=str(self._cert_paths[0]))

    def agent_tls(self) -> dict[str, str] | None:
        """The bootstrap's ``loopback_tls`` block; ``None`` when serving plain HTTP."""
        if self.insecure:
            return None
        certfile, keyfile = self._cert_paths
        return {"certfile": str(certfile), "keyfile": str(keyfile)}

    def mounts(self) -> dict[str, httpx.AsyncHTTPTransport]:
        """``mounts=`` for a client that dials agents. Fresh transports each
        call, because a client closes its transports with it."""
        if self.insecure:
            return {}
        return {
            pattern: httpx.AsyncHTTPTransport(verify=self._trust)
            for pattern in ("all://localhost", "all://127.0.0.1")
        }
