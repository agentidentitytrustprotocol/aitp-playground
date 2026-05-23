"""Scenario authoring CLI: validate, list, dry-run."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import jsonschema

from .config import get_settings
from .errors import PlaygroundError
from .registry.service import RegistryService


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

    args = p.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
