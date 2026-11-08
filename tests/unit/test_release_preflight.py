"""scripts/release/preflight.sh, run against a scratch git repo and stubbed `gh` / `docker`.

The preflight is the release gate (DECISIONS.md D-33): release.yml retags only after it passes.
These tests pin its refusals (missing credentials, malformed or non-increasing version, a tag
at another commit, a commit off main, a missing or mislabelled `sha-` image, a check that is
not `success`) and the happy paths, and that it never retags (no `imagetools create`) and
never puts the Railway token in any argv. No network: `gh` and `docker` are PATH stubs.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "release" / "preflight.sh"
IMAGE = "ghcr.io/agentidentitytrustprotocol/aitp-playground"
TOKEN = "railway-project-token-that-must-never-reach-argv"

pytestmark = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("bash", "jq", "git")),
    reason="needs bash, jq and git",
)

# `docker buildx imagetools inspect <ref> --format <tmpl>` answers from $STUB_IMAGES
# ({ref: {"digest": ..., "revision": ...}}); an unknown ref exits 1 like a missing tag.
DOCKER_STUB = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys
    argv = sys.argv[1:]
    with open(os.environ["STUB_LOG"], "a") as log:
        log.write(json.dumps({{"tool": "docker", "argv": argv}}) + "\\n")
    if argv[:3] != ["buildx", "imagetools", "inspect"]:
        sys.exit(97)
    ref, fmt = argv[3], argv[argv.index("--format") + 1]
    images = json.loads(os.environ.get("STUB_IMAGES", "{{}}"))
    if ref not in images:
        print("ERROR: " + ref + ": not found", file=sys.stderr)
        sys.exit(1)
    img = images[ref]
    if fmt == "{{{{json .Manifest}}}}":
        print(json.dumps({{"mediaType": "application/vnd.oci.image.index.v1+json", "digest": img["digest"]}}))
    elif fmt == "{{{{json .Image}}}}":
        print(json.dumps({{"config": {{"Labels": {{"org.opencontainers.image.revision": img["revision"]}}}}}}))
    else:
        sys.exit(98)
    """
)

