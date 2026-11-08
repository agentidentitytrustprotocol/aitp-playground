"""scripts/console_e2e.py: the Railway-through-the-console E2E tool, tested
without the network. SSE parsing on canned streams, outcome and quota
classification, the 429 layer classifier, evidence scrubbing, the SSE-cut
fallback to polling, and whole modes against an ``httpx.MockTransport``
console."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "console_e2e.py"


def _load():
    name = "console_e2e_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(mod)
    return mod


ce = _load()

CONSOLE = "https://console.test"
PASSWORD = "hunter2-correct-horse"
RUN_ID = "11111111-2222-3333-4444-555555555555"


# --- SSE parsing --------------------------------------------------------------

def test_sse_parses_playground_frames_and_skips_heartbeats():
    stream = (
        'data: {"type": "run.started", "ts": 1}\n\n'
        ": heartbeat\n\n"
        'data: {"type": "llm.started", "ts": 2}\n\n'
        ": heartbeat\n\n"
        'data: {"type": "run.complete", "ts": 3}\n\n'
        'data: {"type":"stream.end"}\n\n'
    )
    events = list(ce.parse_sse_lines(stream.splitlines(keepends=True)))
    assert [e["type"] for e in events] == ["run.started", "llm.started", "run.complete", "stream.end"]


def test_sse_crlf_multiline_data_event_and_id_fields():
    lines = ["event: msg\r\n", "id: 7\r\n", 'data: {"type":\r\n', 'data: "x"}\r\n', "\r\n"]
    (evt,) = ce.parse_sse_lines(lines)
    assert evt == {"type": "x", "_event": "msg", "_id": "7"}


def test_sse_unparsed_payload_and_trailing_event_without_blank_line():
    events = list(ce.parse_sse_lines(["data: not json\n", "\n", 'data: {"type":"tail"}']))
    assert events[0] == {"type": "_unparsed", "data": "not json"}
    assert events[1]["type"] == "tail"


def test_sse_comment_only_stream_yields_nothing():
    assert list(ce.parse_sse_lines([": heartbeat\n", "\n", ": heartbeat\n", "\n"])) == []


# --- 429 layer detection ------------------------------------------------------

@pytest.mark.parametrize(
    "headers, body, layer",
    [
        ({"Retry-After": "412"},
         {"error": "Too many requests", "code": "CONSOLE_RATE_LIMITED", "retryAfterSeconds": 412,
          "limit": 5}, "console"),
        ({"Retry-After": "33"},
         {"code": "run_rate_limited", "limit": 5, "retry_after": 33, "detail": "at most 5"},
         "playground-per-ip"),
        ({"Retry-After": "5"},
         {"code": "max_concurrent_runs", "limit": 3, "retry_after": 5, "detail": "3 runs"},
         "playground-concurrency"),
        ({"Retry-After": "300"}, {"detail": "hourly run limit reached for this service"},
         "playground-hourly"),
        ({"Retry-After": "60"}, {"detail": "rate limit exceeded"}, "playground-token-bucket"),
        ({"Retry-After": "60"}, {"error": "Too many login attempts", "code": "rate_limited"},
         "console-login"),
        ({}, "<html>busy</html>", "unknown"),
    ],
)
def test_classify_429_layers(headers, body, layer):
    rl = ce.classify_429(headers, body)
    assert rl.layer == layer
    if headers:
        assert rl.retry_after == float(headers["Retry-After"])
        assert rl.retry_after_header == headers["Retry-After"]
    else:
        assert rl.retry_after_header is None


def test_classify_429_falls_back_to_body_retry_and_keeps_limit():
    rl = ce.classify_429({}, {"code": "CONSOLE_RATE_LIMITED", "retryAfterSeconds": 9, "limit": 4})
    assert (rl.layer, rl.limit, rl.retry_after, rl.retry_after_header) == ("console", 4, 9.0, None)


def test_parse_retry_after_http_date():
    assert ce.parse_retry_after("Thu, 01 Jan 1970 00:01:40 GMT", now=40.0) == 60.0
    assert ce.parse_retry_after("garbage") is None
    assert ce.parse_retry_after(None) is None


# --- expectations and outcome classification ----------------------------------

def _detail(steps, schema=None, sdk_available=True):
    return {"spec": {"inputs": {"schema": schema or {}}, "workflow": {"steps": steps}},
            "sdk_available": sdk_available}


def test_derive_inputs_from_schema_defaults_and_required_filler():
    schema = {"type": "object", "required": ["document", "n"],
              "properties": {"document": {"type": "string", "default": "Draft policy"},
                             "style": {"type": "string", "default": "casual"},
                             "n": {"type": "integer"}}}
    assert ce.derive_inputs(schema) == {"document": "Draft policy", "style": "casual", "n": 1}
    assert ce.derive_inputs({"required": ["topic"]}) == {"topic": ce.FILLER_TEXT}


def test_llm_steps_skip_faults_and_designed_rejections():
    steps = [
        {"id": "a", "capability": "write.content", "expect_status": 403},
        {"id": "b", "capability": "write.content", "fault": {"kind": "peer_offline"}},
        {"id": "c", "capability": "research.query"},
        {"id": "d", "type": "handshake"},
    ]
    assert ce.llm_capability_steps(_detail(steps)) == ["research.query"]


def test_expectation_sdk_unavailable_and_allowlist():
    exp = ce.expectation_for("x/y@1", _detail([{"capability": "write.content"}],
                                              sdk_available=False))
    assert exp.statuses == {"failed"} and exp.llm is False
    fi = ce.expectation_for("intra-org/fault-injection@1.0.0",
                            _detail([{"capability": "write.content"}]))
    assert fi.statuses == {"success"} and fi.llm is False and "allowlist" in fi.reason


def _exp(caps=("research.query",), **kw):
    return ce.Expectation(ref="r", statuses=frozenset({"success"}), llm=True, inputs={},
                          llm_capabilities=list(caps), **kw)


OK_RUN = {"status": "success", "llm_quota_exceeded": False, "llm_calls_on_mock": 0}


def _checks(run, events, exp):
    return {n: (ok, d) for n, ok, d in ce.classify_outcome(run, events, exp)}


def test_classify_outcome_pass_and_each_failure():
    exp = _exp()
    ok_events = [{"type": "llm.started"}, _real("research.query"), {"type": "run.complete"}]
    assert all(ok for _, ok, _ in ce.classify_outcome(OK_RUN, ok_events, exp))

    bad_run = {"status": "failed", "llm_quota_exceeded": True, "llm_calls_on_mock": 4}
    bad = {n: ok for n, ok, _ in ce.classify_outcome(
        bad_run, [{"type": "llm.quota_exceeded"}], exp)}
    assert {n for n, ok in bad.items() if not ok} == {
        "terminal_status", "llm.started present", "real llm.complete for research.query",
        "no llm.quota_exceeded", "llm_quota_exceeded false", "llm_calls_on_mock == 0"}


def test_classify_outcome_fails_on_unexpected_status_despite_success():
    # The runner records a designed rejection that came back wrong and still
    # ends the run success (runner/engine.py probe steps).
    events = [{"type": "llm.started"}, _real("research.query"),
              {"type": "step.unexpected_status", "step_id": "probe-1"}]
    c = _checks(OK_RUN, events, _exp())
    assert c["terminal_status"][0] is True
    assert c["no step.unexpected_status"] == (False, {"step_ids": ["probe-1"]})


def test_classify_outcome_fails_on_probe_matched_false():
    events = [{"type": "llm.started"}, _real("research.query"),
              {"type": "step.complete", "step_id": "deny",
               "result": {"status_code": 200, "expected_status": 403, "matched": False}},
              {"type": "step.access_denied", "step_id": "top", "matched": False},
              {"type": "step.complete", "step_id": "fine", "result": {"matched": True}}]
    ok, detail = _checks(OK_RUN, events, _exp())["no probe with matched=false"]
    assert ok is False and detail["step_ids"] == ["deny", "top"]


def test_real_llm_is_asserted_per_capability():
    exp = _exp(caps=("research.query", "write.content", "research.query"))
    events = [{"type": "llm.started"}, _real("research.query"),
              {"type": "llm.complete", "capability": "write.content", "provider": None,
               "model": None, "stub": True}]
    c = _checks(OK_RUN, events, exp)
    assert c["real llm.complete for research.query"][0] is True
    ok, detail = c["real llm.complete for write.content"]
    assert ok is False and detail["reason"] == "every llm.complete for it ran on the stub"
    # Distinct capabilities: research.query is checked once.
    assert sum(1 for n in c if n.startswith("real llm.complete")) == 2


def test_real_llm_missing_capability_is_named():
    c = _checks(OK_RUN, [{"type": "llm.started"}, _real("research.query")],
                _exp(caps=("research.query", "analyze.data")))
    ok, detail = c["real llm.complete for analyze.data"]
    assert ok is False and detail["reason"] == "no llm.complete for analyze.data"


def test_real_llm_fails_clearly_when_deploy_predates_stub_field():
    events = [{"type": "llm.started"}, {"type": "llm.complete", "task": "research"}]
    ok, detail = _checks(OK_RUN, events, _exp())["real llm.complete for research.query"]
    assert ok is False and detail["reason"] == ce.PREDATES_STUB_FIELD
    assert "predates" in ce.PREDATES_STUB_FIELD


def test_sdk_unavailable_requires_the_exact_failure_code():
    exp = ce.expectation_for("x/y@1", _detail([{"capability": "write.content"}],
                                              sdk_available=False))
    assert exp.failure_code == "sdk_feature_unavailable"
    failed = {"status": "failed"}
    good = _checks(failed, [{"type": "run.failed", "code": "sdk_feature_unavailable"}], exp)
    assert all(ok for ok, _ in good.values())
    other = _checks(failed, [{"type": "run.failed", "code": "inputs_invalid"}], exp)
    assert other["run.failed code = sdk_feature_unavailable"] == (
        False, {"run_failed_codes": ["inputs_invalid"]})
    assert other["terminal_status"][0] is True


def test_classify_quota_outcome_and_badge_text():
    run = {"status": "success", "llm_quota_exceeded": True, "llm_calls_on_mock": 5,
           "llm_quota": {"reason": "quota_exceeded", "quota": "per_ip_daily", "limit": 1,
                         "resets_at": "2026-11-09T00:00:00Z"}}
    events = [{"type": "llm.quota_exceeded", "resets_at": "2026-11-09T00:00:00Z"}]
    assert all(ok for _, ok, _ in ce.classify_quota_outcome(run, events))
    assert ce.badge_text(run) == (
        "LLM quota reached: mock responses (resets 2026-11-09T00:00:00Z) · 5 calls on mock")
    twice = ce.classify_quota_outcome(run, events * 2)
    assert dict((n, ok) for n, ok, _ in twice)["single llm.quota_exceeded"] is False
    assert ce.badge_text({"llm_quota_exceeded": False}) is None


# --- scrubbing ----------------------------------------------------------------

def test_scrub_removes_keys_bearer_password_and_sensitive_fields():
    rec = {
        "error": f"provider said sk-proj-abcDEF123456 and Bearer eyJhbGciOi.x.y; pw {PASSWORD}",
        "headers": {"Authorization": "Basic Zm9v", "Set-Cookie": "__Host-aitp-console-session=v1.x",
                    "x-api-key": "pk_live"},
        "nested": [{"password": PASSWORD}, "ok"],
    }
    out = json.dumps(ce.scrub(rec, {PASSWORD}))
    for leaked in (PASSWORD, "sk-proj-abcDEF123456", "eyJhbGciOi", "Zm9v", "v1.x", "pk_live"):
        assert leaked not in out
    assert "ok" in out and ce.REDACTED in out


def test_scrub_sk_needs_word_boundary_and_length():
    text = "task-abcd risk-assessment desk-lamp-0123456789abcdef sk-short"
    assert ce.scrub(text) == text
    assert ce.scrub("key=sk-proj-0123456789abcdef!") == f"key=sk-{ce.REDACTED}!"


def test_evidence_file_never_contains_password(tmp_path):
    ev = ce.Evidence(tmp_path / "e.jsonl", echo=False)
    ev.add_secret(PASSWORD)
    ev.record("x", note=f"login with {PASSWORD}", key="sk-ant-api03-zzzzzzzz")
    ev.check("c", False, body={"detail": f"Bearer {PASSWORD}"})
    text = (tmp_path / "e.jsonl").read_text()
    assert PASSWORD not in text and "sk-ant-api03-zzzzzzzz" not in text
    assert len(ev.failures) == 1


def test_evidence_path_inside_repo_refused(tmp_path):
    with pytest.raises(ce.SetupError):
        ce.safe_out_path(ROOT / "evidence.jsonl")
    assert ce.safe_out_path(tmp_path / "e.jsonl") == (tmp_path / "e.jsonl").resolve()


def test_read_password_env_first_then_doppler_failure_refuses():
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return __import__("subprocess").CompletedProcess(cmd, 1, stdout="", stderr="nope")

    assert ce.read_password({"CONSOLE_PASSWORD": PASSWORD}, runner) == PASSWORD
    assert calls == []
    with pytest.raises(ce.SetupError) as exc:
        ce.read_password({}, runner)
    assert calls == [ce.DOPPLER_CMD] and PASSWORD not in str(exc.value)


# --- a fake console -----------------------------------------------------------

SCENARIOS = [
    {"ref": "intra-org/research-and-write@1.0.0", "sdk_available": True},
    {"ref": "cross-cloud/distributed-review@1.0.0", "sdk_available": True},
]
DETAILS = {
    "intra-org/research-and-write@1.0.0": _detail(
        [{"capability": "research.query"}, {"capability": "write.content"}],
        {"required": ["topic"], "properties": {"topic": {"type": "string", "default": "AI"}}}),
    "cross-cloud/distributed-review@1.0.0": _detail(
        [{"capability": "research.query"}],
        {"required": ["document"], "properties": {"document": {"type": "string",
                                                               "default": "Draft"}}}),
}
def _real(cap, **kw):
    """An agent's ``llm.complete`` for a real provider call (agents/base/llm.py llm_meta)."""
    return {"type": "llm.complete", "capability": cap, "provider": "openai",
            "model": "gpt-4o-mini", "stub": False, **kw}


