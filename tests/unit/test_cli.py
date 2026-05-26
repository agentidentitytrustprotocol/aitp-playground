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


# ── new (scaffold) ──────────────────────────────────────────────────────────


def _scaffold_in(tmp_path: Path, monkeypatch, ref: str) -> tuple[int, str]:
    """Run `cli new <ref>` against an empty scenarios dir under tmp_path."""
    monkeypatch.setenv("SCENARIOS_DIR", str(tmp_path))
    from aitp_playground import config
    monkeypatch.setattr(config, "_settings", None)
    return cli.main(["new", ref]), str(tmp_path)


def test_new_creates_pack_and_scenario_files(tmp_path, monkeypatch, capsys) -> None:
    rc, root = _scaffold_in(tmp_path, monkeypatch, "demo-pack/my-scenario@1.0.0")
    capsys.readouterr()
    assert rc == 0
    pack_file = tmp_path / "demo-pack" / "pack.yaml"
    scenario_file = tmp_path / "demo-pack" / "my-scenario" / "1.0.0" / "scenario.yaml"
    assert pack_file.exists() and "slug: demo-pack" in pack_file.read_text()
    assert scenario_file.exists()
    body = scenario_file.read_text()
    assert "pack: demo-pack" in body
    assert "scenario: my-scenario" in body
    assert "version: 1.0.0" in body
    # The scaffolded scenario must load cleanly via the registry.
    from aitp_playground import config
    monkeypatch.setattr(config, "_settings", None)
    from aitp_playground.registry.service import RegistryService
    from aitp_playground.config import get_settings
    reg = RegistryService(get_settings())
    sv = reg.get_scenario("demo-pack/my-scenario@1.0.0")
    assert sv.metadata.pack == "demo-pack"


def test_new_refuses_to_overwrite_existing(tmp_path, monkeypatch, capsys) -> None:
    _scaffold_in(tmp_path, monkeypatch, "demo-pack/sc@1.0.0")
    capsys.readouterr()
    rc, _ = _scaffold_in(tmp_path, monkeypatch, "demo-pack/sc@1.0.0")
    err = capsys.readouterr().err
    assert rc == 1
    assert "already exists" in err


def test_new_rejects_invalid_ref_shape(tmp_path, monkeypatch, capsys) -> None:
    rc, _ = _scaffold_in(tmp_path, monkeypatch, "not-a-ref")
    err = capsys.readouterr().err
    assert rc == 2
    assert "expected <pack>/<scenario>@<version>" in err


def test_new_rejects_non_kebab_slug(tmp_path, monkeypatch, capsys) -> None:
    rc, _ = _scaffold_in(tmp_path, monkeypatch, "demo-pack/MixedCase@1.0.0")
    err = capsys.readouterr().err
    assert rc == 2
    assert "kebab-case" in err


def test_new_rejects_non_semver(tmp_path, monkeypatch, capsys) -> None:
    rc, _ = _scaffold_in(tmp_path, monkeypatch, "demo-pack/sc@v1")
    err = capsys.readouterr().err
    assert rc == 2
    assert "semver" in err.lower() or "MAJOR.MINOR.PATCH" in err


# ── lint ────────────────────────────────────────────────────────────────────


def _run_lint(monkeypatch, capsys, *packs: str) -> tuple[int, str, str]:
    monkeypatch.setenv("SCENARIOS_DIR", str(ROOT / "scenarios"))
    from aitp_playground import config
    monkeypatch.setattr(config, "_settings", None)
    rc = cli.main(["lint", *packs])
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_lint_clean_on_built_in_packs(monkeypatch, capsys) -> None:
    """The packs that ship with the repo must be lint-clean — this is the
    canary that catches dangling agent refs or unknown capabilities."""
    rc, out, _ = _run_lint(monkeypatch, capsys)
    assert rc == 0, out
    assert "lint clean" in out


def test_lint_unknown_pack_returns_usage_error(monkeypatch, capsys) -> None:
    rc, _, err = _run_lint(monkeypatch, capsys, "no-such-pack")
    assert rc == 2
    assert "not found" in err
