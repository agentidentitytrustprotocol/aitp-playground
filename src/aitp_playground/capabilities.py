"""Runtime probe of the installed ``aitp`` SDK feature surface.

The ``aitp`` wheel is compiled from the sibling ``aitp-rs/bindings/aitp-py``
repo (it is *not* the unrelated PyPI package of the same name). Several of
its surfaces are gated behind Cargo ``experimental-*`` features, so a given
wheel may or may not expose renewal, session bundles, SPKI pinning, the TCT
verification cache, or multi-hop delegation verification.

This module reports what the *currently installed* wheel actually provides so
that:

* scenarios requiring a missing feature degrade with a clear
  "feature-not-available" status instead of crashing mid-run, and
* operators can see the SDK's true surface at runtime (``GET /capabilities``).

Detection mirrors the ``hasattr`` convention already used in
``tests/unit/test_sdk_blocked_features.py``. The probe must never raise — the
SDK may be entirely absent (e.g. CI without the wheel), in which case every
feature reports ``False`` and ``sdk_available`` is ``False``.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

# Stable feature keys. Scenarios reference these names when declaring a
# required SDK capability, so keep them stable across releases.
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

            version = _md.version("aitp")
        except Exception:
            version = None

    agent = getattr(aitp, "AitpAgent", None)
    features = {
        FEATURE_OIDC: hasattr(aitp, "JwksProvider"),
        FEATURE_SESSION_BUNDLE: hasattr(aitp, "SessionBundleBuilder"),
        FEATURE_SPKI_PINNING: hasattr(aitp, "SpkiPinVerifier"),
        FEATURE_TCT_RENEWAL: agent is not None
        and hasattr(agent, "build_renewal_request"),
        FEATURE_TCT_CACHE: hasattr(aitp, "TctStore"),
        FEATURE_MULTIHOP_DELEGATION: hasattr(
            aitp, "verify_delegation_experimental_multihop"
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
