"""Tests for the SDK capability probe + the /capabilities endpoint.

These run with or without the ``aitp`` wheel installed — the probe must
report cleanly in both cases and never raise.
"""
from __future__ import annotations

import sys
import types

import pytest
from fastapi.testclient import TestClient

from aitp_playground import capabilities as caps
from aitp_playground.api.health import router as health_router


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    caps.get_capabilities.cache_clear()
    yield
    caps.get_capabilities.cache_clear()


def test_probe_shape_is_stable() -> None:
    report = caps.get_capabilities()
    assert set(report) == {"sdk_available", "version", "features"}
    assert isinstance(report["sdk_available"], bool)
    # Every known feature is reported, as a bool, regardless of SDK presence.
    assert set(report["features"]) == set(caps.ALL_FEATURES)
    assert all(isinstance(v, bool) for v in report["features"].values())


def test_probe_never_raises_without_sdk(monkeypatch) -> None:
    """If aitp is unimportable, the probe reports no SDK and no features."""
    monkeypatch.setitem(sys.modules, "aitp", None)  # forces ImportError
    caps.get_capabilities.cache_clear()
    report = caps.get_capabilities()
    assert report["sdk_available"] is False
    assert report["version"] is None
    assert report["features"] == {name: False for name in caps.ALL_FEATURES}
    assert caps.has_feature(caps.FEATURE_TCT_CACHE) is False
    assert caps.sdk_available() is False


def test_probe_detects_features_from_a_fake_sdk(monkeypatch) -> None:
    """A stub module exposing only some symbols yields the matching flags."""
    fake = types.ModuleType("aitp")
    fake.__version__ = "0.2.0"

    class _Agent:  # only renewal present
        def build_renewal_request(self):  # pragma: no cover - presence check
            ...

    fake.AitpAgent = _Agent
    fake.TctStore = object  # tct_cache present
    fake.JwksProvider = object  # oidc present
    # SessionBundleBuilder / SpkiPinVerifier / multihop intentionally absent.

    monkeypatch.setitem(sys.modules, "aitp", fake)
    caps.get_capabilities.cache_clear()
    report = caps.get_capabilities()

    assert report["sdk_available"] is True
    assert report["version"] == "0.2.0"
    f = report["features"]
    assert f[caps.FEATURE_TCT_CACHE] is True
    assert f[caps.FEATURE_OIDC] is True
    assert f[caps.FEATURE_TCT_RENEWAL] is True
    assert f[caps.FEATURE_SESSION_BUNDLE] is False
    assert f[caps.FEATURE_SPKI_PINNING] is False
    assert f[caps.FEATURE_MULTIHOP_DELEGATION] is False


def test_capabilities_endpoint_returns_probe() -> None:
    client = TestClient(_app_with_health())
    resp = client.get("/capabilities")
    assert resp.status_code == 200
    body = resp.json()
    assert "sdk_available" in body
    assert set(body["features"]) == set(caps.ALL_FEATURES)


def _app_with_health():
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(health_router)
    return app
