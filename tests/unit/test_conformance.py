"""Tests for the conformance catalog + readiness report (no aitp wheel needed)."""
from __future__ import annotations

import json
from pathlib import Path

from aitp_playground import conformance as conf


def _write(dir_: Path, name: str, obj: dict) -> None:
    (dir_ / name).write_text(json.dumps(obj))


def _caps(*, multihop: bool = False, bundle: bool = False) -> dict:
    return {
        "sdk_available": True,
        "version": "0.2.0",
        "features": {
            conf.FEATURE_MULTIHOP_DELEGATION: multihop,
            conf.FEATURE_SESSION_BUNDLE: bundle,
        },
    }


def _core_fixture(fid: str = "del-001") -> dict:
    return {
        "id": fid,
        "rfc": "RFC-AITP-0006",
        "status": "core",
        "required_for_v0_1": True,
        "feature": None,
        "input": {"operation": "verify_delegation_token"},
        "expected": {"outcome": "success"},
    }


def _draft_fixture(fid: str, feature: str) -> dict:
    return {
        "id": fid,
        "rfc": "RFC-AITP-0011",
        "status": "draft",
        "required_for_v0_1": False,
        "feature": feature,
        "input": {"operation": "verify_delegation_token"},
        "expected": {"outcome": "success"},
    }


def test_load_and_catalog(tmp_path: Path) -> None:
    _write(tmp_path, "del-001.json", _core_fixture())
    _write(tmp_path, "mh-001.json", _draft_fixture("mh-001", "experimental-multihop-delegation"))
    report = conf.build_report(tmp_path, capabilities=_caps())
    assert report["total"] == 2
    assert report["required_for_v0_1"] == 1
    assert report["by_tier"] == {"core": 1, "draft": 1}
    assert report["by_rfc"] == {"RFC-AITP-0006": 1, "RFC-AITP-0011": 1}
    assert report["valid"] is True


def test_readiness_tracks_installed_features(tmp_path: Path) -> None:
    _write(tmp_path, "core.json", _core_fixture())
    _write(tmp_path, "mh.json", _draft_fixture("mh-1", "experimental-multihop-delegation"))
    _write(tmp_path, "bundle.json", _draft_fixture("b-1", "experimental-session-bundle"))

    # Wheel has multihop but not bundle.
    report = conf.build_report(tmp_path, capabilities=_caps(multihop=True, bundle=False))
    assert report["by_readiness"] == {"available": 1, "core": 1, "skipped": 1}


def test_unknown_feature_is_flagged(tmp_path: Path) -> None:
    _write(tmp_path, "x.json", _draft_fixture("x-1", "experimental-something-new"))
    report = conf.build_report(tmp_path, capabilities=_caps())
    assert report["by_readiness"].get("unknown-feature") == 1


def test_metadata_violation_fails_report(tmp_path: Path) -> None:
    # Non-core fixture wrongly marked required_for_v0_1 + bad tier.
    _write(tmp_path, "bad.json", {
        "id": "bad-1",
        "rfc": "RFC-AITP-0011",
        "status": "experimental",  # not a valid tier
        "required_for_v0_1": True,  # illegal for non-core
        "feature": None,
    })
    report = conf.build_report(tmp_path, capabilities=_caps())
    assert report["valid"] is False
    assert any("status" in e for e in report["metadata_errors"])
    assert any("required_for_v0_1" in e for e in report["metadata_errors"])


def test_missing_required_field_flagged(tmp_path: Path) -> None:
    _write(tmp_path, "nomf.json", {"id": "n-1", "rfc": "RFC-AITP-0001", "status": "core"})
    report = conf.build_report(tmp_path, capabilities=_caps())
    assert report["valid"] is False
    assert any("required_for_v0_1" in e or "feature" in e for e in report["metadata_errors"])


def test_default_fixtures_dir_locates_or_none() -> None:
    # Either returns a real dir (sibling checkout present) or None — never raises.
    result = conf.default_fixtures_dir()
    assert result is None or result.is_dir()
