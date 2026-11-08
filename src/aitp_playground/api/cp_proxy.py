"""Key-gated, GET-only passthrough to the Control Plane: ``GET /cp-proxy/{path}``.

The console reads the CP through this route instead of calling the CP
anonymously. It always needs the playground API key, whatever
``AITP_PLAYGROUND_REQUIRE_API_KEY`` says: the route is new and has no public
behaviour to keep.

Only an exact allowlist of read routes is forwarded (:data:`ALLOWED`); admin
and export reads stay behind the console's own password. The path is checked
on its raw, still-encoded segments, so an encoded ``..`` or ``/`` can't step
outside the allowlist, and every segment is re-encoded before it goes
upstream. Nothing from the caller's credentials goes upstream: the request
carries only ``accept``, ``if-none-match`` and ``last-event-id``, plus the
playground's own CP key (``CP_READ_API_KEY``, else ``CP_API_KEY``).

``api/events/stream`` (SSE) is streamed, at most :data:`MAX_STREAMS` at a
time; every other response is buffered up to :data:`MAX_BODY_BYTES` within
one ``CP_PROXY_TIMEOUT_SECONDS`` deadline. Redirects are never followed.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any, Optional
from urllib.parse import quote, unquote

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from ..config import Settings
from ._deps import require_api_key

logger = logging.getLogger("aitp_playground.cp_proxy")

PREFIX = "/cp-proxy"
router = APIRouter(prefix=PREFIX, tags=["control-plane"])

#: A single path segment, any value.
_ANY = object()
#: Forwarded as a stream instead of buffered.
STREAMED = ("api", "events", "stream")

#: Exact CP read routes the console needs. ``_ANY`` matches one segment. No
#: CP route has a static sibling at a wildcard position (checked against the
#: CP's ``src/app/api`` tree), so ``{id}`` can't select an admin route.
ALLOWED: tuple[tuple[Any, ...], ...] = (
    ("api", "health"),
    ("api", "readyz"),
    ("api", "dashboard", "overview"),
    ("api", "dashboard", "agents"),
    ("api", "delegations"),
    ("api", "events", "history"),
    STREAMED,
    ("api", "pinned-keys"),
    ("api", "registry", "agents"),
    ("api", "registry", "agents", _ANY),
    ("api", "registry", "agents", _ANY, "manifest"),
    ("api", "sessions"),
    ("api", "sessions", _ANY),
    ("api", "sessions", _ANY, "replay"),
    ("api", "tcts"),
    ("api", "trust-anchors"),
    ("api", "trust-anchors", _ANY),
    (".well-known", "aitp-manifest"),
)

FORWARD_REQUEST_HEADERS = ("accept", "if-none-match", "last-event-id")
FORWARD_RESPONSE_HEADERS = (
    "content-type", "cache-control", "etag", "last-modified", "expires", "retry-after",
)
MAX_BODY_BYTES = 5 * 1024 * 1024
#: Open event streams at once. Each holds an upstream connection, and the
#: client pool is sized above this so buffered reads always get one.
MAX_STREAMS = 20
POOL_CONNECTIONS = 100
MAX_SEGMENT_CHARS = 512
#: RFC 3986 pchar minus "%" (re-added by quoting) and minus "/".
_SAFE = "-._~!$&'()*+,;=:@"


def _error(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse({"code": code, "detail": detail}, status_code=status)


def _raw_tail(request: Request) -> Optional[str]:
    """The still-encoded path after ``/cp-proxy/``, or None."""
    raw = request.scope.get("raw_path")
    path = raw.decode("latin-1") if isinstance(raw, bytes) else quote(request.url.path, safe="/")
    root = request.scope.get("root_path") or ""
    if root and path.startswith(root):
        path = path[len(root):]
    path = path.split("?", 1)[0]
    if not path.startswith(PREFIX + "/"):
        return None
    return path[len(PREFIX) + 1:]


def resolve_target(raw_tail: str) -> Optional[tuple[str, bool]]:
    """Validate the raw path tail against the allowlist.

    Returns ``(encoded_path, streamed)`` or None. A segment that is empty,
    ``.``/``..`` (encoded or not), or decodes to something holding ``/``,
    ``\\``, ``%`` or a control character is refused, as is anything not on
    :data:`ALLOWED`.
    """
    segments: list[str] = []
    for raw in raw_tail.split("/"):
        if not raw or len(raw) > MAX_SEGMENT_CHARS:
            return None
        seg = unquote(raw, errors="strict") if "%" in raw else raw
        if seg in (".", "..") or any(c in seg for c in "/\\%") or any(
            ord(c) < 0x20 or ord(c) == 0x7F for c in seg
        ):
            return None
        segments.append(seg)
    for pattern in ALLOWED:
        if len(pattern) == len(segments) and all(
            p is _ANY or p == s for p, s in zip(pattern, segments)
        ):
            encoded = "/".join(quote(s, safe=_SAFE) for s in segments)
            return encoded, tuple(segments) == STREAMED
    return None


class CpProxy:
    """Holds the shared upstream client; created lazily, closed at shutdown."""

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        #: Open event streams. Read and changed only on the event loop, with
        #: no await between check and change, so no lock is needed.
        self.open_streams = 0

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            t = self.settings.cp_proxy_timeout_seconds
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(t), follow_redirects=False, trust_env=False,
                limits=httpx.Limits(max_connections=POOL_CONNECTIONS),
                transport=self._transport,
            )
        return self._client

    def upstream_headers(self, request: Request) -> dict[str, str]:
        out = {name: request.headers[name] for name in FORWARD_REQUEST_HEADERS
               if name in request.headers}
        key = self.settings.cp_read_api_key or self.settings.cp_api_key
        if key:
            out["authorization"] = f"Bearer {key}"
        return out

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _response_headers(resp: httpx.Response) -> dict[str, str]:
    return {name: resp.headers[name] for name in FORWARD_RESPONSE_HEADERS if name in resp.headers}


@router.get("/{path:path}", dependencies=[Depends(require_api_key)])
async def cp_proxy(path: str, request: Request) -> Response:
    del path  # the decoded form; the raw path is what gets checked
    proxy: CpProxy = request.app.state.cp_proxy
    base = proxy.settings.cp_base_url.rstrip("/")
    tail = _raw_tail(request)
    try:
        target = resolve_target(tail) if tail is not None else None
    except UnicodeDecodeError:
        target = None
    if target is None:
        return _error(404, "not_found", "not an allowed control plane read")
    if not base:
        return _error(503, "cp_disabled", "no control plane configured")

    encoded, streamed = target
    url = f"{base}/{encoded}"
    query = request.scope.get("query_string") or b""
    if query:
        url = f"{url}?{query.decode('latin-1')}"

    slot = _StreamSlot(proxy) if streamed else None
    if slot is not None and not slot.take():
        return _error(503, "too_many_streams", "too many open control plane event streams")
    try:
        return await _forward(proxy, url, encoded, streamed, request, slot)
    except BaseException:
        if slot is not None:
            slot.release()
        raise


class _StreamSlot:
    """One of :data:`MAX_STREAMS`; released at most once."""

    def __init__(self, proxy: CpProxy):
        self._proxy = proxy
        self._held = False

    def take(self) -> bool:
        if self._proxy.open_streams >= MAX_STREAMS:
            return False
        self._proxy.open_streams += 1
        self._held = True
        return True

    def release(self) -> None:
        if self._held:
            self._held = False
            self._proxy.open_streams -= 1


async def _forward(
    proxy: CpProxy, url: str, encoded: str, streamed: bool, request: Request,
    slot: Optional[_StreamSlot],
) -> Response:
    """Send the upstream GET. A streamed 200 hands the stream slot to the
    response, which frees it when the stream ends; every other outcome
    frees it here."""
    client = proxy.client()
    timeout = proxy.settings.cp_proxy_timeout_seconds

    def release() -> None:
        if slot is not None:
            slot.release()

    try:
        upstream = client.build_request(
            "GET", url, headers=proxy.upstream_headers(request),
            timeout=httpx.Timeout(timeout, read=None) if streamed else httpx.Timeout(timeout),
        )
        resp = await client.send(upstream, stream=True)
    except httpx.HTTPError as exc:
        release()
        logger.warning("cp-proxy upstream error on %s: %s", encoded, type(exc).__name__)
        return _error(502, "cp_unreachable", "control plane unreachable")

    if streamed and resp.status_code == 200:
        async def close() -> None:
            # Backstop for a stream the generator never finished closing
            # (e.g. it never started). Both steps are idempotent.
            try:
                await resp.aclose()
            finally:
                release()

        return StreamingResponse(
            _relay(resp, release), status_code=200,
            headers={**_response_headers(resp), "x-accel-buffering": "no"},
            background=BackgroundTask(close),
        )

    try:
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return _error(502, "cp_response_too_large", "control plane response too large")
        try:
            # One deadline for the whole body, so an upstream that drips bytes
            # (or a non-200 event-stream reply, which has no read timeout)
            # can't hold the request open.
            async with asyncio.timeout(timeout):
                body = await _read_capped(resp)
        except TimeoutError:
            logger.warning("cp-proxy upstream read timed out on %s", encoded)
            return _error(502, "cp_unreachable", "control plane unreachable")
        if body is None:
            return _error(502, "cp_response_too_large", "control plane response too large")
    except httpx.HTTPError as exc:
        logger.warning("cp-proxy upstream read error on %s: %s", encoded, type(exc).__name__)
        return _error(502, "cp_unreachable", "control plane unreachable")
    finally:
        await resp.aclose()
        release()
    return Response(content=body, status_code=resp.status_code,
                    headers=_response_headers(resp))


async def _read_capped(resp: httpx.Response) -> Optional[bytes]:
    """The decoded body, or None past :data:`MAX_BODY_BYTES`. Decoded bytes:
    content-encoding is never forwarded, and the cap applies to what the
    caller would receive."""
    body = bytearray()
    async for chunk in resp.aiter_bytes():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


async def _relay(resp: httpx.Response, release: Callable[[], None]) -> AsyncIterator[bytes]:
    """Forward an event stream chunk by chunk. A client disconnect cancels
    this generator, and ``finally`` closes the upstream connection and frees
    the stream slot (a server may skip the response's background task)."""
    try:
        async for chunk in resp.aiter_bytes():
            yield chunk
    except httpx.HTTPError as exc:
        logger.info("cp-proxy stream ended: %s", type(exc).__name__)
    finally:
        try:
            await resp.aclose()
        finally:
            release()