GOOD_EVENTS = [{"type": "run.started"}, {"type": "llm.started"},
               _real("research.query"), _real("write.content"), {"type": "run.complete"}]


def _sse(events, end=True):
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
    return body + ('data: {"type":"stream.end"}\n\n' if end else "")


class FakeConsole:
    def __init__(self, *, sse_cut=False, launch_429=0, public=True, status_seq=None,
                 run_extra=None):
        self.sse_cut = sse_cut
        self.launch_429 = launch_429
        self.public = public
        self.status_seq = list(status_seq or ["success"])
        self.run_extra = run_extra or {}
        self.launched: list[dict] = []
        self.status_calls = 0
        self.signed_in = False
        self.origins: list[str | None] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        cookie = req.headers.get("cookie", "")
        signed = "__Host-aitp-console-session=" in cookie
        if req.method == "POST":
            self.origins.append(req.headers.get("origin"))
            if req.headers.get("origin") != CONSOLE:
                return httpx.Response(403, json={"code": "csrf_blocked"})
        if path == "/login" and req.method == "POST":
            if json.loads(req.content)["password"] != PASSWORD:
                return httpx.Response(401, json={"code": "invalid_credentials"})
            return httpx.Response(200, json={"ok": True}, headers={
                "set-cookie": "__Host-aitp-console-session=v1.999.n.sig; Path=/; Secure; "
                              "HttpOnly; SameSite=Strict"})
        if path == "/api/auth/session":
            return httpx.Response(200, json={"publicMode": self.public, "signedIn": signed})
        if path == "/api/playground/metrics":
            return httpx.Response(200 if signed else 403, text="")
        if path == "/api/playground/scenarios":
            return httpx.Response(200, json={"scenarios": SCENARIOS})
        if path.startswith("/api/playground/scenarios/"):
            ref = path[len("/api/playground/scenarios/"):].replace("%40", "@")
            return httpx.Response(200, json=DETAILS[ref])
        if path == "/api/playground/runs" and req.method == "POST":
            if self.launch_429 > 0:
                self.launch_429 -= 1
                return httpx.Response(
                    429, headers={"Retry-After": "7"},
                    json={"code": "CONSOLE_RATE_LIMITED", "retryAfterSeconds": 7, "limit": 5})
            body = json.loads(req.content)
            self.launched.append(body)
            return httpx.Response(202, json={"run_id": RUN_ID, "status": "pending",
                                             "scenario_ref": body["scenario_ref"]})
        if path.endswith("/events"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text=_sse(GOOD_EVENTS[:2] if self.sse_cut else GOOD_EVENTS,
                                            end=not self.sse_cut))
        if path.endswith("/status"):
            self.status_calls += 1
            st = self.status_seq.pop(0) if len(self.status_seq) > 1 else self.status_seq[0]
            return httpx.Response(200, json={"run_id": RUN_ID, "status": st})
        if path == f"/api/playground/runs/{RUN_ID}":
            run = {"run_id": RUN_ID, "status": "success", "events": GOOD_EVENTS,
                   "llm_quota_exceeded": False, "llm_calls_on_mock": 0, **self.run_extra}
            return httpx.Response(200, json=run)
        return httpx.Response(404, json={"path": path})


