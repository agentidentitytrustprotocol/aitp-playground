"""The published site (`docs/**` + `README.md`, see
`.github/workflows/notify-website.yml`) must not depend on unpublished
`internal_docs/`. Issue #61 found 24 such links, several on the first-run
path (Docker setup, LLM provider config, test layout) — a public reader
followed them into a directory that never left the repo.

Two files are the deliberate exception: `docs/README.md` and the top-level
`README.md` each carry one clearly-labelled "Contributor & build docs (in
the repo, not on the docs site)" section that indexes `internal_docs/` by
name. That's honest disclosure of the split, not the defect this guards
against, so it's allowlisted rather than forbidden.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCS_DIR = _REPO_ROOT / "docs"
_README = _REPO_ROOT / "README.md"

_ALLOWLISTED = {_DOCS_DIR / "README.md", _README}

_MARKDOWN_LINK = re.compile(r"\]\(([^)]*)\)")


def _published_markdown_files() -> list[Path]:
    return [*_DOCS_DIR.glob("*.md"), _README]


def test_no_published_doc_links_into_internal_docs() -> None:
    offenders: dict[str, list[str]] = {}
    for path in _published_markdown_files():
        if path in _ALLOWLISTED:
            continue
        for line_no, line in enumerate(path.read_text().splitlines(), start=1):
            for target in _MARKDOWN_LINK.findall(line):
                if "internal_docs" in target:
                    offenders.setdefault(str(path.relative_to(_REPO_ROOT)), []).append(
                        f"{line_no}: {target}"
                    )
    assert not offenders, (
        "Published docs must not link into unpublished internal_docs/ "
        f"(see issue #61): {offenders}"
    )
