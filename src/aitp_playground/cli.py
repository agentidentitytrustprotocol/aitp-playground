"""Scenario authoring CLI: validate, list, dry-run, new, lint."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable

import jsonschema

from .config import get_settings
from .errors import PlaygroundError
from .registry.service import RegistryService

_KEBAB = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _registry() -> RegistryService:
    return RegistryService(get_settings())


def cmd_list(_: argparse.Namespace) -> int:
    reg = _registry()
    scenarios = reg.list_scenarios()
    print(f"{len(scenarios)} scenarios:")
    for s in scenarios:
        ref = f"{s.metadata.pack}/{s.metadata.scenario}@{s.metadata.version}"
        print(f"  - {ref}  — {s.metadata.name}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    target = Path(args.path).resolve()
    reg = _registry()
    scenarios_root = reg.settings.scenarios_path
    try:
        scenarios = reg.list_scenarios()
        # When target points inside scenarios_dir, filter scenarios to that
        # prefix; pack-level paths match every scenario in the pack, and
        # scenario-level paths match the one scenario.
        filter_to_subset = target.is_dir() and target != scenarios_root
        if filter_to_subset:
            try:
                rel = target.relative_to(scenarios_root).parts
            except ValueError:
                print(f"FAIL  {target} not under scenarios_dir", file=sys.stderr)
                return 1
            scenarios = [
                s for s in scenarios
                if s.metadata.pack == rel[0]
                and (len(rel) < 2 or s.metadata.scenario == rel[1])
            ]
        for sv in scenarios:
            print(f"ok  {sv.metadata.pack}/{sv.metadata.scenario}@{sv.metadata.version}")
        if not filter_to_subset:
            for ref in reg.all_agent_refs():
                print(f"ok  agent {ref}")
    except PlaygroundError as exc:
        print(f"FAIL  {target}: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_dry_run(args: argparse.Namespace) -> int:
    reg = _registry()
    sv = reg.get_scenario(args.scenario_ref)
    inputs = json.loads(args.inputs) if args.inputs else {}
    schema = sv.spec.inputs.schema_
    if schema:
        try:
            jsonschema.validate(inputs, schema)
        except jsonschema.ValidationError as exc:
            print(f"inputs INVALID: {exc.message}", file=sys.stderr)
            return 1
    print(f"Scenario: {sv.metadata.name}")
    print(f"Trust:    boundary={sv.spec.trust.boundary} discovery={sv.spec.trust.discovery}")
    print("Agents:")
    for a in sv.spec.agents:
        m = reg.get_agent_manifest(a.ref)
        print(f"  - {a.id} ({m.metadata.framework}) offers={m.spec.aitp.offered_caps}")
    print("Workflow:")
    for s in sv.spec.workflow.steps:
        marker = f"{s.agent}.{s.capability}" if s.agent and s.capability else "descriptive"
        print(f"  - {s.id:10s} {marker}")
    return 0


def _parse_scenario_ref(ref: str) -> tuple[str, str, str]:
    """Split ``<pack>/<scenario>@<version>`` into its three pieces or raise."""
    if "@" not in ref or "/" not in ref:
        raise ValueError(
            f"expected <pack>/<scenario>@<version>, got {ref!r}"
        )
    head, version = ref.split("@", 1)
    pack, scenario = head.split("/", 1)
    if not _KEBAB.match(pack):
        raise ValueError(f"pack slug must be kebab-case: {pack!r}")
    if not _KEBAB.match(scenario):
        raise ValueError(f"scenario slug must be kebab-case: {scenario!r}")
    if not _SEMVER.match(version):
        raise ValueError(f"version must be MAJOR.MINOR.PATCH: {version!r}")
    return pack, scenario, version


_SCENARIO_TEMPLATE = """\
apiVersion: aitp.dev/v1
kind: ScenarioVersion
metadata:
  pack: {pack}
  scenario: {scenario}
  version: {version}
  name: TODO — human-readable scenario name
  summary: >
    TODO — describe what this scenario demonstrates. Keep it to a few
    sentences; this shows up in `aitp-playground list` and on the run
    detail.
  tags: [intra-org]

