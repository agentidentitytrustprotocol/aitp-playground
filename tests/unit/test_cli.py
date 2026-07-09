"""CLI command tests — validate path filtering."""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from aitp_playground import cli


ROOT = Path(__file__).resolve().parents[2]


def _run_cli(monkeypatch, capsys, *argv: str, scenarios_dir: Path | None = None) -> tuple[int, str, str]:
    monkeypatch.setenv("SCENARIOS_DIR", str(scenarios_dir or ROOT / "scenarios"))
    from aitp_playground import config
    monkeypatch.setattr(config, "_settings", None)
    rc = cli.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


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


def test_lint_flags_dangling_agent_refs(tmp_path, monkeypatch, capsys) -> None:
    """A freshly scaffolded pack references _shared agents that don't exist
    in an empty scenarios dir — lint must call that out instead of passing."""
    _scaffold_in(tmp_path, monkeypatch, "demo-pack/broken@1.0.0")
    capsys.readouterr()
    rc, out, _ = _run_cli(monkeypatch, capsys, "lint", scenarios_dir=tmp_path)
    assert rc == 1
    assert "finding(s):" in out
    assert "does not resolve" in out


def test_lint_flags_pack_with_no_scenarios(tmp_path, monkeypatch, capsys) -> None:
    pack_dir = tmp_path / "empty-pack"
    pack_dir.mkdir()
    (pack_dir / "pack.yaml").write_text(cli._PACK_TEMPLATE.format(pack="empty-pack"))
    rc, out, _ = _run_cli(monkeypatch, capsys, "lint", scenarios_dir=tmp_path)
    assert rc == 1
    assert "no scenarios under this pack" in out


# ── list ────────────────────────────────────────────────────────────────────


def test_list_prints_every_registered_scenario(monkeypatch, capsys) -> None:
    rc, out, _ = _run_cli(monkeypatch, capsys, "list")
    assert rc == 0
    assert "scenarios:" in out
    assert "intra-org/research-and-write@1.0.0" in out
    # Human-readable names ride along.
    assert "—" in out


# ── validate (error paths) ──────────────────────────────────────────────────


