"""deploy/doppler-sync.sh, run against stubbed `doppler` and `curl`.

The script is the operator's only path from Doppler into Railway, so these
tests pin its safety properties: dry run by default, refusals (dirty tree,
RAILWAY_* names, ${{...}} references, Railway-only keys), "absent" keys
skipped, exactly one upsert per changed key with the value on stdin (never in
argv), a redeploy only after a change, and a hard stop when the post-apply
hash disagrees.
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
SCRIPT = REPO / "deploy" / "doppler-sync.sh"

pytestmark = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("bash", "jq", "git")),
    reason="needs bash, jq and git",
)

SECRET = "sk-test-value-that-must-never-leave-stdin"
TOKEN = "railway-token-that-must-never-reach-argv"

# Railway GraphQL stand-in. It keeps the service's variables in a JSON file,
# logs every request (argv, body, and whether the auth header file held the
# expected bearer token) and answers the four operations the script uses.
# Failure injection: FAKE_IGNORE_UPSERT=1 drops writes (post-apply mismatch);
# FAKE_FAIL="<op>:<http|errors>" fails the first request whose query contains
# <op> (an upsert matches only when FAKE_FAIL_KEY names its key);
# FAKE_NO_SERVICE=1 returns a project without the service.
CURL_STUB = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys
    state = os.environ["FAKE_STATE"]
    body = sys.stdin.read()
    argv = sys.argv[1:]
    auth_ok = False
    for i, a in enumerate(argv[:-1]):
        if a == "-H" and argv[i + 1].startswith("@"):
            with open(argv[i + 1][1:]) as fh:
                auth_ok = fh.read() == "Authorization: Bearer " + os.environ["RAILWAY_API_TOKEN"] + "\\n"
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(json.dumps({{"argv": argv, "body": body, "auth_ok": auth_ok}}) + "\\n")
    req = json.loads(body)
    q = req["query"]
    fail = os.environ.get("FAKE_FAIL", "")
    if fail:
        op, how = fail.split(":")
        key = req.get("variables", {{}}).get("i", {{}}).get("name")
        if op in q and (op != "variableUpsert" or key == os.environ.get("FAKE_FAIL_KEY")):
            if how == "http":
                print("curl: (22) The requested URL returned error: 500", file=sys.stderr)
                sys.exit(22)
            print(json.dumps({{"errors": [{{"message": "boom"}}]}}))
            sys.exit(0)
    if "project(id" in q and os.environ.get("FAKE_NO_SERVICE") == "1":
        data = {{"project": {{
            "environments": {{"edges": [{{"node": {{"id": "env-1", "name": "production"}}}}]}},
            "services": {{"edges": []}}}}}}
    elif "project(id" in q:
        data = {{"project": {{
            "environments": {{"edges": [{{"node": {{"id": "env-1", "name": "production"}}}}]}},
            "services": {{"edges": [{{"node": {{"id": "svc-1", "name": "aitp-playground"}}}}]}}}}}}
    elif "variables(" in q:
        data = {{"variables": json.load(open(state))}}
    elif "variableUpsert" in q:
        if os.environ.get("FAKE_IGNORE_UPSERT") != "1":
            i = req["variables"]["i"]
            cur = json.load(open(state))
            cur[i["name"]] = i["value"]
            json.dump(cur, open(state, "w"))
        data = {{"variableUpsert": True}}
    elif "serviceInstanceRedeploy" in q:
        data = {{"serviceInstanceRedeploy": True}}
    else:
        print(json.dumps({{"errors": [{{"message": "unexpected query"}}]}}))
        sys.exit(0)
    print(json.dumps({{"data": data}}))
    """
)