def _client(fake, sleeps):
    return ce.ConsoleClient(CONSOLE, transport=httpx.MockTransport(fake), sleep=sleeps.append)


# --- SSE cut falls back to polling --------------------------------------------

def test_follow_sse_complete_does_not_poll():
    fake, sleeps = FakeConsole(), []
    res = _client(fake, sleeps).follow(RUN_ID, run_timeout=60, poll_interval=1)
    assert res.sse_complete and not res.polled and fake.status_calls == 0
    assert [e["type"] for e in res.events] == [e["type"] for e in GOOD_EVENTS]


def test_follow_sse_cut_polls_status_until_terminal_then_reads_run():
    fake = FakeConsole(sse_cut=True, status_seq=["running", "running", "success"])
    sleeps: list[float] = []
    res = _client(fake, sleeps).follow(RUN_ID, run_timeout=60, poll_interval=2)
    assert not res.sse_complete and res.polled
    assert res.sse_error == "stream ended without stream.end"
    assert fake.status_calls == 3 and sleeps == [2, 2]
    assert res.run["status"] == "success" and res.sse_events == 2
    # The run JSON's full event list wins over the partial stream.
    assert "llm.complete" in [e["type"] for e in res.events]


def test_follow_sse_transport_error_falls_back_to_polling():
    base = FakeConsole()

    def handler(req):
        if req.url.path.endswith("/events"):
            raise httpx.ReadTimeout("cut", request=req)
        return base(req)

    res = ce.ConsoleClient(CONSOLE, transport=httpx.MockTransport(handler),
                           sleep=lambda s: None).follow(RUN_ID, run_timeout=60, poll_interval=1)
    assert res.polled and "ReadTimeout" in res.sse_error and res.run["status"] == "success"


