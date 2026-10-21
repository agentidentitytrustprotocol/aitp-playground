"""Bearer guard for an agent's `/admin/*` routes (issue #95).

`/admin` is the supervisor's control channel: it fills the handshake trust
store (`/admin/trust-peers`), rotates keys, revokes TCTs and drives every
scenario step. Agents bind `0.0.0.0`, so before this guard anyone who could
reach an agent's port could, for one, make it admit any AID as a pinned_key
peer — undoing the admission control aitp-sdk 0.14 added (DECISIONS.md D-22).

The supervisor generates a random token per agent at spawn and hands it over
in the bootstrap file (mode 0600, never argv or env). Every `/admin` request
must carry it in `X-Aitp-Admin-Token`. An agent whose bootstrap has no token
rejects every guarded request: there is no unauthenticated fallback.

`/admin/process-renewal` is exempt. The holder agent calls it on the issuer
(RFC-AITP-0013) and does not hold the issuer's admin token; the body is a
signed renewal request that `process_renewal_request` verifies against the
TCT the issuer itself minted, so the SDK — not this guard — authenticates it.

This is pure ASGI rather than a FastAPI dependency so it covers routes on
both the AitpServer router and the build_admin_router router, including any
added later, and `run_agent()` applies it to every agent in one place.
"""
from __future__ import annotations

import hmac
import json
from typing import Any, Awaitable, Callable, Optional

ADMIN_TOKEN_HEADER = "x-aitp-admin-token"

#: `/admin` paths authenticated by the protocol payload instead (see above).
EXEMPT_PATHS = frozenset({"/admin/process-renewal"})

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


def _guarded(path: str) -> bool:
    return (path == "/admin" or path.startswith("/admin/")) and path not in EXEMPT_PATHS


class AdminAuthMiddleware:
    def __init__(self, app: Any, *, token: Optional[str]) -> None:
        self.app = app
        self._token = token.encode() if token else None

    def _authorized(self, scope: Scope) -> bool:
        if self._token is None:
            return False
        for name, value in scope.get("headers") or ():
            if name == ADMIN_TOKEN_HEADER.encode():
                return hmac.compare_digest(value, self._token)
        return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _guarded(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        if self._authorized(scope):
            await self.app(scope, receive, send)
            return
        body = json.dumps({"detail": "admin token missing or invalid"}).encode()
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})