# `gh api repos/<repo>/commits/<sha>/check-runs...` prints $STUB_CHECKS.
GH_STUB = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys
    argv = sys.argv[1:]
    with open(os.environ["STUB_LOG"], "a") as log:
        log.write(json.dumps({{"tool": "gh", "argv": argv}}) + "\\n")
    if argv[0] != "api" or "/check-runs" not in argv[1]:
        sys.exit(97)
    print(os.environ.get("STUB_CHECKS", '{{"check_runs": []}}'))
    """
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> dict:
    """A repo with v0.2.0 on main, main HEAD two commits later, and a side-branch commit."""
    work = tmp_path / "repo"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@example.invalid")
    _git(work, "config", "user.name", "t")
    _git(work, "config", "commit.gpgsign", "false")
    _git(work, "config", "tag.gpgsign", "false")
    _git(work, "commit", "-q", "--allow-empty", "-m", "one")
    _git(work, "tag", "v0.2.0")
    old = _git(work, "rev-parse", "HEAD")
    _git(work, "tag", "not-a-version")
    _git(work, "commit", "-q", "--allow-empty", "-m", "two")
    _git(work, "commit", "-q", "--allow-empty", "-m", "three")
    head = _git(work, "rev-parse", "HEAD")
    _git(work, "update-ref", "refs/remotes/origin/main", head)
    _git(work, "checkout", "-q", "-b", "side")
    _git(work, "commit", "-q", "--allow-empty", "-m", "side")
    side = _git(work, "rev-parse", "HEAD")
    _git(work, "checkout", "-q", "main")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", DOCKER_STUB), ("gh", GH_STUB)):
        p = bin_dir / name
        p.write_text(body.format(python=sys.executable))
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return {
        "work": work,
        "bin": bin_dir,
        "log": tmp_path / "stub.log",
        "head": head,
        "old": old,
        "side": side,
        "out": tmp_path / "gh_output",
    }


def _digest(ch: str) -> str:
    return "sha256:" + ch * 64


def _checks(
    e2e: tuple[str, str | None] = ("completed", "success"),
    build: tuple[str, str | None] = ("completed", "success"),
) -> str:
    runs = [
        # An older failed attempt of e2e: the newest run (highest id) must win.
        {
            "id": 1,
            "name": "docker-compose e2e",
            "status": "completed",
            "conclusion": "failure",
        },
        {"id": 5, "name": "docker-compose e2e", "status": e2e[0], "conclusion": e2e[1]},
        {
            "id": 3,
            "name": "Build & push playground image",
            "status": build[0],
            "conclusion": build[1],
        },
        {
            "id": 4,
            "name": "Detect SDK pin changes",
            "status": "completed",
            "conclusion": "skipped",
        },
    ]
    return json.dumps({"check_runs": runs})


def _run(
    repo: dict,
    *,
    images: dict | None = None,
    checks: str | None = None,
    **env_over: str,
) -> subprocess.CompletedProcess[str]:
    head = repo["head"]
    if images is None:
        images = {f"{IMAGE}:sha-{head[:7]}": {"digest": _digest("a"), "revision": head}}
    env = {
        "PATH": f"{repo['bin']}{os.pathsep}{os.environ['PATH']}",
        "HOME": os.environ.get("HOME", "/tmp"),
        "STUB_LOG": str(repo["log"]),
        "STUB_IMAGES": json.dumps(images),
        "STUB_CHECKS": checks if checks is not None else _checks(),
        "GITHUB_OUTPUT": str(repo["out"]),
        "RELEASE_MODE": "release",
        "RELEASE_EVENT": "dispatch",
        "VERSION": "0.3.0",
        "COMMIT": head,
        "RAILWAY_TOKEN": TOKEN,
        "RAILWAY_PROJECT_ID": "proj",
        "RAILWAY_ENVIRONMENT_ID": "env",
        "RAILWAY_SERVICE_ID": "svc",
        "WAIT_TIMEOUT_SECONDS": "0",
        "POLL_SECONDS": "0",
    }
    env.update(env_over)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=repo["work"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _calls(repo: dict) -> list[dict]:
    if not repo["log"].exists():
        return []
    return [json.loads(line) for line in repo["log"].read_text().splitlines()]


def _assert_never_retags_or_leaks(
    repo: dict, proc: subprocess.CompletedProcess[str]
) -> None:
    for call in _calls(repo):
        assert "create" not in call["argv"], call
        assert TOKEN not in json.dumps(call["argv"])
    assert TOKEN not in proc.stdout + proc.stderr


def _outputs(proc: subprocess.CompletedProcess[str]) -> dict[str, str]:
    return dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)


def test_happy_path_dispatch(repo: dict) -> None:
    proc = _run(repo)
    assert proc.returncode == 0, proc.stderr
    out = _outputs(proc)
    assert out["commit"] == repo["head"]
    assert out["source_ref"] == f"{IMAGE}:sha-{repo['head'][:7]}"
    assert out["source_digest"] == _digest("a")
    assert (
        out["version"] == "0.3.0" and out["minor"] == "0.3" and out["tag"] == "v0.3.0"
    )
    assert out["prev_tag"] == "v0.2.0"
    assert "source_digest=" + _digest("a") in repo["out"].read_text()
    _assert_never_retags_or_leaks(repo, proc)


def test_happy_path_human_pushed_tag(repo: dict) -> None:
    _git(repo["work"], "tag", "v0.3.0", repo["head"])
    proc = _run(repo, RELEASE_EVENT="tag")
    assert proc.returncode == 0, proc.stderr
    assert _outputs(proc)["prev_tag"] == "v0.2.0"


def test_resumed_dispatch_with_tag_at_same_commit_passes(repo: dict) -> None:
    _git(repo["work"], "tag", "v0.3.0", repo["head"])
    proc = _run(repo)
    assert proc.returncode == 0, proc.stderr


def test_rollback_happy_path(repo: dict) -> None:
    images = {f"{IMAGE}:0.2.0": {"digest": _digest("b"), "revision": repo["old"]}}
    proc = _run(
        repo,
        images=images,
        checks='{"check_runs": []}',
        RELEASE_MODE="rollback",
        VERSION="0.2.0",
        COMMIT="",
    )
    assert proc.returncode == 0, proc.stderr
    out = _outputs(proc)
    assert out["commit"] == repo["old"]
    assert out["source_ref"] == f"{IMAGE}:0.2.0"
    assert out["source_digest"] == _digest("b")
    assert not [
        c for c in _calls(repo) if c["tool"] == "gh"
    ]  # rollback skips the checks


@pytest.mark.parametrize(
    "var",
    [
        "RAILWAY_TOKEN",
        "RAILWAY_PROJECT_ID",
        "RAILWAY_ENVIRONMENT_ID",
        "RAILWAY_SERVICE_ID",
    ],
)
def test_refuses_empty_credential(repo: dict, var: str) -> None:
    proc = _run(repo, **{var: ""})
    assert proc.returncode == 1
    assert f"{var} is empty" in proc.stderr
    assert _calls(repo) == []


@pytest.mark.parametrize(
    "version", ["0.3", "v0.3.0", "0.3.0-rc1", "01.3.0", "", "0.3.0 "]
)
def test_refuses_malformed_version(repo: dict, version: str) -> None:
    proc = _run(repo, VERSION=version)
    assert proc.returncode == 1
    assert "is not X.Y.Z" in proc.stderr


@pytest.mark.parametrize("version", ["0.1.9", "0.2.0"])
def test_refuses_version_not_above_last_tag(repo: dict, version: str) -> None:
    proc = _run(repo, VERSION=version)
    assert proc.returncode == 1
    # 0.2.0 exists at another commit; 0.1.9 is simply lower.
    assert (
        "not greater than the last tag v0.2.0" in proc.stderr
        or "already exists" in proc.stderr
    )
    _assert_never_retags_or_leaks(repo, proc)


def test_numeric_not_lexical_version_order(repo: dict) -> None:
    _git(repo["work"], "tag", "v0.10.0", repo["old"])
    assert _run(repo, VERSION="0.9.0").returncode == 1
    assert _run(repo, VERSION="0.11.0").returncode == 0


def test_refuses_existing_tag_at_other_commit(repo: dict) -> None:
    _git(repo["work"], "tag", "v0.3.0", repo["old"])
    proc = _run(repo)
    assert proc.returncode == 1
    assert "already exists" in proc.stderr


def test_tag_event_refuses_missing_tag(repo: dict) -> None:
    proc = _run(repo, RELEASE_EVENT="tag")
    assert proc.returncode == 1
    assert "does not exist" in proc.stderr


def test_refuses_commit_not_on_main(repo: dict) -> None:
    side = repo["side"]
    images = {f"{IMAGE}:sha-{side[:7]}": {"digest": _digest("c"), "revision": side}}
    proc = _run(repo, images=images, COMMIT=side)
    assert proc.returncode == 1
    assert "is not on origin/main" in proc.stderr
    _assert_never_retags_or_leaks(repo, proc)


def test_refuses_missing_sha_image_after_bounded_wait(repo: dict) -> None:
    proc = _run(repo, images={})
    assert proc.returncode == 1
    assert "gave up after 0s" in proc.stderr and "not in GHCR yet" in proc.stderr


def test_refuses_wrong_revision_label(repo: dict) -> None:
    images = {
        f"{IMAGE}:sha-{repo['head'][:7]}": {
            "digest": _digest("a"),
            "revision": repo["old"],
        }
    }
    proc = _run(repo, images=images)
    assert proc.returncode == 1
    assert "carries revision" in proc.stderr
    _assert_never_retags_or_leaks(repo, proc)


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped", "timed_out"])
def test_refuses_e2e_not_success(repo: dict, conclusion: str) -> None:
    proc = _run(repo, checks=_checks(e2e=("completed", conclusion)))
    assert proc.returncode == 1
    assert (
        f"check 'docker-compose e2e' on {repo['head'][:7]} concluded '{conclusion}'"
        in proc.stderr
    )
    _assert_never_retags_or_leaks(repo, proc)


def test_refuses_failed_image_build_check(repo: dict) -> None:
    proc = _run(repo, checks=_checks(build=("completed", "failure")))
    assert proc.returncode == 1
    assert "Build & push playground image" in proc.stderr


def test_refuses_e2e_still_running_after_bounded_wait(repo: dict) -> None:
    proc = _run(repo, checks=_checks(e2e=("in_progress", None)))
    assert proc.returncode == 1
    assert "gave up after 0s" in proc.stderr and "is in_progress" in proc.stderr


def test_refuses_e2e_never_reported(repo: dict) -> None:
    runs = json.loads(_checks())["check_runs"]
    checks = json.dumps(
        {"check_runs": [r for r in runs if r["name"] != "docker-compose e2e"]}
    )
    proc = _run(repo, checks=checks)
    assert proc.returncode == 1
    assert "not reported yet" in proc.stderr


def test_rollback_refuses_unknown_tag(repo: dict) -> None:
    proc = _run(repo, RELEASE_MODE="rollback", VERSION="0.1.0", COMMIT="")
    assert proc.returncode == 1
    assert "is not a tag" in proc.stderr


def test_rollback_refuses_missing_image_without_waiting(repo: dict) -> None:
    proc = _run(
        repo,
        images={},
        RELEASE_MODE="rollback",
        VERSION="0.2.0",
        COMMIT="",
        WAIT_TIMEOUT_SECONDS="600",
    )
    assert proc.returncode == 1
    assert "not in GHCR yet" in proc.stderr
