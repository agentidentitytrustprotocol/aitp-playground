"""The `aitp-sdk` floor's rationale comment must name its own version.

`pyproject.toml`'s dependency comment documents each breaking change behind
the floor as a bullet (`#   X.Y.Z — ...`). Phase 1 of this pass fixed the
comment stopping at 0.6.0 while the declared floor was already >=0.7.0 — the
same defect the original effort's own Phase 1 fixed once before, at
0.3.0-vs-0.4.0. The dependency-bump bot (`bump-aitp.yml`) can only ever move
`uv.lock`'s resolved version; the specifier and its rationale comment move
only by hand (`DECISIONS.md` D-18), so this is a recurring drift class, not a
one-off — this test is the mechanical catch for the next recurrence.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"

# Matches a top-level rationale bullet like "  #   0.7.0 — two verify calls...".
# Anchored on the leading "#   X.Y.Z —" shape so the nested
# `[tool.uv.sources]` example a few lines below (no version-then-em-dash) and
# any other comment in the file cannot match by accident.
_BULLET_RE = re.compile(r"^\s*#\s+(\d+\.\d+\.\d+)\s+—", re.MULTILINE)


def _declared_floor() -> tuple[int, int, int]:
    data = tomllib.loads(_PYPROJECT.read_text())
    for dep in data["project"]["dependencies"]:
        m = re.match(r"aitp-sdk>=(\d+)\.(\d+)\.(\d+)", dep)
        if m:
            return tuple(int(x) for x in m.groups())
    raise AssertionError("no 'aitp-sdk>=X.Y.Z' dependency found in pyproject.toml")


def _highest_rationale_bullet() -> tuple[int, int, int]:
    text = _PYPROJECT.read_text()
    versions = [tuple(int(x) for x in m.group(1).split(".")) for m in _BULLET_RE.finditer(text)]
    assert versions, (
        "the rationale-bullet regex matched nothing — either the comment "
        "block's format changed (update this test's pattern) or the "
        "comment was deleted (restore the rationale, don't just widen the "
        "regex to match nothing forever)"
    )
    return max(versions)


def test_the_rationale_comment_names_the_current_floor() -> None:
    declared = _declared_floor()
    highest_bullet = _highest_rationale_bullet()
    assert highest_bullet == declared, (
        f"pyproject.toml declares aitp-sdk>={'.'.join(map(str, declared))} but "
        f"the floor-rationale comment's highest bullet is "
        f"{'.'.join(map(str, highest_bullet))} — the floor moved without a "
        "bullet recording why (the exact defect Phase 1 of this cleanup "
        "and the original effort's own Phase 1 both had to fix once already)"
    )


def test_the_bullet_regex_is_not_vacuously_matching_nothing() -> None:
    """A regex that matches zero bullets would make the test above pass by
    doing nothing — pin that at least the known-historical bullets are
    found, so a format change is caught here rather than silently disabling
    the drift guard."""
    versions = {
        tuple(int(x) for x in m.group(1).split("."))
        for m in _BULLET_RE.finditer(_PYPROJECT.read_text())
    }
    assert (0, 3, 0) in versions, "the 0.3.0 historical bullet must still parse"
    assert (0, 7, 0) in versions or _declared_floor() != (0, 7, 0), (
        "expected the current floor's own bullet to be present"
    )
