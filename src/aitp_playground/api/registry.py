"""Read-only routes for the scenario registry."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..registry.service import RegistryService
from ._deps import get_registry

router = APIRouter(tags=["registry"])


@router.get("/packs")
def list_packs(registry: RegistryService = Depends(get_registry)) -> dict:
    return {"packs": [p.model_dump(by_alias=True) for p in registry.list_packs()]}


@router.get("/scenarios")
def list_scenarios(registry: RegistryService = Depends(get_registry)) -> dict:
    return {
        "scenarios": [
            {
                "ref": f"{s.metadata.pack}/{s.metadata.scenario}@{s.metadata.version}",
                "metadata": s.metadata.model_dump(),
            }
            for s in registry.list_scenarios()
        ]
    }


@router.get("/scenarios/{pack}/{scenario}@{version}")
def get_scenario(
    pack: str, scenario: str, version: str,
    registry: RegistryService = Depends(get_registry),
) -> dict:
    ref = f"{pack}/{scenario}@{version}"
    sv = registry.get_scenario(ref)
    body = sv.model_dump(by_alias=True)
    body["templates"] = [
        {"name": t.metadata.name, "summary": t.metadata.summary}
        for t in registry.list_templates(ref)
    ]
    return body


@router.get("/scenarios/{pack}/{scenario}@{version}/templates")
def list_scenario_templates(
    pack: str, scenario: str, version: str,
    registry: RegistryService = Depends(get_registry),
) -> dict:
    ref = f"{pack}/{scenario}@{version}"
    return {
        "ref": ref,
        "templates": [
            {"name": t.metadata.name, "summary": t.metadata.summary}
            for t in registry.list_templates(ref)
        ],
    }


@router.get("/scenarios/{pack}/{scenario}@{version}/templates/{name}")
def get_scenario_template(
    pack: str, scenario: str, version: str, name: str,
    registry: RegistryService = Depends(get_registry),
) -> dict:
    """Return the scenario *merged* with the named template — what the
    runner would execute if you passed ``template=<name>`` to ``POST /runs``.
    """
    ref = f"{pack}/{scenario}@{version}"
    resolved = registry.get_scenario_resolved(ref, template=name)
    return resolved.model_dump(by_alias=True)
