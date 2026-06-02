"""RFC conformance fixture catalog + wheel-readiness report.

The AITP specs repo ships a corpus of conformance fixtures
(``schemas/conformance/*.json``) — each a metadata block (id, rfc, tier,
feature gate) plus an ``input``/``expected`` pair describing one protocol
check. This module loads that corpus, validates the metadata contract every
fixture must carry, and cross-references each fixture's feature gate against
the *installed* ``aitp`` wheel's capabilities (via the Phase 0 probe) so an
operator can see, for the wheel they're actually running:

  * how many fixtures exist, grouped by RFC and conformance tier,
  * how many are required for v0.1, and
  * which feature-gated fixtures the current wheel could execute vs. skip.

It deliberately does NOT re-implement the protocol checks — executing a
fixture against the SDK is a separate, SDK-gated concern (see
``execute_delegation_fixture``). The default report is static and needs no
wheel, so it runs anywhere.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .capabilities import (
    FEATURE_MULTIHOP_DELEGATION,
    FEATURE_SESSION_BUNDLE,
    get_capabilities,
)

# Map a fixture's ``feature`` gate to a capability-probe key. A fixture whose
# feature is null is core (always runnable); a feature with no mapping here is
# reported as an unknown gate rather than silently assumed runnable.
FEATURE_TO_CAPABILITY = {
    "experimental-multihop-delegation": FEATURE_MULTIHOP_DELEGATION,
    "experimental-session-bundle": FEATURE_SESSION_BUNDLE,
}

# Candidate locations for the specs-repo fixture corpus, relative to this repo
# root. The playground does not vendor the fixtures; it reads them from the
# sibling specs checkout.
_DEFAULT_FIXTURE_CANDIDATES = (
    "../agentidentitytrustprotocol/schemas/conformance",
    "../../agentidentitytrustprotocol/schemas/conformance",
)

_REQUIRED_META = ("id", "rfc", "status", "required_for_v0_1", "feature")
_VALID_TIERS = {"core", "draft", "extension", "reserved"}


@dataclass(frozen=True)
class Fixture:
    id: str
    rfc: str
    status: str  # conformance tier: core | draft | extension | reserved
    required_for_v0_1: bool
    feature: Optional[str]
    dynamic: bool
    operation: Optional[str]
    path: Path


def default_fixtures_dir(repo_root: Optional[Path] = None) -> Optional[Path]:
    """Best-effort locate the specs-repo fixture corpus. Returns None if no
    candidate exists — callers should surface a clear error."""
    root = repo_root or Path(__file__).resolve().parents[2]
    for cand in _DEFAULT_FIXTURE_CANDIDATES:
        p = (root / cand).resolve()
        if p.is_dir():
            return p
    return None


def load_fixtures(fixtures_dir: Path) -> list[Fixture]:
    """Load every ``*.json`` fixture under ``fixtures_dir`` (non-recursive).

    Files that fail to parse as JSON objects are skipped with their metadata
    treated as missing — ``validate_metadata`` reports them. The fixture-shape
    ``input``/``expected`` content is not parsed here beyond ``operation``.
    """
    fixtures: list[Fixture] = []
    for path in sorted(fixtures_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        op = None
        inp = raw.get("input")
        if isinstance(inp, dict):
            op = inp.get("operation")
        fixtures.append(Fixture(
            id=str(raw.get("id") or path.stem),
            rfc=str(raw.get("rfc") or ""),
            status=str(raw.get("status") or ""),
            required_for_v0_1=bool(raw.get("required_for_v0_1")),
            feature=raw.get("feature") if isinstance(raw.get("feature"), str) else None,
            dynamic=bool(raw.get("dynamic")),
            operation=op if isinstance(op, str) else None,
            path=path,
        ))
    return fixtures


def validate_metadata(fx: Fixture, raw: dict[str, Any]) -> list[str]:
    """Return a list of metadata-contract violations for one fixture (empty if
    valid). Enforces the same required-fields + tier rules as the fixture
    schema, plus the cross-field rule that non-core fixtures are never
    required for v0.1."""
    errors: list[str] = []
    for key in _REQUIRED_META:
        if key not in raw:
            errors.append(f"{fx.path.name}: missing required metadata '{key}'")
    if fx.status and fx.status not in _VALID_TIERS:
        errors.append(
            f"{fx.path.name}: status '{fx.status}' not in {sorted(_VALID_TIERS)}"
        )
    if fx.rfc and not fx.rfc.startswith("RFC-AITP-"):
        errors.append(f"{fx.path.name}: rfc '{fx.rfc}' is not an RFC-AITP-#### id")
    if fx.status and fx.status != "core" and fx.required_for_v0_1:
        errors.append(
            f"{fx.path.name}: required_for_v0_1 must be false when status != core"
        )
    return errors


def readiness(fx: Fixture, capabilities: Optional[dict[str, Any]] = None) -> str:
    """Classify whether the installed wheel could execute ``fx``:

      * "core"            — no feature gate, runnable on any wheel
      * "available"       — feature-gated and the wheel exposes it
      * "skipped"         — feature-gated and the wheel lacks it
      * "unknown-feature" — gated by a feature we don't map to a capability
    """
    if fx.feature is None:
        return "core"
    caps = capabilities if capabilities is not None else get_capabilities()
    cap_key = FEATURE_TO_CAPABILITY.get(fx.feature)
    if cap_key is None:
        return "unknown-feature"
    return "available" if caps["features"].get(cap_key) else "skipped"


def build_report(
    fixtures_dir: Path, capabilities: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """Build the full catalog + readiness report for a fixtures directory."""
    caps = capabilities if capabilities is not None else get_capabilities()
    fixtures = load_fixtures(fixtures_dir)

    errors: list[str] = []
    by_rfc: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    by_readiness: dict[str, int] = {}
    required_v01 = 0

    for fx in fixtures:
        raw = json.loads(fx.path.read_text()) if fx.path.exists() else {}
        if not isinstance(raw, dict):
            raw = {}
        errors.extend(validate_metadata(fx, raw))
        by_rfc[fx.rfc or "?"] = by_rfc.get(fx.rfc or "?", 0) + 1
        by_tier[fx.status or "?"] = by_tier.get(fx.status or "?", 0) + 1
        if fx.required_for_v0_1:
            required_v01 += 1
        r = readiness(fx, caps)
        by_readiness[r] = by_readiness.get(r, 0) + 1

    return {
        "fixtures_dir": str(fixtures_dir),
        "sdk_available": caps["sdk_available"],
        "sdk_version": caps["version"],
        "total": len(fixtures),
        "required_for_v0_1": required_v01,
        "by_rfc": dict(sorted(by_rfc.items())),
        "by_tier": dict(sorted(by_tier.items())),
        "by_readiness": dict(sorted(by_readiness.items())),
        "metadata_errors": errors,
        "valid": not errors,
    }
