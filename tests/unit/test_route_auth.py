"""Route-walk guard: no route is reachable without authentication unless it
is on the explicit public allowlist.

Applies per route (not per router) so the static scenario listings stay
public. A new route fails this test until it either takes
``require_api_key`` (directly, or through ``enforce_spend_limits``) or is
added to an allowlist below with a reason.
"""
from __future__ import annotations

from typing import Any, Callable, Iterator, NamedTuple

import pytest
from fastapi import routing as fastapi_routing
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from aitp_playground.api._deps import (
    enforce_spend_limits,
    require_api_key,
    require_telemetry_token,
)
from aitp_playground.config import Settings
from aitp_playground.main import create_app
from tests._auth import AUTH

#: Static content only — no run data, no mutation.
PUBLIC_GET = {
    "/healthz",
    "/capabilities",
    "/packs",
    "/scenarios",
    "/scenarios/{pack}/{scenario}@{version}",
    "/scenarios/{pack}/{scenario}@{version}/templates",
    "/scenarios/{pack}/{scenario}@{version}/templates/{name}",
    "/dashboard",
}

#: Routes authenticated by something other than the API key.
OTHER_AUTH = {
    # CP holds only the per-webhook HMAC secret; verified in the handler.
    ("POST", "/webhooks/cp/{run_id}"): None,
    # Agent subprocesses hold a per-process telemetry token, not the key.
    ("POST", "/internal/telemetry"): require_telemetry_token,
}

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def _calls(dependant) -> Iterator[Callable]:
    for dep in dependant.dependencies:
        if dep.call is not None:
            yield dep.call
        yield from _calls(dep)


class _Route(NamedTuple):
    path: str
    methods: set[str]
    dependant: Any


def _api_routes() -> list[_Route]:
    """Every APIRoute with its full (prefixed) path.

    FastAPI >= 0.140 keeps included routers as ``_IncludedRouter`` entries
    in ``app.routes``; ``iter_route_contexts`` flattens them. Older FastAPI
    lists the APIRoutes directly.
    """
    routes = create_app().routes
    out: list[_Route] = []
    if hasattr(fastapi_routing, "iter_route_contexts"):
        for ctx in fastapi_routing.iter_route_contexts(routes):
            if isinstance(ctx.route, APIRoute):
                out.append(_Route(ctx.path, set(ctx.methods or ()), ctx.route.dependant))
    else:
        for r in routes:
            if isinstance(r, APIRoute):
                out.append(_Route(r.path, set(r.methods), r.dependant))
    # A walk that finds nothing would pass vacuously.
    assert len(out) > 30, f"route walk found only {len(out)} routes"
    return out


def test_every_route_is_authenticated_or_explicitly_public() -> None:
    unguarded = []
    for route in _api_routes():
        calls = set(_calls(route.dependant))
        for method in route.methods:
            key = (method, route.path)
            if key in OTHER_AUTH:
                expected = OTHER_AUTH[key]
                assert expected is None or expected in calls, key
                continue
            if method in {"GET", "HEAD"} and route.path in PUBLIC_GET:
                continue
            if require_api_key not in calls:
                unguarded.append(key)
    assert not unguarded, f"routes without require_api_key: {sorted(unguarded)}"


def test_every_mutating_route_is_guarded() -> None:
    """Acceptance criterion 1, stated directly: no POST/PUT/PATCH/DELETE
    is public."""
    for route in _api_routes():
        for method in route.methods & MUTATING:
            assert route.path not in PUBLIC_GET
            if (method, route.path) not in OTHER_AUTH:
                assert require_api_key in set(_calls(route.dependant)), (method, route.path)


def test_spend_routes_carry_the_spend_limits() -> None:
    spend = {("POST", "/runs"), ("POST", "/hosted-agents/{hosted_id}/invoke")}
    for route in _api_routes():
        for method in route.methods:
            if (method, route.path) in spend:
                assert enforce_spend_limits in set(_calls(route.dependant))


def _concrete(path: str) -> str:
    return (
        path.replace("{pack}/{scenario}@{version}", "p/s@1.0.0")
        .replace("{", "").replace("}", "")
    )


@pytest.mark.parametrize("method,path", sorted({
    (m, r.path) for r in _api_routes() for m in r.methods
    if (m, r.path) not in OTHER_AUTH and not (m in {"GET", "HEAD"} and r.path in PUBLIC_GET)
}))
def test_guarded_route_without_key_is_401_and_with_wrong_key_is_401(method: str, path: str) -> None:
    client = TestClient(create_app())
    url = _concrete(path)
    assert client.request(method, url).status_code == 401
    wrong = {"Authorization": "Bearer not-the-key"}
    assert client.request(method, url, headers=wrong).status_code == 401
    assert client.request(method, url, headers={"Authorization": "Basic x"}).status_code == 401


def test_unset_key_fails_closed_with_503() -> None:
    client = TestClient(create_app(Settings(playground_api_key="")), headers=AUTH)
    r = client.post("/runs", json={"scenario_ref": "intra-org/trust-gate@1.0.0"})
    assert r.status_code == 503
    assert client.post("/runs/x/cancel").status_code == 503
    assert client.delete("/hosted-agents/x").status_code == 503
    assert client.get("/runs/x/events").status_code == 503
    # Static listings stay up.
    assert client.get("/scenarios").status_code == 200


def test_any_key_in_the_rotation_list_is_accepted() -> None:
    app = create_app(Settings(playground_api_key="old-key, new-key"))
    for key in ("old-key", "new-key"):
        r = TestClient(app).get("/runs", headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 200
    r = TestClient(app).get("/runs", headers={"Authorization": "Bearer old-key, new-key"})
    assert r.status_code == 401


def test_public_listings_need_no_key() -> None:
    client = TestClient(create_app())
    for path in ("/healthz", "/capabilities", "/packs", "/scenarios", "/dashboard"):
        assert client.get(path).status_code == 200, path
