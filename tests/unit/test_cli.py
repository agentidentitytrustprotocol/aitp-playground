"""CLI command tests — validate path filtering."""
from __future__ import annotations

from pathlib import Path

import pytest

from aitp_playground import cli


ROOT = Path(__file__).resolve().parents[2]


def _run_validate(monkeypatch, capsys, path: str) -> tuple[int, str]:
    monkeypatch.setenv("SCENARIOS_DIR", str(ROOT / "scenarios"))
    # cli.cmd_validate calls _registry() which calls get_settings(); reset the
    # cached singleton so the SCENARIOS_DIR env var is re-read.
    from aitp_playground import config
    monkeypatch.setattr(config, "_settings", None)
    rc = cli.main(["validate", path])
    captured = capsys.readouterr()
    return rc, captured.out


def test_validate_no_path_lists_all(monkeypatch, capsys) -> None:
    rc, out = _run_validate(monkeypatch, capsys, str(ROOT / "scenarios"))
    assert rc == 0
    # All packs represented
    assert "intra-org/" in out
    assert "cross-org/" in out
    assert "cross-cloud/" in out
    # And agents are listed too
    assert "agent " in out


def test_validate_pack_filters_to_pack(monkeypatch, capsys) -> None:
    rc, out = _run_validate(monkeypatch, capsys, str(ROOT / "scenarios" / "intra-org"))
    assert rc == 0
    assert "intra-org/" in out
    assert "cross-org/" not in out
    assert "cross-cloud/" not in out
    # Sub-path filter should not list agents.
    assert "agent " not in out


def test_validate_scenario_dir_filters_to_one(monkeypatch, capsys) -> None:
    rc, out = _run_validate(
        monkeypatch, capsys,
        str(ROOT / "scenarios" / "intra-org" / "research-and-write"),
    )
    assert rc == 0
    assert "intra-org/research-and-write@" in out
    # No other scenarios in intra-org should be listed.
    assert "intra-org/trust-gate" not in out
    assert "intra-org/revocation-demo" not in out
