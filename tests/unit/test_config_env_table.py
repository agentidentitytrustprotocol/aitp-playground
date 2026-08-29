"""Every `Settings` field a deployment can meaningfully set must appear in
`docs/getting-started.md`'s env table — the same class of drift guard as
`test_sdk_floor_comment_matches_specifier.py`. `PENDING.md` P13.2 found six
fields missing (`CP_AID` and the three `REVOCATION_*` vars, `PUBLIC_HOST`,
`PUBLIC_SCHEME`); this stops the table drifting again the same way.
"""
from __future__ import annotations

import re
from pathlib import Path

from aitp_playground.config import Settings

_DOCS = Path(__file__).resolve().parents[2] / "docs" / "getting-started.md"


def test_every_settings_field_is_documented_in_the_env_table() -> None:
    table_text = _DOCS.read_text()
    documented = set(re.findall(r"`([A-Z][A-Z0-9_]*)`", table_text))

    missing = [
        name.upper()
        for name in Settings.model_fields
        if name.upper() not in documented
    ]
    assert not missing, (
        f"Settings field(s) {missing} have no row in "
        f"{_DOCS.relative_to(_DOCS.parents[2])}'s env table"
    )
