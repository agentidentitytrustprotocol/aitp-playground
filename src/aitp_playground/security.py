"""Backend access gate, API-key matching and client-IP resolution.

No AITP protocol logic lives here; this is the playground's own HTTP edge.

Keys
----
A caller presents the playground key as ``x-api-key: <key>`` (canonical) or
``Authorization: Bearer <key>`` (compatibility: the console sent Bearer before
wave 3). ``PLAYGROUND_API_KEY`` may hold several comma-separated keys so a key
can be rotated without downtime. Matching is constant time over every
configured key; the caller is identified in logs and limits only by
``sha256(key)[:16]``, never by the key itself.

The gate
--------
:class:`ApiKeyMiddleware` is raw ASGI so it is fail-closed: FastAPI's
``/docs`` and ``/openapi.json``, SSE streams, unknown paths and any route added
later are covered without anyone remembering to attach a dependency.

``AITP_PLAYGROUND_REQUIRE_API_KEY`` stages enforcement:

- **false** (the default): behaviour is exactly as before. The middleware only
  resolves the caller (below) and lets every request through; the per-route
  ``require_api_key`` dependency still guards the routes it always guarded,
  and the static scenario listings stay public. A key that is presented but
  wrong is logged (fingerprint only) and treated as absent.
- **true**: a request without a valid key gets ``401`` before any route or
  limiter runs, with these exceptions:

  - ``/healthz``: the platform health check;
  - ``POST /webhooks/cp/{run_id}``: each delivery carries its own HMAC
    signature, verified in the handler;
  - ``/internal/*`` from loopback with no ``X-Forwarded-For`` or
    ``Forwarded``: agent
    subprocesses report through it with their own per-process token and never
    hold the playground key. A request that came through a proxy is never
    treated as loopback, even if the proxy rewrote the socket peer;
  - ``OPTIONS``: preflight requests never carry credentials.

  ``Settings`` refuses to load when this is on and no key is configured.

Client resolution
-----------------
The middleware stores a :class:`ClientResolution` in ``scope["state"]``:

- ``key_fp``: fingerprint of the valid key presented, else ``None``;
- ``subject_ip``: the caller's IP for per-IP limits and quotas, taken ONLY
  from ``X-AITP-Client-IP`` on a request that carries a valid key, and
  normalised (one IPv4 address, or one IPv6 /64). It is ``None`` otherwise:
  without a key the header is ignored so nobody can pick their own bucket, and
  without the header the socket peer is an edge proxy shared by every visitor,
  so per-IP limits would throttle everyone together;
- ``trusted_header``: ``subject_ip`` came from the trusted header.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)

API_KEY_HEADER = "x-api-key"
CLIENT_IP_HEADER = "x-aitp-client-ip"
WWW_AUTHENTICATE = 'ApiKey header="x-api-key"'
STATE_KEY = "aitp_client"

_MAX_IP_LEN = 64


# --- IP normalisation -------------------------------------------------------

def normalize_client_ip(raw: str) -> str:
    """One rate-limit identity per subscriber.

    Mirrors ``aitp-docs/mcp/src/ratelimit.ts`` ``normalizeClientIp``: an IPv4
    address as is; an IPv4-mapped IPv6 address as its dotted quad; any other
    IPv6 address as its /64, written as the first four groups (uncompressed,
    lower-case hex) plus ``::/64``. A zone (``%eth0``) is dropped. Anything that
    is not an IP address is returned trimmed but otherwise unchanged.
    """
    addr = raw.strip()
    try:
        return str(ipaddress.IPv4Address(addr))
    except ValueError:
        pass
    unzoned = addr.split("%", 1)[0]
    try:
        ip6 = ipaddress.IPv6Address(unzoned)
    except ValueError:
        return addr
    if ip6.ipv4_mapped is not None:
        return str(ip6.ipv4_mapped)
    groups = [int.from_bytes(ip6.packed[i:i + 2], "big") for i in range(0, 8, 2)]
    return ":".join(f"{g:x}" for g in groups) + "::/64"


def parse_client_ip(raw: str | None) -> str | None:
    """The normalised IP in a client-IP header value, or ``None``.

    Takes the first comma-separated value. Anything that is not a bare IP
    address (empty, a hostname, ``host:port``, ``[v6]``) yields ``None``.
    """
    if raw is None:
        return None
    first = raw.split(",", 1)[0].strip()
    if not first or len(first) > _MAX_IP_LEN:
        return None
    if not _is_ip(first):
        return None
    return normalize_client_ip(first)


def _is_ip(value: str) -> bool:
    """A bare IP address; a ``%zone`` suffix is accepted on IPv6 only."""
    unzoned, zoned, _ = value.partition("%")
    try:
        if zoned:
            ipaddress.IPv6Address(unzoned)
        else:
            ipaddress.ip_address(unzoned)
    except ValueError:
        return False
    return True


# --- keys -------------------------------------------------------------------

def configured_keys(raw: str) -> list[bytes]:
    """The keys in ``PLAYGROUND_API_KEY``: comma-separated, blanks dropped."""
    return [k.strip().encode() for k in raw.split(",") if k.strip()]


def fingerprint(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


def match_key(presented: Iterable[bytes | None], keys: list[bytes]) -> str | None:
    """Fingerprint of the first presented value equal to a configured key.

    Every presented value is compared against every key with
    ``hmac.compare_digest`` and no short-circuit, so timing reveals neither
    which key matched nor how many are configured.
    """
    matched: bytes | None = None
    for value in presented:
        if not value:
            continue
        for key in keys:
            if hmac.compare_digest(value, key) and matched is None:
                matched = value
    return fingerprint(matched) if matched is not None else None


def _headers(scope: Scope) -> dict[bytes, bytes]:
    """Header map, lower-case names; a repeated header keeps its first value."""
    out: dict[bytes, bytes] = {}
    for name, value in scope.get("headers") or ():
        out.setdefault(name.lower(), value)
    return out


def _has_header(scope: Scope, name: bytes) -> bool:
    return any(k.lower() == name for k, _ in scope.get("headers") or ())


def presented_keys(headers: dict[bytes, bytes]) -> list[bytes | None]:
    """The ``x-api-key`` value and the Bearer token, either possibly absent."""
    api_key = headers.get(API_KEY_HEADER.encode())
    bearer: bytes | None = None
    auth = headers.get(b"authorization")
    if auth is not None:
        scheme, _, token = auth.partition(b" ")
        if scheme.lower() == b"bearer" and token.strip():
            bearer = token.strip()
    return [api_key.strip() if api_key is not None else None, bearer]


# --- client resolution ------------------------------------------------------

@dataclass(frozen=True)
class ClientResolution:
    key_fp: str | None
    subject_ip: str | None
    trusted_header: bool
    key_presented: bool


def resolve_scope(scope: Scope, api_keys: str) -> ClientResolution:
    headers = _headers(scope)
    presented = presented_keys(headers)
    key_fp = match_key(presented, configured_keys(api_keys))
    subject_ip: str | None = None
    if key_fp is not None:
        raw = headers.get(CLIENT_IP_HEADER.encode())
        subject_ip = parse_client_ip(raw.decode("latin-1")) if raw is not None else None
    return ClientResolution(
        key_fp=key_fp,
        subject_ip=subject_ip,
        trusted_header=subject_ip is not None,
        key_presented=any(v for v in presented),
    )


def resolve_client(request: Any) -> ClientResolution:
    """The resolution the middleware stored, or a fresh one (no middleware)."""
    cached = getattr(request.state, STATE_KEY, None)
    if isinstance(cached, ClientResolution):
        return cached
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        from .config import get_settings  # noqa: PLC0415 - avoid an import cycle

        settings = get_settings()
    return resolve_scope(request.scope, settings.playground_api_key)


def is_direct_loopback(scope: Scope) -> bool:
    """The TCP peer is loopback and the request did not come through a proxy.

    Reads the raw ``X-Forwarded-For`` from the scope: uvicorn's proxy-headers
    support rewrites ``scope["client"]`` from it when the peer is 127.0.0.1,
    but leaves the header in place, so its presence is the tell.
    """
    client = scope.get("client")
    if not client:
        return False
    try:
        peer = ipaddress.ip_address(str(client[0]).split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped is not None:
        peer = peer.ipv4_mapped
    if not peer.is_loopback:
        return False
    return not _has_header(scope, b"x-forwarded-for") and not _has_header(scope, b"forwarded")


def is_loopback_url(url: str) -> bool:
    """The URL's host is ``localhost`` or a loopback IP address."""
    host = (urlsplit(url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# --- the gate ---------------------------------------------------------------

def _exempt(scope: Scope) -> bool:
    path: str = scope.get("path", "")
    method: str = scope.get("method", "")
    if scope["type"] == "http" and method == "OPTIONS":
        return True
    if path == "/healthz":
        return True
    if method == "POST" and path.startswith("/webhooks/cp/"):
        rest = path[len("/webhooks/cp/"):]
        if rest and "/" not in rest:
            return True
    if path.startswith("/internal/") and is_direct_loopback(scope):
        return True
    return False


class ApiKeyMiddleware:
    def __init__(self, app: ASGIApp, *, settings: Any) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        resolution = resolve_scope(scope, self.settings.playground_api_key)
        state: dict[str, Any] = scope.setdefault("state", {})
        state[STATE_KEY] = resolution

        if resolution.key_fp is not None or _exempt(scope):
            await self.app(scope, receive, send)
            return

        if resolution.key_presented:
            log.warning(
                "rejected API key on %s %s", scope.get("method", "WS"), scope.get("path", "")
            )
        if not self.settings.require_api_key:
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps(
            {"code": "unauthorized", "detail": "missing or invalid API key (x-api-key)"}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", WWW_AUTHENTICATE.encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
