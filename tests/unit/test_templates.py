"""Tests for the ScenarioTemplate variant model."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from aitp_playground.config import Settings, get_settings
from aitp_playground.errors import ScenarioNotFoundError
from aitp_playground.registry.models import (
    ScenarioTemplate,
    ScenarioTemplateMeta,
    ScenarioTemplateSpec,
)
from aitp_playground.registry.service import RegistryService
from aitp_playground.registry.templates import apply_template


REF = "intra-org/research-and-write@1.0.0"


def _svc() -> RegistryService:
    return RegistryService(Settings())


# ── loader/discovery ────────────────────────────────────────────────────────


def test_loader_discovers_built_in_templates() -> None:
    svc = _svc()
    names = {t.metadata.name for t in svc.list_templates(REF)}
    assert {"trust-strict", "revoking"}.issubset(names)


def test_list_templates_for_scenario_without_templates_is_empty() -> None:
    svc = _svc()
    # trust-gate currently ships no templates/ directory.
    assert svc.list_templates("intra-org/trust-gate@1.0.0") == []


def test_list_templates_for_unknown_scenario_raises() -> None:
    svc = _svc()
    with pytest.raises(ScenarioNotFoundError):
        svc.list_templates("nope/missing@0.0.0")


def test_get_template_returns_typed_model() -> None:
    svc = _svc()
    tpl = svc.get_template(REF, "trust-strict")
    assert isinstance(tpl, ScenarioTemplate)
    assert tpl.spec.trust == {"eager": False}
    assert tpl.spec.workflow is not None
    assert {s.id for s in tpl.spec.workflow.steps} == {
        "probe_no_tct", "trust", "research", "write",
    }


def test_get_template_unknown_name_raises() -> None:
    svc = _svc()
    with pytest.raises(ScenarioNotFoundError):
        svc.get_template(REF, "nope")


# ── resolver/merge ──────────────────────────────────────────────────────────


def test_resolved_scenario_overrides_workflow_and_trust() -> None:
    svc = _svc()
    base = svc.get_scenario(REF)
    merged = svc.get_scenario_resolved(REF, template="trust-strict")
    # Trust got patched (eager flipped) but boundary fell through.
    assert merged.spec.trust.eager is False
    assert merged.spec.trust.boundary == base.spec.trust.boundary
    # Workflow steps replaced.
    assert [s.id for s in merged.spec.workflow.steps] == [
        "probe_no_tct", "trust", "research", "write",
    ]
    # Agents not touched.
    assert {a.id for a in merged.spec.agents} == {a.id for a in base.spec.agents}
    # Name marker is appended exactly once.
    assert merged.metadata.name.endswith("(template: trust-strict)")


def test_resolved_scenario_without_template_is_base() -> None:
    svc = _svc()
    base = svc.get_scenario(REF)
    same = svc.get_scenario_resolved(REF, template=None)
    assert same.metadata.name == base.metadata.name
    assert [s.id for s in same.spec.workflow.steps] == [
        s.id for s in base.spec.workflow.steps
    ]


def test_apply_template_is_idempotent_on_name() -> None:
    svc = _svc()
    base = svc.get_scenario(REF)
    tpl = svc.get_template(REF, "trust-strict")
    once = apply_template(base, tpl)
    twice = apply_template(once, tpl)
    # The "(template: trust-strict)" suffix is appended at most once.
    assert once.metadata.name == twice.metadata.name


def test_template_with_no_overrides_leaves_base_unchanged() -> None:
    base = _svc().get_scenario(REF)
    tpl = ScenarioTemplate(
        apiVersion="aitp.dev/v1",
        kind="ScenarioTemplate",
        metadata=ScenarioTemplateMeta(name="noop", summary=None),
        spec=ScenarioTemplateSpec(),
    )
    merged = apply_template(base, tpl)
    assert [s.id for s in merged.spec.workflow.steps] == [
        s.id for s in base.spec.workflow.steps
    ]
    assert merged.spec.trust.model_dump() == base.spec.trust.model_dump()
    assert merged.metadata.name.endswith("(template: noop)")


# ── loader robustness ──────────────────────────────────────────────────────


def test_loader_skips_files_without_kind_scenario_template(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """A file under templates/ that isn't kind:ScenarioTemplate is skipped,
    not fatal — this keeps stray notes from breaking a registry load."""
    # Build a minimal scenario tree with one valid template + one stray file.
    root = tmp_path
    pack = root / "demo-pack"
    sv_dir = pack / "sc" / "1.0.0"
    sv_dir.mkdir(parents=True)
    (pack / "pack.yaml").write_text(textwrap.dedent("""
        apiVersion: aitp.dev/v1
        kind: ScenarioPack
        metadata:
          slug: demo-pack
          name: Demo Pack
    """).strip())
    (sv_dir / "scenario.yaml").write_text(textwrap.dedent("""
        apiVersion: aitp.dev/v1
        kind: ScenarioVersion
        metadata:
          pack: demo-pack
          scenario: sc
          version: 1.0.0
          name: Demo
        spec:
          agents:
            - id: only
              ref: _shared/agents/researcher
          trust:
            boundary: intra_org
            discovery: static
          workflow:
            steps: []
    """).strip())
    tpl_dir = sv_dir / "templates"
    tpl_dir.mkdir()
    # Stray file (no kind: ScenarioTemplate) — must be skipped, not raised.
    (tpl_dir / "stray.yaml").write_text("topic: foo\nstyle: bar\n")
    (tpl_dir / "ok.yaml").write_text(textwrap.dedent("""
        apiVersion: aitp.dev/v1
        kind: ScenarioTemplate
        metadata:
          name: ok
        spec: {}
    """).strip())

    # Point the loader at our temp tree (need access to a real _shared agent
    # manifest too — copy the canonical one).
    real_root = Path(__file__).resolve().parents[2] / "scenarios"
    shared_target = root / "_shared" / "agents"
    shared_target.mkdir(parents=True)
    for agent_yaml in (real_root / "_shared" / "agents").glob("*.yaml"):
        (shared_target / agent_yaml.name).write_text(agent_yaml.read_text())

    monkeypatch.setenv("SCENARIOS_DIR", str(root))
    from aitp_playground import config
    monkeypatch.setattr(config, "_settings", None)

    svc = RegistryService(get_settings())
    names = {t.metadata.name for t in svc.list_templates("demo-pack/sc@1.0.0")}
    assert names == {"ok"}