spec:
  inputs:
    schema:
      type: object
      properties:
        topic:
          type: string
          default: "TODO topic"
      required: [topic]

  agents:
    - id: researcher
      ref: _shared/agents/researcher
      port_offset: 0
    - id: writer
      ref: _shared/agents/writer
      port_offset: 1

  trust:
    boundary: intra_org
    discovery: static
    eager: true

  workflow:
    steps:
      - id: research
        agent: researcher
        capability: research.query
        input_template: "{{{{ inputs.topic }}}}"
        description: Researcher produces findings on the topic.

      - id: write
        agent: writer
        capability: write.content
        input_from: research
        description: Writer turns findings into a paragraph.
"""


_PACK_TEMPLATE = """\
apiVersion: aitp.dev/v1
kind: ScenarioPack
metadata:
  slug: {pack}
  name: TODO — human-readable pack name
  description: TODO — one-line description of what this pack contains.
  tags: [{pack}]
"""


def cmd_new(args: argparse.Namespace) -> int:
    try:
        pack, scenario, version = _parse_scenario_ref(args.scenario_ref)
    except ValueError as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        return 2

    reg = _registry()
    root = reg.settings.scenarios_path
    pack_dir = root / pack
    scenario_dir = pack_dir / scenario / version
    scenario_file = scenario_dir / "scenario.yaml"
    pack_file = pack_dir / "pack.yaml"

    if scenario_file.exists():
        print(
            f"FAIL  {scenario_file.relative_to(root)} already exists — "
            f"bump the version or delete the existing tree to re-scaffold",
            file=sys.stderr,
        )
        return 1

    created: list[Path] = []
    if not pack_file.exists():
        pack_dir.mkdir(parents=True, exist_ok=True)
        pack_file.write_text(_PACK_TEMPLATE.format(pack=pack))
        created.append(pack_file)

    scenario_dir.mkdir(parents=True, exist_ok=True)
    scenario_file.write_text(
        _SCENARIO_TEMPLATE.format(pack=pack, scenario=scenario, version=version)
    )
    created.append(scenario_file)

    for path in created:
        print(f"created {path.relative_to(root.parent)}")
    print()
    print(f"Next: edit {scenario_file.relative_to(root.parent)} and run:")
    print(f"  aitp-playground validate {pack_dir.relative_to(root.parent)}")
    print(f"  aitp-playground dry-run {pack}/{scenario}@{version}")
    return 0


def _lint_pack_dir(pack_dir: Path, reg: RegistryService) -> Iterable[str]:
    """Yield lint findings for every scenario under ``pack_dir``."""
    pack_slug = pack_dir.name
    if pack_slug.startswith("_"):
        # Shared fragments dir (_shared/) — skipped at the pack level.
        return
    if not _KEBAB.match(pack_slug):
        yield f"{pack_slug}: directory name is not kebab-case"

    # Pack file present + slug agreement.
    pack_file = pack_dir / "pack.yaml"
    if not pack_file.exists():
        yield f"{pack_slug}: missing pack.yaml"
        return

    scenarios = [
        s for s in reg.list_scenarios()
        if s.metadata.pack == pack_slug
    ]
    if not scenarios:
        yield f"{pack_slug}: no scenarios under this pack"
        return

    for sv in scenarios:
        ref = f"{sv.metadata.pack}/{sv.metadata.scenario}@{sv.metadata.version}"
        if not _KEBAB.match(sv.metadata.scenario):
            yield f"{ref}: scenario slug not kebab-case"
        if not _SEMVER.match(sv.metadata.version):
            yield f"{ref}: version not semver MAJOR.MINOR.PATCH"
        if not sv.metadata.summary:
            yield f"{ref}: summary is empty (operators rely on it in list views)"

        offered: dict[str, set[str]] = {}
        agent_ids = {a.id for a in sv.spec.agents}
        for agent in sv.spec.agents:
            try:
                manifest = reg.get_agent_manifest(agent.ref)
            except PlaygroundError as exc:
                yield f"{ref}: agent {agent.id} ref={agent.ref!r} does not resolve ({exc})"
                continue
            offered[agent.id] = set(manifest.spec.aitp.offered_caps)

        prior_step_ids: set[str] = set()
        for step in sv.spec.workflow.steps:
            stype = step.type or ("workflow" if (step.agent and step.capability) else "meta")
            # agent / target_agent / initiator / responder / delegator /
            # delegatee / via_peer must all resolve to agents in this scenario.
            for field in (
                "agent", "target_agent", "initiator", "responder",
                "delegator", "delegatee", "via_peer", "issuer", "audience",
            ):
                val = getattr(step, field, None)
                if val and val not in agent_ids:
                    yield f"{ref} step {step.id}: {field}={val!r} not in agents {sorted(agent_ids)}"
            # capability must be offered somewhere in the scenario.
            if step.capability:
                offering = {a for a, caps in offered.items() if step.capability in caps}
                if not offering:
                    yield (
                        f"{ref} step {step.id}: capability {step.capability!r} "
                        f"not offered by any agent in this scenario"
                    )
            # requested_grants on handshake must be offered by the responder.
            if stype == "handshake" and step.requested_grants and step.responder in offered:
                missing = set(step.requested_grants) - offered[step.responder]
                if missing:
                    yield (
                        f"{ref} step {step.id}: requested_grants {sorted(missing)} "
                        f"not offered by responder {step.responder}"
                    )
            # via_delegation must reference an earlier step in this workflow.
            if step.via_delegation and step.via_delegation not in prior_step_ids:
                yield (
                    f"{ref} step {step.id}: via_delegation={step.via_delegation!r} "
                    f"does not reference an earlier step"
                )
            # input_from must reference a prior step too.
            if step.input_from and step.input_from not in prior_step_ids:
                yield (
                    f"{ref} step {step.id}: input_from={step.input_from!r} "
                    f"does not reference an earlier step"
                )
            prior_step_ids.add(step.id)


def cmd_lint(args: argparse.Namespace) -> int:
    reg = _registry()
    root = reg.settings.scenarios_path

    target_packs: list[Path]
    if args.packs:
        target_packs = []
        for name in args.packs:
            cand = (root / name).resolve()
            if not cand.exists() or not cand.is_dir():
                print(f"FAIL  pack {name!r} not found under {root}", file=sys.stderr)
                return 2
            target_packs.append(cand)
    else:
        target_packs = sorted(
            p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")
        )

    findings: list[str] = []
    for pack_dir in target_packs:
        findings.extend(_lint_pack_dir(pack_dir, reg))

    if not findings:
        print(f"ok  {len(target_packs)} pack(s) lint clean")
        return 0

    print(f"FAIL  {len(findings)} finding(s):")
    for f in findings:
        print(f"  - {f}")
    return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aitp-playground")
    sub = p.add_subparsers(required=True, dest="command")

    sub.add_parser("list", help="list scenarios").set_defaults(func=cmd_list)

    pv = sub.add_parser("validate", help="validate scenarios on disk")
    pv.add_argument("path", nargs="?", default=".")
    pv.set_defaults(func=cmd_validate)

    pd = sub.add_parser("dry-run", help="dry-run a scenario (no spawn)")
    pd.add_argument("scenario_ref")
    pd.add_argument("--inputs", default="{}")
    pd.set_defaults(func=cmd_dry_run)

    pn = sub.add_parser(
        "new",
        help="scaffold a new <pack>/<scenario>@<version> on disk",
    )
    pn.add_argument("scenario_ref", help="e.g. intra-org/my-scenario@1.0.0")
    pn.set_defaults(func=cmd_new)

    pl = sub.add_parser(
        "lint",
        help="cross-scenario checks (agent refs, capabilities, step graph)",
    )
    pl.add_argument(
        "packs",
        nargs="*",
        help="optional pack slugs to lint; default = every pack under scenarios/",
    )
    pl.set_defaults(func=cmd_lint)

    args = p.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