def test_poll_honours_the_deadline_through_429s():
    t = [0.0]
    sleeps: list[float] = []

    def sleep(s):
        sleeps.append(s)
        t[0] += s

    def handler(req):
        return httpx.Response(429, headers={"Retry-After": "30"},
                              json={"code": "CONSOLE_RATE_LIMITED", "retryAfterSeconds": 30})

    client = ce.ConsoleClient(CONSOLE, transport=httpx.MockTransport(handler),
                              sleep=sleep, clock=lambda: t[0])
    st = client.poll_until_terminal(RUN_ID, interval=5, deadline=100)
    assert st is None
    assert t[0] == 100 and sleeps == [30, 30, 30, 10]


# --- whole modes --------------------------------------------------------------

def _main(argv, fake, tmp_path, reader=lambda: PASSWORD):
    sleeps: list[float] = []
    out = tmp_path / "ev.jsonl"
    rc = ce.main([*argv, "--out", str(out), "--console-url", CONSOLE],
                 transport=httpx.MockTransport(fake), sleep=sleeps.append,
                 password_reader=reader)
    recs = [json.loads(line) for line in out.read_text().splitlines()]
    return rc, recs, sleeps, out.read_text()


def test_matrix_signed_in_passes_paces_and_never_writes_secrets(tmp_path):
    fake = FakeConsole()
    rc, recs, sleeps, text = _main(["matrix", "--auth", "signed-in", "--pace", "135"],
                                   fake, tmp_path)
    assert rc == 0, [r for r in recs if r.get("ok") is False]
    assert fake.origins and all(o == CONSOLE for o in fake.origins)
    assert [b["scenario_ref"] for b in fake.launched] == [s["ref"] for s in SCENARIOS]
    assert fake.launched[1]["inputs"] == {"document": "Draft"}
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 135
    assert PASSWORD not in text and "v1.999.n.sig" not in text


