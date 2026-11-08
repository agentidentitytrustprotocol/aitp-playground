"""FastAPI dependency helpers — pull services from app.state, and authenticate.

Auth model (GHSA-v4gm-5727-qpq6):

* ``require_api_key`` guards every mutating route and every route that
  exposes run data. It is applied per route, not per router, so the static
  scenario listings can stay public; ``tests/unit/test_route_auth.py`` walks
  ``app.routes`` and fails if a route is neither guarded nor on the explicit
  public allowlist.
* ``enforce_spend_limits`` adds the rate limit and the global hourly cap on
  the two routes that spend LLM credit (run create, hosted invoke).
* With ``AITP_PLAYGROUND_REQUIRE_API_KEY=true``, :class:`~aitp_playground.security.ApiKeyMiddleware`
  additionally gates every path (public listings, docs, unknown paths) except
  ``/healthz``, the HMAC webhook and loopback ``/internal/*``.
* ``require_telemetry_token`` guards ``/internal/telemetry``, which the
  playground's own agent subprocesses call with a per-process token from
  their bootstrap file — they never hold the API key.
"""
from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request

from ..config import Settings, get_settings
from ..cp_client.client import CpClient
from ..hosting.supervisor import AgentSupervisor
from ..limits import SpendLimits
from ..registry.service import RegistryService
from ..runner.engine import ScenarioRunner
from ..runner.store import RunStore
from ..security import configured_keys, normalize_client_ip, resolve_client


def get_registry(request: Request) -> RegistryService:
    return request.app.state.registry


def get_runner(request: Request) -> ScenarioRunner:
    return request.app.state.runner


def get_run_store(request: Request) -> RunStore:
    return request.app.state.run_store


def get_supervisor(request: Request) -> AgentSupervisor:
    return request.app.state.supervisor


def get_cp_client(request: Request) -> CpClient:
    return request.app.state.cp


def get_hosted_manager(request: Request):
    return request.app.state.hosted


def _settings(request: Request) -> Settings:
    return getattr(request.app.state, "settings", None) or get_settings()


def _bearer(request: Request) -> bytes | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token.encode("utf-8")


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="missing or invalid API key",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_api_key(request: Request) -> str:
    """Check the presented key against ``PLAYGROUND_API_KEY``.

    The key may come as ``x-api-key: <key>`` or ``Authorization: Bearer
    <key>``; both go through the one matcher in :mod:`aitp_playground.security`
    (constant time, every configured key compared). Returns a short
    fingerprint of the matched key (never the key itself) so downstream limits
    can key on the caller. No key configured → 503: the service fails closed
    instead of treating a missing secret as open.
    """
    settings = _settings(request)
    if not configured_keys(settings.playground_api_key):
        raise HTTPException(status_code=503, detail="API key not configured on this service")
    key_fp = resolve_client(request).key_fp
    if key_fp is None:
        raise _unauthorized()
    return key_fp


def client_ip(request: Request) -> str:
    """The caller's network address, for logs and the per-IP token bucket.

    Railway's edge proxy appends the connecting address to
    ``X-Forwarded-For``; the right-most entry is the one that proxy wrote, so
    a client cannot pick its own bucket by sending the header itself. Off
    Railway (no header) the socket peer is used. Normalised to one IPv4
    address or one IPv6 /64.

    This is NOT the subject of per-IP run limits or LLM quotas: that is
    ``resolve_client(request).subject_ip``, which only a keyed caller can set.
    """
    xff = request.headers.get("x-forwarded-for", "")
    if xff.strip():
        return normalize_client_ip(xff.split(",")[-1])
    return normalize_client_ip(request.client.host) if request.client else "unknown"


def get_spend_limits(request: Request) -> SpendLimits:
    limits = getattr(request.app.state, "spend_limits", None)
    if limits is None:
        limits = SpendLimits(_settings(request))
        request.app.state.spend_limits = limits
    return limits


def enforce_spend_limits(
    request: Request,
    key_id: str = Depends(require_api_key),
    limits: SpendLimits = Depends(get_spend_limits),
) -> None:
    """Per-IP and per-key token buckets, then the global hourly cap — all
    checked before any agent is spawned or provider called."""
    if not limits.per_ip.allow(f"ip:{client_ip(request)}") or not limits.per_key.allow(f"key:{key_id}"):
        raise HTTPException(
            status_code=429, detail="rate limit exceeded", headers={"Retry-After": "60"},
        )
    if not limits.hourly.try_acquire():
        raise HTTPException(
            status_code=429,
            detail="hourly run limit reached for this service",
            headers={"Retry-After": "300"},
        )


def require_telemetry_token(request: Request) -> None:
    """Agents post telemetry with the per-process token from their bootstrap."""
    expected: str | None = getattr(request.app.state, "telemetry_token", None)
    if not expected:
        raise HTTPException(status_code=503, detail="telemetry not configured")
    presented = _bearer(request)
    if presented is None or not hmac.compare_digest(presented, expected.encode("utf-8")):
        raise _unauthorized()
