"""Agent-side check for optional ``aitp-sdk`` features (#117).

From aitp-sdk 0.24 (aitp-rs 1.0-rc) TCT renewal, session bundles and
multi-hop delegation are compiled only into an ``experimental`` build, so on
a default wheel the names below are simply missing attributes. Routes that
call them check first and refuse with a coded 501 instead of crashing into
an ``AttributeError`` 500.

The playground's own probe (``src/aitp_playground/capabilities.py``) answers
the same question for the engine preflight; agents can't import
``aitp_playground``, so this mirrors it, and a test keeps the two in step.

Names are resolved at call time, never cached, so a test that deletes an
attribute (or swaps ``sys.modules["aitp"]``) sees the change.
"""
from __future__ import annotations

import importlib
import importlib.metadata
from typing import Optional

from fastapi.responses import JSONResponse

TCT_RENEWAL = "tct_renewal"
SESSION_BUNDLE = "session_bundle"
MULTIHOP_DELEGATION = "multihop_delegation"

# feature -> (owner attribute on the ``aitp`` module, or None for the module
# itself; names that must all be present on that owner).
FEATURES: dict[str, tuple[Optional[str], tuple[str, ...]]] = {
    TCT_RENEWAL: ("AitpAgent", ("build_renewal_request", "process_renewal_request")),
    SESSION_BUNDLE: (None, ("SessionBundleBuilder", "verify_session_bundle")),
    MULTIHOP_DELEGATION: (None, ("verify_delegation_multihop",)),
}

UNAVAILABLE_CODE = "SDK_FEATURE_UNAVAILABLE"


def has(feature: str) -> bool:
    """True if the installed ``aitp`` exposes every name ``feature`` needs.

    Raises ``KeyError`` for a feature this module doesn't know, so a typo at
    a call site fails its tests instead of silently refusing.
    """
    owner_name, names = FEATURES[feature]
    try:
        module = importlib.import_module("aitp")
    except ImportError:
        return False
    owner = module if owner_name is None else getattr(module, owner_name, None)
    return owner is not None and all(hasattr(owner, name) for name in names)


def sdk_version() -> str:
    try:
        return importlib.metadata.version("aitp-sdk")
    except importlib.metadata.PackageNotFoundError:
        return "(unknown)"


def unavailable(feature: str) -> JSONResponse:
    """The 501 a route returns when ``has(feature)`` is False."""
    FEATURES[feature]  # same KeyError contract as has()
    return JSONResponse(
        status_code=501,
        content={
            "error": f"aitp-sdk {sdk_version()} lacks {feature}",
            "code": UNAVAILABLE_CODE,
            "feature": feature,
        },
    )