def test_matrix_retries_a_429_once_after_retry_after(tmp_path):
    fake = FakeConsole(launch_429=1)
    rc, recs, sleeps, _ = _main(["matrix", "--only", SCENARIOS[0]["ref"]], fake, tmp_path)
    assert rc == 0
    assert 8 in sleeps  # Retry-After 7 + 1 s
    assert [r["layer"] for r in recs if r["kind"] == "launch_429"] == ["console"]


def test_matrix_fails_on_quota_fallback(tmp_path):
    fake = FakeConsole(run_extra={"llm_quota_exceeded": True, "llm_calls_on_mock": 2})
    rc, recs, _, _ = _main(["matrix", "--limit", "1"], fake, tmp_path)
    assert rc == 1
    failed = {r["name"] for r in recs if r.get("ok") is False}
    assert any(n.endswith("llm_calls_on_mock == 0") for n in failed)


def _failing(fake, *, path_pred, error):
    """Wrap ``fake``: requests matching ``path_pred`` get ``error`` (a status or exception)."""
    def handler(req):
        if path_pred(req):
            if isinstance(error, int):
                return httpx.Response(error, text="upstream down")
            raise error("boom", request=req)
        return fake(req)
    return handler


@pytest.mark.parametrize("what,pred,error", [
    ("scenario detail 503",
     lambda r: r.url.path.endswith("research-and-write@1.0.0"), 503),
    ("scenario detail transport",
     lambda r: r.url.path.endswith("research-and-write@1.0.0"), httpx.ConnectError),
    ("launch transport",
     lambda r: r.method == "POST" and r.url.path == "/api/playground/runs"
     and b"research-and-write" in r.content, httpx.ReadTimeout),
])
def test_matrix_one_bad_ref_fails_it_and_continues(tmp_path, what, pred, error):
    fake = FakeConsole()
    rc, recs, _, _ = _main(["matrix"], _failing(fake, path_pred=pred, error=error), tmp_path)
    assert rc == 1, what
    failed = [r for r in recs if r.get("ok") is False]
    assert len(failed) == 1 and SCENARIOS[0]["ref"] in failed[0]["name"]
    assert failed[0]["reason"]
    # The second ref still ran and passed.
    assert [b["scenario_ref"] for b in fake.launched] == [SCENARIOS[1]["ref"]]
    assert any(r.get("ok") and r["name"].startswith(SCENARIOS[1]["ref"]) for r in recs)
    assert recs[-1]["kind"] == "end" and recs[-1]["failures"] == 1