def test_validate_path_outside_scenarios_dir_fails(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("SCENARIOS_DIR", str(ROOT / "scenarios"))
    from aitp_playground import config
    monkeypatch.setattr(config, "_settings", None)
    rc = cli.main(["validate", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "not under scenarios_dir" in err


# ── dry-run ─────────────────────────────────────────────────────────────────


def test_dry_run_prints_agents_workflow_and_templates(monkeypatch, capsys) -> None:
    rc, out, _ = _run_cli(
        monkeypatch, capsys,
        "dry-run", "intra-org/research-and-write@1.0.0",
        "--inputs", '{"topic": "test"}',
    )
    assert rc == 0
    assert "Scenario:" in out
    assert "Trust:" in out and "boundary=intra_org" in out
    assert "researcher" in out and "writer" in out
    assert "Workflow:" in out
    # This scenario ships template variants; without --template they are listed.
    assert "Templates available" in out
    assert "trust-strict" in out


def test_dry_run_with_template_merges_variant(monkeypatch, capsys) -> None:
    rc, out, _ = _run_cli(
        monkeypatch, capsys,
        "dry-run", "intra-org/research-and-write@1.0.0",
        "--inputs", '{"topic": "test"}',
        "--template", "trust-strict",
    )
    assert rc == 0
    # The template flips eager off and swaps the step list in.
    assert "eager=False" in out
    assert "probe_no_tct" in out
    # With an explicit template the availability listing is suppressed.
    assert "Templates available" not in out


def test_dry_run_unknown_scenario_fails(monkeypatch, capsys) -> None:
    rc, _, err = _run_cli(monkeypatch, capsys, "dry-run", "nope/missing@9.9.9")
    assert rc == 1
    assert err.startswith("FAIL")


def test_dry_run_rejects_inputs_violating_schema(monkeypatch, capsys) -> None:
    rc, _, err = _run_cli(
        monkeypatch, capsys,
        "dry-run", "intra-org/research-and-write@1.0.0",
        "--inputs", '{"topic": 123}',
    )
    assert rc == 1
    assert "inputs INVALID" in err


# ── trace ───────────────────────────────────────────────────────────────────


def _patch_trace_client(monkeypatch, handler) -> None:
    """Route cmd_trace's httpx.Client through a MockTransport."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", fake_client)


def _playground_handler(final_status: str):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/runs":
            return httpx.Response(202, json={
                "run_id": "r-1", "status": "pending", "scenario_ref": "x",
            })
        if request.url.path == "/runs/r-1/narrate":
            return httpx.Response(200, text="[run] started\n[run] complete\n")
        if request.url.path == "/runs/r-1/status":
            return httpx.Response(200, json={"status": final_status})
        return httpx.Response(404)
    return handler


def test_trace_narrates_until_success(monkeypatch, capsys) -> None:
    _patch_trace_client(monkeypatch, _playground_handler("success"))
    rc, out, _ = _run_cli(
        monkeypatch, capsys,
        "trace", "intra-org/trust-gate@1.0.0",
        "--poll-secs", "0", "--timeout-secs", "5",
    )
    assert rc == 0
    assert "# run_id=r-1" in out
    assert "[run] started" in out
    assert "[run] complete" in out


def test_trace_failed_run_exits_nonzero(monkeypatch, capsys) -> None:
    _patch_trace_client(monkeypatch, _playground_handler("failed"))
    rc, _, _ = _run_cli(
        monkeypatch, capsys,
        "trace", "intra-org/trust-gate@1.0.0",
        "--poll-secs", "0", "--timeout-secs", "5",
    )
    assert rc == 1


def test_trace_reports_non_202_submit(monkeypatch, capsys) -> None:
    _patch_trace_client(
        monkeypatch,
        lambda request: httpx.Response(500, text="boom"),
    )
    rc, _, err = _run_cli(monkeypatch, capsys, "trace", "x/y@1.0.0")
    assert rc == 1
    assert "POST /runs returned 500" in err


def test_trace_reports_unreachable_playground(monkeypatch, capsys) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _patch_trace_client(monkeypatch, refuse)
    rc, _, err = _run_cli(monkeypatch, capsys, "trace", "x/y@1.0.0")
    assert rc == 1
    assert "not reachable" in err


def test_trace_times_out_with_rc2(monkeypatch, capsys) -> None:
    # Status never becomes terminal and the deadline is immediate.
    _patch_trace_client(monkeypatch, _playground_handler("running"))
    rc, _, err = _run_cli(
        monkeypatch, capsys,
        "trace", "x/y@1.0.0", "--poll-secs", "0", "--timeout-secs", "0",
    )
    assert rc == 2
    assert "timed out" in err


# ── conformance ─────────────────────────────────────────────────────────────


def _write_fixture(dir_: Path, name: str, obj: dict) -> None:
    (dir_ / name).write_text(json.dumps(obj))


_VALID_FIXTURE = {
    "id": "del-001",
    "rfc": "RFC-AITP-0006",
    "status": "core",
    "required_for_v0_1": True,
    "feature": None,
    "input": {"operation": "verify_delegation_token"},
    "expected": {"outcome": "success"},
}


def test_conformance_json_report_on_valid_corpus(tmp_path, monkeypatch, capsys) -> None:
    _write_fixture(tmp_path, "del-001.json", _VALID_FIXTURE)
    rc, out, _ = _run_cli(
        monkeypatch, capsys,
        "conformance", "--fixtures-dir", str(tmp_path), "--json",
    )
    assert rc == 0
    report = json.loads(out)
    assert report["valid"] is True
    assert report["total"] == 1
    assert report["by_rfc"] == {"RFC-AITP-0006": 1}


def test_conformance_human_report_flags_metadata_violations(tmp_path, monkeypatch, capsys) -> None:
    bad = {k: v for k, v in _VALID_FIXTURE.items() if k != "rfc"}
    _write_fixture(tmp_path, "bad-001.json", bad)
    rc, out, _ = _run_cli(
        monkeypatch, capsys,
        "conformance", "--fixtures-dir", str(tmp_path),
    )
    assert rc == 1
    assert "metadata violation" in out
    assert "missing required metadata 'rfc'" in out


def test_conformance_human_report_ok_line(tmp_path, monkeypatch, capsys) -> None:
    _write_fixture(tmp_path, "del-001.json", _VALID_FIXTURE)
    rc, out, _ = _run_cli(
        monkeypatch, capsys,
        "conformance", "--fixtures-dir", str(tmp_path),
    )
    assert rc == 0
    assert "Conformance corpus:" in out
    assert "ok  fixture metadata valid" in out


def test_conformance_missing_fixtures_dir_is_usage_error(monkeypatch, capsys) -> None:
    rc, _, err = _run_cli(
        monkeypatch, capsys,
        "conformance", "--fixtures-dir", "/does/not/exist",
    )
    assert rc == 2
    assert "fixtures dir not found" in err
