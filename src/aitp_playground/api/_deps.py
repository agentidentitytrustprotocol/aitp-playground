"""FastAPI dependency helpers — pull services from app.state, and authenticate.

Auth model (GHSA-v4gm-5727-qpq6):

* ``require_api_key`` guards every mutating route and every route that
  exposes run data. It is applied per route, not per router, so the static
  scenario listings can stay public; ``tests/unit/test_route_auth.py`` walks
  ``app.routes`` and fails if a route is neither guarded nor on the explicit
  public allowlist.
* ``enforce_run_limits`` (``POST /runs``) and ``enforce_spend_limits``
  (hosted invoke) apply the spend limits before anything is spawned: the
  token buckets, the run launch limits (:class:`~aitp_playground.limits.RunLimiter`:
  the per-IP window, plus the concurrency cap for runs only) and the global
  hourly cap.
* With ``AITP_PLAYGROUND_REQUIRE_API_KEY=true``, :class:`~aitp_playground.security.ApiKeyMiddleware`
  additionally gates every path (public listings, docs, unknown paths) except
  ``/healthz``, the HMAC webhook and loopback ``/internal/*``.
* ``require_telemetry_token`` guards ``/internal/telemetry``, which the
  playground's own agent subprocesses call with a per-process token from
  their bootstrap file — they never hold the API key.
"""
from __future__ import annotations

import hmac
from typing import Iterator

from fastapi import Depends, HTTPException, Request

from ..config import Settings, get_settings
from ..cp_client.client import CpClient
from ..hosting.supervisor import AgentSupervisor
from ..limits import Reservation, RunLimiter, SpendLimits
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
    """The caller's network address, for logs and, absent a trusted subject
    IP, the per-IP token bucket.

    Railway's edge proxy appends the connecting address to
    ``X-Forwarded-For``; the right-most entry is the one that proxy wrote, so
    a client cannot pick its own bucket by sending the header itself. Off
    Railway (no header) the socket peer is used. Normalised to one IPv4
    address or one IPv6 /64.

    This is NOT the subject of per-IP run limits or LLM quotas: that is
    ``resolve_client(request).subject_ip``, which only a keyed caller can set.
    When a subject IP is present the per-IP token bucket keys on it instead
    (every console visitor would otherwise share the console's egress bucket).
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


def get_run_limiter(request: Request) -> RunLimiter:
    limiter = getattr(request.app.state, "run_limiter", None)
    if limiter is None:
        limiter = RunLimiter.from_settings(_settings(request))
        request.app.state.run_limiter = limiter
    return limiter


def _admit(
    request: Request, key_id: str, limits: SpendLimits, limiter: RunLimiter, *, slot: bool,
) -> Reservation:
    """Every spend limit, cheapest refusal first, nothing spent on a refusal
    that a later check would have made anyway.

    1. Token buckets. Per IP: the trusted subject IP when the caller sent
       one, else the proxy-appended client IP. Per key: skipped when a
       trusted subject IP was sent, because that key (the console) fronts
       many visitors and the per-IP limits take over.
    2. The run limiter: concurrency (``slot``), then the per-IP launch
       window. The window only applies to a trusted subject IP; keyless and
       header-less traffic all arrives from one edge-proxy address.
    3. The global hourly cap, last, so a 429 above never burns it. A refusal
       here hands back the slot and the window entry taken in step 2.
    """
    client = resolve_client(request)
    ip_key = client.subject_ip if client.trusted_header else client_ip(request)
    if not limits.per_ip.allow(f"ip:{ip_key}") or (
        not client.trusted_header and not limits.per_key.allow(f"key:{key_id}")
    ):
        raise HTTPException(
            status_code=429, detail="rate limit exceeded", headers={"Retry-After": "60"},
        )
    reservation = limiter.reserve(client.subject_ip, slot=slot)
    if not limits.hourly.try_acquire():
        reservation.cancel()
        raise HTTPException(
            status_code=429,
            detail="hourly run limit reached for this service",
            headers={"Retry-After": "300"},
        )
    return reservation


def enforce_spend_limits(
    request: Request,
    key_id: str = Depends(require_api_key),
    limits: SpendLimits = Depends(get_spend_limits),
    limiter: RunLimiter = Depends(get_run_limiter),
) -> None:
    """Spend limits for hosted-agent invoke: buckets, the per-IP window and
    the hourly cap. An invoke is not a run, so it takes no concurrency slot."""
    _admit(request, key_id, limits, limiter, slot=False)


def enforce_run_limits(
    request: Request,
    key_id: str = Depends(require_api_key),
    limits: SpendLimits = Depends(get_spend_limits),
    limiter: RunLimiter = Depends(get_run_limiter),
) -> Iterator[Reservation]:
    """Spend limits for ``POST /runs``, holding a concurrency slot.

    The route hands the reservation to the run's background task (sets
    ``handed_off``), which releases the slot when the run ends. If the request
    fails before that (a 422 body, an error creating the record) the slot and
    the window entry are handed back here.
    """
    reservation = _admit(request, key_id, limits, limiter, slot=True)
    try:
        yield reservation
    finally:
        if not reservation.handed_off:
            reservation.cancel()


def require_telemetry_token(request: Request) -> None:
    """Agents post telemetry with the per-process token from their bootstrap."""
    expected: str | None = getattr(request.app.state, "telemetry_token", None)
    if not expected:
        raise HTTPException(status_code=503, detail="telemetry not configured")
    presented = _bearer(request)
    if presented is None or not hmac.compare_digest(presented, expected.encode("utf-8")):
        raise _unauthorized()