def test_matrix_final_run_fetch_5xx_fails_that_ref_only(tmp_path):
    fake = FakeConsole()
    calls = {"n": 0}

    def pred(req):
        if req.method == "GET" and req.url.path == f"/api/playground/runs/{RUN_ID}":
            calls["n"] += 1
            return calls["n"] == 1
        return False

    rc, recs, _, _ = _main(["matrix"], _failing(fake, path_pred=pred, error=502), tmp_path)
    assert rc == 1 and len(fake.launched) == 2
    (failed,) = [r for r in recs if r.get("ok") is False]
    assert failed["name"].endswith("run followed") and "HTTP 502" in failed["reason"]
    assert failed["run_id"] == RUN_ID
    assert recs[-1]["kind"] == "end"


def test_matrix_scenario_list_5xx_is_a_failure_not_a_traceback(tmp_path):
    fake = FakeConsole()
    handler = _failing(fake, path_pred=lambda r: r.url.path == "/api/playground/scenarios",
                       error=500)
    rc, recs, _, _ = _main(["matrix"], handler, tmp_path)
    assert rc == 1 and fake.launched == []
    assert recs[-1]["kind"] == "end"


def test_quota_transport_error_still_writes_end(tmp_path):
    fake = FakeConsole()
    handler = _failing(fake, path_pred=lambda r: r.url.path.startswith(
        "/api/playground/scenarios/"), error=httpx.ConnectError)
    rc, recs, _, _ = _main(["quota"], handler, tmp_path)
    assert rc == 1 and fake.launched == []
    assert recs[-1]["kind"] == "end" and recs[-1]["failures"] == 1


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]"])
def test_bad_inputs_json_exits_2_before_any_request(tmp_path, raw):
    fake = FakeConsole()
    seen: list[str] = []

    def handler(req):
        seen.append(req.url.path)
        return fake(req)

    out = tmp_path / "ev.jsonl"
    rc = ce.main(["matrix", "--inputs", raw, "--out", str(out), "--console-url", CONSOLE],
                 transport=httpx.MockTransport(handler), sleep=lambda s: None,
                 password_reader=lambda: PASSWORD)
    assert rc == 2 and seen == [] and fake.launched == []