DOPPLER_STUB = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    echo "$*" >> "$FAKE_DOPPLER_ARGV"
    [ "$1 $2" = "secrets download" ] || exit 3
    [ "${FAKE_DOPPLER_FAIL:-0}" = 1 ] && { echo "Doppler Error: unauthorized" >&2; exit 1; }
    cat "$FAKE_DOPPLER"
    """
)


def _exe(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class Env:
    """A temp git repo holding a copy of deploy/, plus stub binaries and state."""

    def __init__(self, tmp: Path) -> None:
        self.repo = tmp / "repo"
        (self.repo / "deploy" / "doppler-keys").mkdir(parents=True)
        shutil.copy2(SCRIPT, self.repo / "deploy" / "doppler-sync.sh")
        self.bin = tmp / "bin"
        self.bin.mkdir()
        _exe(self.bin / "curl", CURL_STUB.format(python=sys.executable))
        _exe(self.bin / "doppler", DOPPLER_STUB)
        if shutil.which("shasum") is None:  # Linux without perl's shasum
            _exe(self.bin / "shasum", '#!/usr/bin/env bash\nsha256sum | sed "s/ .*/  -/"\n')
        self.state = tmp / "railway.json"
        self.doppler = tmp / "doppler.json"
        self.log = tmp / "curl.log"
        self.doppler_argv = tmp / "doppler.argv"
        self.log.write_text("")
        self.doppler_argv.write_text("")

    def setup(self, keys: list[str], doppler: dict, railway: dict, commit: bool = True) -> None:
        (self.repo / "deploy" / "doppler-keys" / "playground.txt").write_text(
            "# test allowlist\n" + "\n".join(keys) + "\n"
        )
        self.doppler.write_text(json.dumps(doppler))
        self.state.write_text(json.dumps(railway))
        if commit:
            git = ["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t"]
            subprocess.run([*git, "init", "-q"], check=True)
            subprocess.run([*git, "add", "-A"], check=True)
            subprocess.run([*git, "commit", "-qm", "init"], check=True)

    def run(
        self, *args: str, ignore_upsert: bool = False, **extra: str
    ) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.repo),  # no real ~/.railway/config.json
            "RAILWAY_API_TOKEN": TOKEN,
            "FAKE_STATE": str(self.state),
            "FAKE_LOG": str(self.log),
            "FAKE_DOPPLER": str(self.doppler),
            "FAKE_DOPPLER_ARGV": str(self.doppler_argv),
            "FAKE_IGNORE_UPSERT": "1" if ignore_upsert else "0",
            **extra,
        }
        return subprocess.run(
            ["bash", str(self.repo / "deploy" / "doppler-sync.sh"), "playground", *args],
            env=env, capture_output=True, text=True, timeout=60,
        )

    def requests(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def ops(self, name: str) -> list[dict]:
        return [r for r in self.requests() if name in json.loads(r["body"])["query"]]


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _no_leak(e: Env, proc: subprocess.CompletedProcess) -> None:
    out = proc.stdout + proc.stderr
    assert SECRET not in out
    assert TOKEN not in out
    for r in e.requests():
        assert not any(SECRET in a or TOKEN in a for a in r["argv"]), r["argv"]
        assert r["auth_ok"], "every Railway request must carry the bearer token header"
    assert SECRET not in e.doppler_argv.read_text()


BASE = {"OPENAI_API_KEY": SECRET, "LLM_PROVIDER": "openai"}


def test_script_is_executable_and_shellcheck_clean() -> None:
    assert os.access(SCRIPT, os.X_OK)
    if shutil.which("shellcheck"):
        subprocess.run(["shellcheck", str(SCRIPT)], check=True)


def test_help_and_bad_args() -> None:
    assert subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True).returncode == 0
    assert subprocess.run(["bash", str(SCRIPT)], capture_output=True).returncode == 2
    assert subprocess.run(["bash", str(SCRIPT), "nope"], capture_output=True).returncode == 2
    bad = subprocess.run(["bash", str(SCRIPT), "playground", "--bogus"], capture_output=True)
    assert bad.returncode == 2


def test_dry_run_unchanged_matches(env: Env) -> None:
    env.setup(list(BASE), dict(BASE), dict(BASE))
    p = env.run()
    assert p.returncode == 0, p.stderr
    assert "OPENAI_API_KEY\tunchanged" in p.stderr
    assert "(match)" in p.stderr
    assert "nothing to change" in p.stderr
    assert not env.ops("variableUpsert") and not env.ops("serviceInstanceRedeploy")
    _no_leak(env, p)


def test_reads_railway_unrendered(env: Env) -> None:
    # Without unrendered: true Railway resolves ${{...}} references, so the
    # railway-reference refusal could never fire.
    env.setup(list(BASE), dict(BASE), dict(BASE))
    p = env.run()
    assert p.returncode == 0, p.stderr
    reads = env.ops("variables(")
    assert reads
    for r in reads:
        assert "unrendered: true" in json.loads(r["body"])["query"].replace("unrendered:true", "unrendered: true")


def test_dry_run_reports_change_without_writing(env: Env) -> None:
    env.setup(list(BASE), dict(BASE), {**BASE, "LLM_PROVIDER": "anthropic"})
    p = env.run()
    assert p.returncode == 0, p.stderr
    assert "LLM_PROVIDER\tchanged" in p.stderr
    assert "1 key(s) would change" in p.stderr
    assert "(differ)" in p.stderr
    assert not env.ops("variableUpsert")


def test_absent_on_both_sides_is_skipped(env: Env) -> None:
    env.setup([*BASE, "ANTHROPIC_API_KEY"], dict(BASE), dict(BASE))
    p = env.run()
    assert p.returncode == 0, p.stderr
    assert "ANTHROPIC_API_KEY\tabsent" in p.stderr
    assert "(match)" in p.stderr


def test_railway_only_key_is_refused(env: Env) -> None:
    env.setup(list(BASE), {"LLM_PROVIDER": "openai"}, dict(BASE))
    p = env.run()
    assert p.returncode == 1
    assert "OPENAI_API_KEY\tmissing-in-doppler" in p.stderr
    assert "refusing: OPENAI_API_KEY" in p.stderr


def test_railway_reference_is_refused(env: Env) -> None:
    env.setup(["DATABASE_URL"], {"DATABASE_URL": "x"}, {"DATABASE_URL": "${{pg.DATABASE_URL}}"})
    p = env.run()
    assert p.returncode == 1
    assert "DATABASE_URL\trailway-reference" in p.stderr


def test_doppler_reference_is_refused(env: Env) -> None:
    env.setup(["DATABASE_URL"], {"DATABASE_URL": "${{pg.DATABASE_URL}}"}, {})
    p = env.run()
    assert p.returncode == 1
    assert "doppler-value-is-a-reference" in p.stderr


@pytest.mark.parametrize("name", ["RAILWAY_TOKEN", "lower_case", "1BAD"])
def test_invalid_allowlist_names_are_refused(env: Env, name: str) -> None:
    env.setup([name], {}, {})
    p = env.run()
    assert p.returncode == 1
    assert name in p.stderr
    assert not env.requests(), "must refuse before any network call"


def test_empty_allowlist_is_refused(env: Env) -> None:
    env.setup([], {}, {})
    p = env.run()
    assert p.returncode == 1
    assert "lists no keys" in p.stderr


def test_apply_refuses_dirty_tree(env: Env) -> None:
    env.setup(list(BASE), dict(BASE), {**BASE, "LLM_PROVIDER": "x"})
    (env.repo / "untracked.txt").write_text("dirty")
    p = env.run("--apply")
    assert p.returncode == 1
    assert "dirty" in p.stderr
    assert not env.requests()


def test_apply_nothing_to_change_does_not_redeploy(env: Env) -> None:
    env.setup(list(BASE), dict(BASE), dict(BASE))
    p = env.run("--apply")
    assert p.returncode == 0, p.stderr
    assert "nothing to change; no redeploy" in p.stderr
    assert not env.ops("variableUpsert") and not env.ops("serviceInstanceRedeploy")


def test_apply_upserts_changed_key_by_stdin_then_redeploys_once(env: Env) -> None:
    env.setup(list(BASE), dict(BASE), {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "old"})
    p = env.run("--apply")
    assert p.returncode == 0, p.stderr
    ups = env.ops("variableUpsert")
    assert len(ups) == 1
    i = json.loads(ups[0]["body"])["variables"]["i"]
    assert i["name"] == "OPENAI_API_KEY" and i["value"] == SECRET and i["skipDeploys"] is True
    assert (i["projectId"], i["environmentId"], i["serviceId"]) == (
        "70754f0b-c930-4433-a268-7b408e8230e2", "env-1", "svc-1")
    assert len(env.ops("serviceInstanceRedeploy")) == 1
    assert "hash after apply" in p.stderr and "(match)" in p.stderr
    assert json.loads(env.state.read_text())["OPENAI_API_KEY"] == SECRET
    _no_leak(env, p)


def test_apply_new_key_and_never_deletes_unlisted(env: Env) -> None:
    env.setup(list(BASE), dict(BASE), {"LLM_PROVIDER": "openai", "KEEP_ME": "1"})
    p = env.run("--apply")
    assert p.returncode == 0, p.stderr
    assert "OPENAI_API_KEY\tnew" in p.stderr
    assert "left alone (not in doppler-keys/playground.txt): KEEP_ME" in p.stderr
    assert json.loads(env.state.read_text())["KEEP_ME"] == "1"


def test_apply_no_redeploy_flag(env: Env) -> None:
    env.setup(list(BASE), dict(BASE), {**BASE, "LLM_PROVIDER": "x"})
    p = env.run("--apply", "--no-redeploy")
    assert p.returncode == 0, p.stderr
    assert len(env.ops("variableUpsert")) == 1
    assert not env.ops("serviceInstanceRedeploy")


def test_apply_hash_mismatch_after_write_stops_before_redeploy(env: Env) -> None:
    env.setup(list(BASE), dict(BASE), {**BASE, "LLM_PROVIDER": "x"})
    p = env.run("--apply", ignore_upsert=True)
    assert p.returncode == 1
    assert "hash after apply" in p.stderr and "expected" in p.stderr
    assert not env.ops("serviceInstanceRedeploy")


def test_no_railway_token_is_refused(env: Env, tmp_path: Path) -> None:
    env.setup(list(BASE), dict(BASE), dict(BASE))
    proc = subprocess.run(
        ["bash", str(env.repo / "deploy" / "doppler-sync.sh"), "playground"],
        env={"PATH": f"{env.bin}{os.pathsep}{os.environ['PATH']}", "HOME": str(tmp_path)},
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 1
    assert "no Railway token" in proc.stderr


# --- Failure paths: every one must fail closed, before any write or redeploy.


def test_missing_allowlist_file_is_refused(env: Env) -> None:
    env.setup(list(BASE), dict(BASE), dict(BASE))
    (env.repo / "deploy" / "doppler-keys" / "playground.txt").unlink()
    p = env.run()
    assert p.returncode == 1
    assert "doppler-keys/playground.txt not found" in p.stderr
    assert not env.requests()


@pytest.mark.parametrize("how", ["http", "errors"])
def test_railway_project_lookup_failure_stops(env: Env, how: str) -> None:
    env.setup(list(BASE), dict(BASE), dict(BASE))
    p = env.run(FAKE_FAIL=f"project(id:{how}")
    assert p.returncode != 0
    assert "Railway project lookup failed" in p.stderr
    if how == "errors":
        assert "railway: boom" in p.stderr


def test_service_not_found_stops(env: Env) -> None:
    env.setup(list(BASE), dict(BASE), dict(BASE))
    p = env.run("--apply", FAKE_NO_SERVICE="1")
    assert p.returncode == 1
    assert "service aitp-playground not found" in p.stderr
    assert not env.ops("variableUpsert")


@pytest.mark.parametrize("how", ["http", "errors"])
def test_railway_variables_read_failure_stops(env: Env, how: str) -> None:
    env.setup(list(BASE), dict(BASE), {**BASE, "LLM_PROVIDER": "x"})
    p = env.run("--apply", FAKE_FAIL=f"variables(:{how}")
    assert p.returncode == 1
    assert "reading Railway variables failed" in p.stderr
    assert not env.ops("variableUpsert")


def test_doppler_download_failure_stops(env: Env) -> None:
    env.setup(list(BASE), dict(BASE), {**BASE, "LLM_PROVIDER": "x"})
    p = env.run("--apply", FAKE_DOPPLER_FAIL="1")
    assert p.returncode == 1
    assert "doppler secrets download failed for aitp-playground/prd" in p.stderr
    assert not env.ops("variableUpsert")


def test_doppler_download_not_an_object_stops(env: Env) -> None:
    env.setup(list(BASE), dict(BASE), dict(BASE))
    env.doppler.write_text("[]")
    p = env.run()
    assert p.returncode == 1
    assert "Doppler download is not a JSON object" in p.stderr


@pytest.mark.parametrize("how", ["http", "errors"])
def test_partial_apply_stops_names_progress_and_never_redeploys(env: Env, how: str) -> None:
    keys = ["A_KEY", "B_KEY", "C_KEY"]
    env.setup(keys, {k: SECRET + k for k in keys}, {k: "old" for k in keys})
    p = env.run("--apply", FAKE_FAIL=f"variableUpsert:{how}", FAKE_FAIL_KEY="B_KEY")
    assert p.returncode == 1
    assert "setting B_KEY failed; already set: A_KEY; no redeploy" in p.stderr
    assert not env.ops("serviceInstanceRedeploy")
    state = json.loads(env.state.read_text())
    assert state == {"A_KEY": SECRET + "A_KEY", "B_KEY": "old", "C_KEY": "old"}
    # A re-run picks up where it stopped: A unchanged, B and C set, one redeploy.
    env.log.write_text("")
    p2 = env.run("--apply")
    assert p2.returncode == 0, p2.stderr
    assert "A_KEY\tunchanged" in p2.stderr
    assert len(env.ops("variableUpsert")) == 2
    assert len(env.ops("serviceInstanceRedeploy")) == 1


def test_apply_writes_planned_value_with_one_doppler_read(env: Env) -> None:
    env.setup(list(BASE), dict(BASE), {"LLM_PROVIDER": "x", "OPENAI_API_KEY": "old"})
    p = env.run("--apply")
    assert p.returncode == 0, p.stderr
    downloads = [ln for ln in env.doppler_argv.read_text().splitlines() if ln]
    assert len(downloads) == 1, downloads


def test_values_with_newlines_equals_and_unicode_round_trip(env: Env) -> None:
    tricky = {"PEM_KEY": "-----BEGIN-----\nab=cd\n-----END-----", "NOTE": "héllo=wörld ✓"}
    env.setup(list(tricky), dict(tricky), {})
    p = env.run("--apply")
    assert p.returncode == 0, p.stderr
    assert json.loads(env.state.read_text()) == tricky
    assert "hash after apply" in p.stderr and "(match)" in p.stderr
    p2 = env.run()
    assert "nothing to change" in p2.stderr and "(match)" in p2.stderr


def test_redeploy_failure_says_values_are_set(env: Env) -> None:
    env.setup(list(BASE), dict(BASE), {**BASE, "LLM_PROVIDER": "x"})
    p = env.run("--apply", FAKE_FAIL="serviceInstanceRedeploy:http")
    assert p.returncode == 1
    assert "values set and verified, but the redeploy call failed" in p.stderr
    assert json.loads(env.state.read_text())["LLM_PROVIDER"] == "openai"
