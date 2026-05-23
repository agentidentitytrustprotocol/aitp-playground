"""API surface tests — exercises health + registry routes via TestClient."""
from __future__ import annotations

from fastapi.testclient import TestClient

from aitp_playground.main import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def test_healthz() -> None:
    r = _client().get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_list_packs() -> None:
    r = _client().get("/packs")
    assert r.status_code == 200
    slugs = {p["metadata"]["slug"] for p in r.json()["packs"]}
    assert {"intra-org", "cross-org", "cross-cloud"}.issubset(slugs)


def test_list_scenarios() -> None:
    r = _client().get("/scenarios")
    assert r.status_code == 200
    refs = {s["ref"] for s in r.json()["scenarios"]}
    assert "intra-org/research-and-write@1.0.0" in refs


def test_get_scenario_returns_spec() -> None:
    r = _client().get("/scenarios/intra-org/research-and-write@1.0.0")
    assert r.status_code == 200
    body = r.json()
    assert body["metadata"]["name"] == "Research and Write"
    assert body["spec"]["trust"]["boundary"] == "intra_org"


def test_unknown_scenario_returns_404() -> None:
    r = _client().get("/scenarios/nope/missing@0.0.0")
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "scenario_not_found"