def test_setup_error_still_writes_end(tmp_path):
    fake = FakeConsole(public=False)
    rc, recs, _, _ = _main(["matrix"], fake, tmp_path)
    assert rc == 2 and recs[-1]["kind"] == "end" and "setup_error" in recs[-1]


def test_signed_in_without_password_refuses_before_launch(tmp_path):
    def reader():
        raise ce.SetupError("CONSOLE_PASSWORD unset and doppler exited 1")

    fake = FakeConsole()
    rc, _, _, _ = _main(["matrix", "--auth", "signed-in"], fake, tmp_path, reader=reader)
    assert rc == 2 and fake.launched == []


def test_anon_matrix_refuses_when_console_not_public(tmp_path):
    fake = FakeConsole(public=False)
    rc, _, _, _ = _main(["matrix"], fake, tmp_path)
    assert rc == 2 and fake.launched == []


def test_list_launches_nothing(tmp_path, capsys):
    fake = FakeConsole()
    rc, recs, _, _ = _main(["list"], fake, tmp_path)
    assert rc == 0 and fake.launched == []
    assert [r["ref"] for r in recs if r["kind"] == "plan"] == [s["ref"] for s in SCENARIOS]
    assert "2 scenario refs" in capsys.readouterr().out


def test_ratelimit_classifies_console_layer(tmp_path):
    fake = FakeConsole(launch_429=0)
    fake.status_seq = ["success"]
    launches = {"n": 0}
    inner = fake.__call__

    def handler(req):
        if req.url.path == "/api/playground/runs" and req.method == "POST":
            launches["n"] += 1
            if launches["n"] > 3:
                return httpx.Response(429, headers={"Retry-After": "590"},
                                      json={"code": "CONSOLE_RATE_LIMITED", "limit": 5,
                                            "retryAfterSeconds": 590})
        if req.url.path.startswith("/api/playground/scenarios/"):
            return httpx.Response(200, json=DETAILS["intra-org/research-and-write@1.0.0"])
        return inner(req)

    rc, recs, _, _ = _main(["ratelimit", "--expect-layer", "console"], handler, tmp_path)
    assert rc == 0, [r for r in recs if r.get("ok") is False]
    (hit,) = [r for r in recs if r["kind"] == "launch_429"]
    assert hit["layer"] == "console" and hit["retry_after_header"] == "590"


