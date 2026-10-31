"""Runtime probe of the installed ``aitp`` SDK feature surface.

The ``aitp`` wheel is compiled from the sibling ``aitp-rs/bindings/aitp-py``
repo (it is *not* the unrelated PyPI package of the same name). From SDK
0.4.0 to 0.23 the full surface shipped by default. From 0.24 (aitp-rs
1.0-rc), TCT renewal, session bundles and multi-hop delegation are compiled
only into an ``experimental`` build (#117), so a default wheel lacks them.

This module reports what the *currently installed* wheel actually provides so
that:

* a scenario whose workflow needs a missing feature is refused before any
  agent is spawned (``ScenarioRunner.run`` preflight, ``run.failed`` with
  ``code="sdk_feature_unavailable"``) instead of crashing mid-run. A
  scenario's requirements are derived from its step types
  (``STEP_SDK_FEATURES``), never declared by hand, and
* operators can see the SDK's true surface at runtime (``GET /capabilities``).

Detection mirrors the ``hasattr`` convention already used in
``tests/unit/test_sdk_blocked_features.py``. The probe must never raise — the
SDK may be entirely absent (e.g. CI without the wheel), in which case every
feature reports ``False`` and ``sdk_available`` is ``False``.
"""
from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

if TYPE_CHECKING:
    from .registry.models import ScenarioVersion

# Stable feature keys, reported by GET /capabilities and the scenario
# listings and used by STEP_SDK_FEATURES (scenarios never declare them), so
# keep them stable across releases.
FEATURE_OIDC = "oidc"
FEATURE_SESSION_BUNDLE = "session_bundle"
FEATURE_SPKI_PINNING = "spki_pinning"
FEATURE_TCT_RENEWAL = "tct_renewal"
FEATURE_TCT_CACHE = "tct_cache"
FEATURE_MULTIHOP_DELEGATION = "multihop_delegation"

ALL_FEATURES = (
    FEATURE_OIDC,
    FEATURE_SESSION_BUNDLE,
    FEATURE_SPKI_PINNING,
    FEATURE_TCT_RENEWAL,
    FEATURE_TCT_CACHE,
    FEATURE_MULTIHOP_DELEGATION,
)


def _probe() -> dict[str, Any]:
    try:
        import aitp  # type: ignore
    except Exception:
        # ImportError when the wheel is absent; other exceptions if a broken
        # build is on the path. Either way: no SDK, no features.
        return {
            "sdk_available": False,
            "version": None,
            "features": {name: False for name in ALL_FEATURES},
        }

    # The compiled wheel doesn't set ``__version__``; fall back to the
    # installed distribution metadata so /capabilities reports a real version.
    version = getattr(aitp, "__version__", None)
    if version is None:
        try:
            import importlib.metadata as _md

            # Distribution name is "aitp-sdk" (import name "aitp"); fall back
            # to the bare "aitp" only if the renamed dist isn't found.
            try:
                version = _md.version("aitp-sdk")
            except _md.PackageNotFoundError:
                version = _md.version("aitp")
        except Exception:
            version = None

    agent = getattr(aitp, "AitpAgent", None)
    features = {
        FEATURE_OIDC: hasattr(aitp, "JwksProvider"),
        FEATURE_SESSION_BUNDLE: hasattr(aitp, "SessionBundleBuilder")
        and hasattr(aitp, "verify_session_bundle"),
        FEATURE_SPKI_PINNING: hasattr(aitp, "SpkiPinVerifier"),
        # Both halves: a wheel with only one would pass the probe and then
        # fail on the issuer (or holder) side. Mirrors agents/base/sdk_features.py.
        FEATURE_TCT_RENEWAL: agent is not None
        and hasattr(agent, "build_renewal_request")
        and hasattr(agent, "process_renewal_request"),
        FEATURE_TCT_CACHE: hasattr(aitp, "TctStore"),
        FEATURE_MULTIHOP_DELEGATION: hasattr(
            aitp, "verify_delegation_multihop"
        ),
    }
    return {
        "sdk_available": True,
        "version": version,
        "features": features,
    }


@lru_cache(maxsize=1)
def get_capabilities() -> dict[str, Any]:
    """Return the cached SDK capability report.

    The installed wheel does not change during a process lifetime, so the
    result is cached. Tests that monkeypatch the SDK can call
    ``get_capabilities.cache_clear()`` to force a re-probe.
    """
    return _probe()


def has_feature(name: str) -> bool:
    """True if the installed SDK exposes ``name`` (one of ``ALL_FEATURES``)."""
    return bool(get_capabilities()["features"].get(name, False))


def sdk_available() -> bool:
    """True if the real ``aitp`` SDK is importable."""
    return bool(get_capabilities()["sdk_available"])


# Workflow step types that call an optional SDK feature, and which one. A
# scenario's requirements are the union over its resolved steps, so a
# template that adds or drops a gated step changes them with no YAML to
# keep in sync. A step type not listed here needs only the stable surface.
STEP_SDK_FEATURES: Mapping[str, str] = {
    "renew_tct": FEATURE_TCT_RENEWAL,
    "export_session_bundle": FEATURE_SESSION_BUNDLE,
    "verify_session_bundle": FEATURE_SESSION_BUNDLE,
}

CapabilitiesProvider = Callable[[], dict[str, Any]]


def required_sdk_features(scenario: "ScenarioVersion") -> list[str]:
    """The optional SDK features ``scenario``'s workflow calls, sorted."""
    return sorted({
        STEP_SDK_FEATURES[step.type]
        for step in scenario.spec.workflow.steps
        if step.type in STEP_SDK_FEATURES
    })


def missing_sdk_features(
    scenario: "ScenarioVersion",
    capabilities: Optional[CapabilitiesProvider] = None,
) -> list[str]:
    """The required features the installed SDK lacks. With no SDK at all,
    every requirement is missing."""
    caps = (capabilities or get_capabilities)()
    features = caps["features"] if caps["sdk_available"] else {}
    return [f for f in required_sdk_features(scenario) if not features.get(f, False)]
