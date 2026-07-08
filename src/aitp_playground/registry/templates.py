"""Apply a ScenarioTemplate on top of a ScenarioVersion.

A template is a *named override*: ``trust`` is merged field-by-field on
top of the base, ``agents`` and ``workflow.steps`` are full
replacements. Anything the template omits falls through unchanged.

Full replacement (rather than per-step patching) for ``agents`` and
``workflow.steps`` is a deliberate choice — it keeps the merge
deterministic and small enough to read in one pass. If a future
template needs to *append* a step, write the full step list out; the
templates directory is a demo, not a config language.
"""
from __future__ import annotations

from .models import (
    ScenarioMeta,
    ScenarioTemplate,
    ScenarioVersion,
    TrustSpec,
)


def apply_template(base: ScenarioVersion, tpl: ScenarioTemplate) -> ScenarioVersion:
    """Return a new ScenarioVersion with the template's overrides applied."""
    spec_overrides: dict = {}
    if tpl.spec.trust is not None:
        merged_trust = {**base.spec.trust.model_dump(), **tpl.spec.trust}
        spec_overrides["trust"] = TrustSpec.model_validate(merged_trust)
    if tpl.spec.agents is not None:
        spec_overrides["agents"] = list(tpl.spec.agents)
    if tpl.spec.workflow is not None:
        spec_overrides["workflow"] = tpl.spec.workflow

    new_spec = base.spec.model_copy(update=spec_overrides, deep=True)

    suffix = f" (template: {tpl.metadata.name})"
    new_name = (
        base.metadata.name
        if base.metadata.name.endswith(suffix)
        else f"{base.metadata.name}{suffix}"
    )
    new_summary = tpl.metadata.summary or base.metadata.summary
    new_meta = ScenarioMeta(
        pack=base.metadata.pack,
        scenario=base.metadata.scenario,
        version=base.metadata.version,
        name=new_name,
        summary=new_summary,
        tags=list(base.metadata.tags),
    )
    return ScenarioVersion(
        apiVersion=base.api_version,
        kind=base.kind,
        metadata=new_meta,
        spec=new_spec,
    )