def test_quota_mode_passes_on_flagged_run(tmp_path):
    q_events = GOOD_EVENTS + [{"type": "llm.quota_exceeded", "resets_at": "2026-11-09T00:00:00Z"}]
    fake = FakeConsole(run_extra={
        "events": q_events, "llm_quota_exceeded": True, "llm_calls_on_mock": 5,
        "llm_quota": {"reason": "quota_exceeded", "quota": "per_ip_daily", "limit": 1,
                      "resets_at": "2026-11-09T00:00:00Z"}})
    rc, recs, _, _ = _main(["quota"], fake, tmp_path)
    assert rc == 0, [r for r in recs if r.get("ok") is False]
    (run,) = [r for r in recs if r["kind"] == "run"]
    assert run["badge_text"].startswith("LLM quota reached: mock responses")


def test_lockdown_asserts_status_codes(tmp_path):
    def handler(req):
        host, path = req.url.host, req.url.path
        if host == "pg.test":
            return httpx.Response(200 if path == "/healthz" else 401)
        if host == "cp.test":
            return httpx.Response(200 if path in ("/api/health", "/api/registry/agents") else 401)
        if path == "/api/auth/session":
            return httpx.Response(200, json={"publicMode": True, "signedIn": False})
        return httpx.Response(403)

    argv = ["lockdown", "--playground-url", "https://pg.test", "--cp-url", "https://cp.test"]
    rc, recs, _, _ = _main(argv, handler, tmp_path)
    asserts = [r for r in recs if r["kind"] == "assert"]
    assert rc == 0 and len(asserts) == 9 and all(r["ok"] for r in asserts)

    def leaky(req):
        if req.url.host == "pg.test" and req.url.path == "/scenarios":
            return httpx.Response(200)
        return handler(req)

    rc, recs, _, _ = _main(argv, leaky, tmp_path)
    assert rc == 1

    def private(req):
        # Public mode off: the console answers 401 to every anonymous route.
        if req.url.host in ("pg.test", "cp.test"):
            return handler(req)
        return httpx.Response(401, json={"code": "unauthenticated"})

    rc, recs, _, _ = _main(argv, private, tmp_path / "private")
    asserts = [r for r in recs if r["kind"] == "assert"]
    assert rc == 0 and all(r["ok"] for r in asserts)
    assert not any(r["name"] == "console session anonymous" for r in asserts)
    assert any(r["kind"] == "report" and r.get("name") == "console not in public mode"
               for r in recs)
    assert any(r["name"].startswith("console admin") and r["status"] == 401 for r in asserts)


def test_help_documents_every_mode(capsys):
    with pytest.raises(SystemExit):
        ce.main(["--help"])
    out = capsys.readouterr().out
    for mode in ("lockdown", "list", "matrix", "ratelimit", "quota"):
        assert mode in out


def test_interrupt_mid_run_still_writes_end_and_reraises(tmp_path):
    def handler(request):
        raise KeyboardInterrupt

    out = tmp_path / "ev.jsonl"
    with pytest.raises(KeyboardInterrupt):
        ce.main(["list", "--out", str(out), "--console-url", CONSOLE],
                transport=httpx.MockTransport(handler), sleep=lambda s: None,
                password_reader=lambda: PASSWORD)
    recs = [json.loads(line) for line in out.read_text().splitlines()]
    assert recs[-1]["kind"] == "end" and recs[-1]["aborted"] == "KeyboardInterrupt"


@pytest.mark.parametrize("mode", ["matrix", "quota", "ratelimit", "list"])
def test_default_pace_is_not_zero(mode):
    """Regression: a set_defaults(pace=0) on `list` leaked into every mode via the
    shared `launch` parent, so an unflagged matrix launched unpaced."""
    args = ce.build_parser().parse_args([mode])
    assert args.pace == ce.DEFAULT_PACE_S
