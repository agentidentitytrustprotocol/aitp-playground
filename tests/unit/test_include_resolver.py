"""!include YAML tag — file-relative inclusion used by scenario packs."""
from __future__ import annotations

from pathlib import Path

from aitp_playground.registry.include_resolver import load_yaml


def test_include_inlines_referenced_file(tmp_path: Path) -> None:
    (tmp_path / "agents.yaml").write_text(
        "- id: researcher\n- id: writer\n"
    )
    main = tmp_path / "scenario.yaml"
    main.write_text(
        "name: demo\nagents: !include agents.yaml\n"
    )
    loaded = load_yaml(main)
    assert loaded == {
        "name": "demo",
        "agents": [{"id": "researcher"}, {"id": "writer"}],
    }


def test_include_resolves_relative_to_the_including_file(tmp_path: Path) -> None:
    """The include path is relative to the file that declares it — not the
    process cwd — and nested includes resolve relative to *their* file."""
    parts = tmp_path / "parts"
    parts.mkdir()
    (parts / "trust.yaml").write_text("boundary: intra_org\n")
    (parts / "spec.yaml").write_text("trust: !include trust.yaml\n")
    main = tmp_path / "scenario.yaml"
    main.write_text("spec: !include parts/spec.yaml\n")

    loaded = load_yaml(main)
    assert loaded == {"spec": {"trust": {"boundary": "intra_org"}}}


def test_plain_yaml_loads_unchanged(tmp_path: Path) -> None:
    f = tmp_path / "plain.yaml"
    f.write_text("k: v\nn: 3\n")
    assert load_yaml(f) == {"k": "v", "n": 3}
