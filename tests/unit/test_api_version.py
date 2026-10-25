"""``apiVersion`` strict cutover: only the new value is accepted."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from aitp_playground.registry.models import (
    API_VERSION,
    AgentManifest,
    Pack,
    ScenarioTemplate,
    ScenarioVersion,
)

_DOCS = {
    Pack: {"kind": "ScenarioPack", "metadata": {"slug": "p", "name": "P"}},
    ScenarioTemplate: {"kind": "ScenarioTemplate", "metadata": {"name": "t"}},
    AgentManifest: {
        "kind": "AgentManifest",
        "metadata": {"id": "a", "name": "A", "framework": "custom"},
        "spec": {
            "entrypoint": {},
            "aitp": {"offered_caps": [], "display_name": "A"},
        },
    },
}


def test_api_version_value() -> None:
    assert API_VERSION == "agentidentitytrustprotocol.io/playground/v1"


@pytest.mark.parametrize("model", list(_DOCS))
def test_new_value_accepted(model) -> None:
    doc = model.model_validate({"apiVersion": API_VERSION, **_DOCS[model]})
    assert doc.api_version == API_VERSION


@pytest.mark.parametrize("model", [*_DOCS, ScenarioVersion])
def test_old_value_rejected_naming_new_value(model) -> None:
    with pytest.raises(ValidationError) as exc:
        model.model_validate({"apiVersion": "aitp.dev/v1", **_DOCS.get(model, {})})
    msg = str(exc.value)
    assert "retired" in msg
    assert API_VERSION in msg


@pytest.mark.parametrize("value", ["example.com/v1", "agentidentitytrustprotocol.io/playground/v2", ""])
def test_unknown_value_rejected(value: str) -> None:
    with pytest.raises(ValidationError) as exc:
        Pack.model_validate({"apiVersion": value, **_DOCS[Pack]})
    assert "unsupported apiVersion" in str(exc.value)
    assert API_VERSION in str(exc.value)
