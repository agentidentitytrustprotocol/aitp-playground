"""An agent's view of loopback TLS, from its bootstrap.

The service mints the certificate (``aitp_playground/hosting/loopback.py``) and
names it in ``bootstrap["loopback_tls"]``. It is trusted only for loopback URLs
on the clients that take these mounts; every other host keeps default trust.
"""
from __future__ import annotations

import ssl
from typing import Any

import httpx


class LoopbackClients:
    def __init__(self, bootstrap: dict[str, Any]) -> None:
        tls = bootstrap.get("loopback_tls")
        self._trust = ssl.create_default_context(cafile=tls["certfile"]) if tls else None

    def origin(self, port: int) -> str:
        return f"{'https' if self._trust else 'http'}://localhost:{port}"

    def mounts(self) -> dict[str, httpx.AsyncHTTPTransport]:
        if self._trust is None:
            return {}
        return {
            pattern: httpx.AsyncHTTPTransport(verify=self._trust)
            for pattern in ("all://localhost", "all://127.0.0.1")
        }


def insecure_localhost(bootstrap: dict[str, Any]) -> bool:
    """The local/test opt-in, as the service decided it. Off unless stated."""
    return bool(bootstrap.get("aitp", {}).get("insecure_localhost_dangerous"))
