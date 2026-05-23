"""!include YAML tag support (simple file-relative inclusion)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class _IncludeLoader(yaml.SafeLoader):
    """SafeLoader variant that records the base path for !include resolution."""

    def __init__(self, stream) -> None:  # type: ignore[no-untyped-def]
        try:
            self._root = Path(stream.name).parent
        except AttributeError:
            self._root = Path.cwd()
        super().__init__(stream)


def _construct_include(loader: _IncludeLoader, node: yaml.Node) -> Any:
    rel = loader.construct_scalar(node)  # type: ignore[arg-type]
    target = (loader._root / rel).resolve()
    with open(target, "r", encoding="utf-8") as f:
        return yaml.load(f, _IncludeLoader)


_IncludeLoader.add_constructor("!include", _construct_include)


def load_yaml(path: Path) -> Any:
    """Load a YAML file with !include support."""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.load(f, _IncludeLoader)
