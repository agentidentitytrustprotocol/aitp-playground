"""#117 Phase 3: the unit suite passes on a stable-only aitp-sdk wheel.

From aitp-sdk 0.24 the default wheel lacks renewal, session bundles and
multi-hop delegation. Re-running ``tests/unit`` with the
``tests._stable_only_sdk`` plugin (which deletes those attributes) must
pass, with exactly the ``@requires_sdk_feature`` tests skipped; with
``AITP_EXPECT_EXPERIMENTAL=1`` exactly those tests must fail.

Each run is a subprocess: the plugin would strip this session's real SDK.
"""
from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STABLE_ONLY = ["-p", "tests._stable_only_sdk"]


def _env(tmp: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "AITP_EXPECT_EXPERIMENTAL"}
    env["CP_BASE_URL"] = ""
    # The runs go in parallel; unit tests write fixed-name bootstrap files
    # under the temp dir, so each run gets its own.
    tmp.mkdir()
    env["TMPDIR"] = str(tmp)
    env.update(extra)
    return env


def _pytest(*args: str, env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "pytest", "tests/unit", "-q", "-p", "no:cacheprovider", *args],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def _node_key(nodeid: str) -> tuple[str, str]:
    """``tests/unit/a.py::Cls::test[x]`` -> junit's (classname, name)."""
    path, *rest = nodeid.split("::")
    classname = ".".join([path.removesuffix(".py").replace("/", "."), *rest[:-1]])
    return classname, rest[-1]


def _cases(junit: Path, tag: str, needle: str) -> set[tuple[str, str]]:
    out = set()
    for case in ET.parse(junit).getroot().iter("testcase"):
        for child in case:
            if child.tag == tag and needle in (child.get("message") or "") + (child.text or ""):
                out.add((case.get("classname"), case.get("name")))
    return out


def test_marked_tests_are_exactly_what_a_stable_only_wheel_skips(tmp_path) -> None:
    collect = _pytest("--collect-only", "-m", "sdk_feature", env=_env(tmp_path / "t-collect"))
    stable = _pytest(
        *STABLE_ONLY, f"--junitxml={tmp_path / 'stable.xml'}", env=_env(tmp_path / "t-stable"),
    )
    expect = _pytest(
        *STABLE_ONLY, f"--junitxml={tmp_path / 'expect.xml'}",
        env=_env(tmp_path / "t-expect", AITP_EXPECT_EXPERIMENTAL="1"),
    )
    procs = (collect, stable, expect)
    try:
        collected, _ = collect.communicate(timeout=300)
        stable_out, _ = stable.communicate(timeout=600)
        expect_out, _ = expect.communicate(timeout=600)
    finally:
        for proc in procs:  # never leave a suite running after a timeout
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    assert collect.returncode == 0, collected
    marked = {_node_key(line) for line in collected.splitlines() if "::" in line}
    # Renewal, bundle and multihop tests across five files.
    assert marked, collected

    assert stable.returncode == 0, stable_out[-4000:]
    # Every skip, not only ours: nothing else may skip on a stable-only wheel.
    skipped = _cases(tmp_path / "stable.xml", "skipped", "")
    assert skipped == marked, stable_out[-4000:]

    assert expect.returncode != 0, expect_out[-4000:]
    # The marker fails the test in setup, which junit records as <error>.
    xml = tmp_path / "expect.xml"
    refused = _cases(xml, "error", "AITP_EXPECT_EXPERIMENTAL=1")
    assert refused == marked, expect_out[-4000:]
    # ...and nothing else failed or errored.
    assert _cases(xml, "error", "") | _cases(xml, "failure", "") == marked, expect_out[-4000:]
