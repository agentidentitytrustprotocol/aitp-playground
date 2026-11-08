#!/usr/bin/env python3
"""Railway E2E through the public console: lockdown, matrix, 429 and quota probes.

Drives the AITP console's HTTP API (``/api/playground/*``) the way a browser
does: an ``Origin`` header on every mutation, the ``__Host-aitp-console-session``
cookie for an operator, the SSE event stream, and polling of ``/runs/{id}`` when
that stream is cut (Vercel ends it at ``maxDuration`` = 300 s).

Modes (``uv run python scripts/console_e2e.py <mode> --help`` for each):

  lockdown   Keyless probes of the playground and the control plane, plus the
             console's anonymous view. Read-only: launches nothing.
  list       The live scenario list, with the inputs and the designed outcome
             the matrix would use for each ref. Launches nothing.
  matrix     Every live scenario ref, launched through the console, followed to
             the end and asserted. ``--auth anon|signed-in``. Costs LLM money.
  ratelimit  Cheap sequential launches until a 429; asserts Retry-After and
             names the layer (console, playground per-IP / concurrency /
             hourly / token bucket).
  quota      One research-and-write launch while the per-IP LLM quota is
             exhausted; asserts the run fell back to the mock and carries the
             fields the console's "LLM quota reached" badge keys on.

URLs come from flags or env (``CONSOLE_URL``, ``PLAYGROUND_URL``, ``CP_URL``),
with the production hosts as documented defaults. Evidence is JSONL written to
``--out`` (default under ``$TMPDIR``, never inside the repo); every record is
scrubbed of ``sk-`` keys, ``Bearer`` tokens, cookies and the console password.

The operator password (signed-in mode) is read from ``CONSOLE_PASSWORD`` or,
when unset, from ``doppler secrets get CONSOLE_PASSWORD --project aitp-ui-console
--config prd --plain``. It is held in memory only and never logged.

Exit status: 0 when every assertion held, 1 when any failed (a transport error
or 5xx counts as a failure: in ``matrix`` it fails that ref and the matrix goes
on), 2 on a usage or setup error (bad URL, bad ``--inputs`` JSON, missing
password, evidence path inside the repo). An ``end`` record is always written.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import email.utils
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional
from urllib.parse import quote, urlsplit

import httpx

# ---------------------------------------------------------------------------
# Defaults (documented in --help and internal_docs/testing.md)
# ---------------------------------------------------------------------------

DEFAULT_CONSOLE_URL = "https://console.agentidentitytrustprotocol.io"
DEFAULT_PLAYGROUND_URL = "https://aitp-playground-production.up.railway.app"
#: aitp-control-plane/scripts/handoff/rotate-api-key.sh:51
DEFAULT_CP_URL = "https://aitp-control-plane-production.up.railway.app"

#: Seconds between launches. The console's launch bucket and the playground's
#: per-IP window are both 5 per 600 s; the global cap is 30 runs/hour. 135 s
#: keeps a long matrix under all three (26.7 launches/hour).
DEFAULT_PACE_S = 135.0
#: The playground ends a run at RUN_DEADLINE_SECONDS (900 s); give it slack.
DEFAULT_RUN_TIMEOUT_S = 960.0
DEFAULT_POLL_INTERVAL_S = 5.0
#: A Retry-After longer than this fails the step instead of sleeping.
DEFAULT_MAX_WAIT_S = 900.0

SESSION_COOKIE = "__Host-aitp-console-session"
TERMINAL_STATUSES = frozenset({"success", "failed", "cancelled"})
TERMINAL_EVENTS = frozenset({"run.complete", "run.failed", "run.cancelled"})
#: Capability prefixes served by an LLM-backed agent (researcher / writer /
#: analyzer). Each call emits ``llm.started`` + ``llm.complete``, stub or not;
#: ``llm.complete`` carries ``capability``/``provider``/``model``/``stub``.
LLM_CAPABILITY_PREFIXES = ("research.", "write.", "analyze.")

DOPPLER_CMD = [
    "doppler", "secrets", "get", "CONSOLE_PASSWORD",
    "--project", "aitp-ui-console", "--config", "prd", "--plain",
]

#: Designed outcomes that the scenario packs cannot express. No pack declares
#: an expected outcome (only per-step ``expect_status``), and every pack is
#: designed to end ``success``: fault and rejection steps record their result
#: instead of failing the run. A rejection that came back with the wrong
#: status is recorded too (``step.unexpected_status`` / ``matched: false``)
#: and the run still ends ``success``, so ``classify_outcome`` fails on those.
#: Entries here override what the tool derives from the pack:
#:   statuses  - acceptable terminal statuses;
#:   llm       - whether a real (``stub: false``) llm.complete must appear.
#: A pack whose ``sdk_available`` is false is expected to fail with
#: ``sdk_feature_unavailable`` (derived, not listed here).
DESIGNED_OUTCOMES: dict[str, dict[str, Any]] = {
    # Its only capability step is the injected peer_offline fault, so no LLM
    # call ever succeeds; the run itself still ends success.
    "intra-org/fault-injection@1.0.0": {"statuses": {"success"}, "llm": False},
}

#: Cheapest ref for ``ratelimit``: no network, no LLM capability.
DEFAULT_RATELIMIT_REF = "intra-org/spki-pinning@1.0.0"
DEFAULT_QUOTA_REF = "intra-org/research-and-write@1.0.0"
FILLER_TEXT = "AITP console e2e"

REPO_ROOT = Path(__file__).resolve().parent.parent


class SetupError(Exception):
    """A usage or environment problem: exit 2, before any launch."""


#: A transport failure, an HTTP error status (``raise_for_status``: 5xx and
#: friends) or a non-JSON body. Caught per ref in ``matrix``; elsewhere it
#: becomes a failed assertion (exit 1), never a traceback.
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (httpx.HTTPError, ValueError)


def _err(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} from {exc.request.url.path}"
    return f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Scrubbing and evidence
# ---------------------------------------------------------------------------

#: Word boundary + minimum length, so ``task-abcd`` / ``risk-assessment`` survive.
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s\"',;]+")
_SENSITIVE_KEYS = frozenset({
    "password", "authorization", "cookie", "set-cookie", "x-api-key", "api_key",
    "apikey", SESSION_COOKIE.lower(),
})
REDACTED = "[REDACTED]"


def scrub(obj: Any, secrets: Iterable[str] = ()) -> Any:
    """Return a copy of ``obj`` with secrets removed.

    Every string has each literal secret, ``sk-…`` keys and ``Bearer <token>``
    replaced; every dict value under a sensitive key name (password, cookie,
    authorization, x-api-key, the session cookie) is replaced outright.
    """
    lits = sorted({s for s in secrets if s}, key=len, reverse=True)

    def s(v: Any) -> Any:
        if isinstance(v, str):
            for lit in lits:
                v = v.replace(lit, REDACTED)
            v = _SK_RE.sub("sk-" + REDACTED, v)
            return _BEARER_RE.sub("Bearer " + REDACTED, v)
        if isinstance(v, dict):
            return {
                k: (REDACTED if str(k).lower() in _SENSITIVE_KEYS else s(x))
                for k, x in v.items()
            }
        if isinstance(v, (list, tuple)):
            return [s(x) for x in v]
        return v

    return s(obj)


class Evidence:
    """Append-only JSONL evidence; scrubs every record before writing."""

    def __init__(self, path: Optional[Path], secrets: Optional[set[str]] = None,
                 echo: bool = True) -> None:
        self.path = path
        self.secrets: set[str] = secrets if secrets is not None else set()
        self.echo = echo
        self.records: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def add_secret(self, value: Optional[str]) -> None:
        if value:
            self.secrets.add(value)

    def record(self, kind: str, **fields: Any) -> dict[str, Any]:
        rec = scrub({"ts": _now_iso(), "kind": kind, **fields}, self.secrets)
        self.records.append(rec)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, default=str, sort_keys=True) + "\n")
        return rec

    def check(self, name: str, ok: bool, **fields: Any) -> bool:
        rec = self.record("assert", name=name, ok=bool(ok), **fields)
        if not ok:
            self.failures.append(rec)
        if self.echo:
            detail = {k: v for k, v in rec.items() if k not in ("ts", "kind", "name", "ok")}
            print(f"  [{'PASS' if ok else 'FAIL'}] {name} {json.dumps(detail, default=str)}")
        return bool(ok)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def default_out_path(mode: str) -> Path:
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(tempfile.gettempdir()) / "aitp-console-e2e" / f"{mode}-{stamp}.jsonl"


def safe_out_path(path: Path, repo_root: Path = REPO_ROOT) -> Path:
    """Refuse an evidence path inside the repository."""
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(repo_root.resolve())
    except ValueError:
        return resolved
    raise SetupError(f"refusing to write evidence inside the repo: {resolved}")


# ---------------------------------------------------------------------------
# SSE parsing
# ---------------------------------------------------------------------------


def parse_sse_lines(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """Decode an SSE line stream into event dicts.

    The playground frames every event as ``data: <json>\\n\\n`` and idles with
    ``: heartbeat`` comments; the console relays those bytes unchanged
    (``lib/api/proxy.ts`` ``proxySse``). Per the SSE spec, consecutive
    ``data:`` lines of one event are joined with ``\\n``; ``event:``/``id:``
    fields are kept as ``_event``/``_id``. A non-JSON payload is yielded as
    ``{"type": "_unparsed", "data": …}``.
    """
    data: list[str] = []
    meta: dict[str, str] = {}

    def flush() -> Optional[dict[str, Any]]:
        if not data:
            meta.clear()
            return None
        raw = "\n".join(data)
        data.clear()
        try:
            evt = json.loads(raw)
            if not isinstance(evt, dict):
                evt = {"type": "_unparsed", "data": raw}
        except ValueError:
            evt = {"type": "_unparsed", "data": raw}
        for k, v in meta.items():
            evt.setdefault(f"_{k}", v)
        meta.clear()
        return evt

    for line in lines:
        line = line.rstrip("\r\n")
        if line == "":
            evt = flush()
            if evt is not None:
                yield evt
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "data":
            data.append(value)
        elif name in ("event", "id"):
            meta[name] = value
    evt = flush()
    if evt is not None:
        yield evt


# ---------------------------------------------------------------------------
# 429 classification
# ---------------------------------------------------------------------------


@dataclass
class RateLimit:
    layer: str
    code: Optional[str]
    limit: Optional[int]
    retry_after: Optional[float]
    retry_after_header: Optional[str]
    detail: Optional[str] = None


def parse_retry_after(value: Optional[str], now: Optional[float] = None) -> Optional[float]:
    """Seconds from a ``Retry-After`` header (delta-seconds or HTTP-date)."""
    if value is None or value.strip() == "":
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    return max(0.0, when.timestamp() - (time.time() if now is None else now))


def _json_or_none(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def classify_429(headers: httpx.Headers | dict[str, str], body: Any) -> RateLimit:
    """Name the layer that refused a launch.

    - ``console``: ``{"code": "CONSOLE_RATE_LIMITED", "limit": …}`` from
      ``lib/server/access.ts`` ``rateLimited`` (launch bucket 5/600 s, read
      300/min, stream gate 4/IP) or the edge's Basic-failure limiter.
    - ``console-login``: ``{"code": "rate_limited"}`` from ``POST /login``.
    - ``playground-per-ip`` / ``playground-concurrency``: the flat
      ``{code, limit, retry_after, detail}`` of ``RunLimitExceeded``
      (``run_rate_limited``, ``max_concurrent_runs``; ``limits.py``).
    - ``playground-hourly``: ``{"detail": "hourly run limit reached …"}``
      (``api/_deps.py``, the global ``MAX_RUNS_PER_HOUR`` cap).
    - ``playground-token-bucket``: ``{"detail": "rate limit exceeded"}``
      (``api/_deps.py``, per-IP / per-key token buckets).
    """
    h = httpx.Headers(headers)
    ra_header = h.get("retry-after")
    b = body if isinstance(body, dict) else {}
    code = b.get("code") if isinstance(b.get("code"), str) else None
    detail = b.get("detail") if isinstance(b.get("detail"), str) else None
    limit = b.get("limit") if isinstance(b.get("limit"), int) else None
    body_retry = b.get("retryAfterSeconds", b.get("retry_after"))
    if code == "CONSOLE_RATE_LIMITED":
        layer = "console"
    elif code == "rate_limited":
        layer = "console-login"
    elif code == "run_rate_limited":
        layer = "playground-per-ip"
    elif code == "max_concurrent_runs":
        layer = "playground-concurrency"
    elif detail and "hourly run limit reached" in detail:
        layer = "playground-hourly"
    elif detail == "rate limit exceeded":
        layer = "playground-token-bucket"
    else:
        layer = "unknown"
    retry = parse_retry_after(ra_header)
    if retry is None and isinstance(body_retry, (int, float)):
        retry = float(body_retry)
    return RateLimit(layer, code, limit, retry, ra_header, detail)


# ---------------------------------------------------------------------------
# Expectations and outcome classification
# ---------------------------------------------------------------------------


@dataclass
class Expectation:
    ref: str
    statuses: frozenset[str]
    llm: bool
    inputs: dict[str, Any]
    reason: str = "pack: designed to end success"
    sdk_available: bool = True
    llm_capabilities: list[str] = field(default_factory=list)
    #: The ``code`` a designed failure's ``run.failed`` event must carry.
    failure_code: Optional[str] = None


#: ``run.failed`` code for a pack the deployed SDK wheel can't run
#: (``errors.SdkFeatureUnavailableError.code``, raised by runner/engine.py).
SDK_UNAVAILABLE_CODE = "sdk_feature_unavailable"


def derive_inputs(schema: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Inputs from a pack's JSON schema: every property's default; a required
    property without one gets filler of its type."""
    schema = schema or {}
    props = schema.get("properties") or {}
    out: dict[str, Any] = {}
    for name, spec in props.items():
        if isinstance(spec, dict) and "default" in spec:
            out[name] = spec["default"]
    for name in schema.get("required") or []:
        if name in out:
            continue
        typ = (props.get(name) or {}).get("type", "string")
        out[name] = {"string": FILLER_TEXT, "integer": 1, "number": 1,
                     "boolean": True, "array": [], "object": {}}.get(typ, FILLER_TEXT)
    return out


def llm_capability_steps(detail: dict[str, Any]) -> list[str]:
    """Capabilities whose call the pack expects to succeed on an LLM agent:
    a step naming an LLM capability with no injected ``fault`` and no
    non-200 ``expect_status`` (a designed rejection never reaches the LLM)."""
    steps = (((detail.get("spec") or {}).get("workflow") or {}).get("steps")) or []
    caps: list[str] = []
    for st in steps:
        if not isinstance(st, dict):
            continue
        cap = st.get("capability")
        if not isinstance(cap, str) or not cap.startswith(LLM_CAPABILITY_PREFIXES):
            continue
        if st.get("fault"):
            continue
        if st.get("expect_status") not in (None, 200):
            continue
        caps.append(cap)
    return caps


def expectation_for(ref: str, detail: dict[str, Any],
                    summary: Optional[dict[str, Any]] = None,
                    overrides: Optional[dict[str, dict[str, Any]]] = None) -> Expectation:
    summary = summary or {}
    inputs = derive_inputs(((detail.get("spec") or {}).get("inputs") or {}).get("schema"))
    caps = llm_capability_steps(detail)
    sdk_ok = detail.get("sdk_available", summary.get("sdk_available", True)) is not False
    exp = Expectation(ref=ref, statuses=frozenset({"success"}), llm=bool(caps),
                      inputs=inputs, sdk_available=sdk_ok, llm_capabilities=caps)
    if not sdk_ok:
        exp.statuses = frozenset({"failed"})
        exp.llm = False
        exp.failure_code = SDK_UNAVAILABLE_CODE
        exp.reason = "pack: sdk_available=false, refused with sdk_feature_unavailable"
    table = DESIGNED_OUTCOMES if overrides is None else overrides
    if ref in table:
        o = table[ref]
        if "statuses" in o:
            exp.statuses = frozenset(o["statuses"])
            if "failed" not in exp.statuses:
                exp.failure_code = None
        if "llm" in o:
            exp.llm = bool(o["llm"])
        exp.reason = "allowlist: DESIGNED_OUTCOMES"
    return exp


def _probe_unmatched(evt: dict[str, Any]) -> bool:
    """A probe step whose observed status differed from ``expect_status``:
    ``matched: false`` at the top level or in the step result."""
    if evt.get("matched") is False:
        return True
    res = evt.get("result")
    return isinstance(res, dict) and res.get("matched") is False


#: Shown when ``llm.complete`` lacks the ``stub`` field: the agents emit it
#: from agents/base/llm.py ``llm_meta``; an older image cannot prove a real call.
PREDATES_STUB_FIELD = ("llm.complete carries no 'stub' field: the deployed playground "
                       "predates it (agents/base/llm.py llm_meta), so a real LLM call "
                       "cannot be told from the stub. Redeploy, then re-run.")


def _real_llm_checks(events: list[dict[str, Any]], capabilities: list[str]
                     ) -> list[tuple[str, bool, dict[str, Any]]]:
    """One check per distinct LLM capability: some ``llm.complete`` for it
    has ``stub: false``. ``llm.started`` / ``llm.complete`` fire on the stub
    too, so their presence alone proves nothing."""
    completes = [e for e in events if e.get("type") == "llm.complete"]
    caps = list(dict.fromkeys(capabilities)) or [None]
    checks: list[tuple[str, bool, dict[str, Any]]] = []
    for cap in caps:
        mine = [e for e in completes if cap is None or e.get("capability") == cap]
        real = [e for e in mine if e.get("stub") is False]
        detail: dict[str, Any] = {
            "capability": cap,
            "llm_complete": [{k: e.get(k) for k in ("agent_id", "capability", "provider",
                                                    "model", "stub", "notes")}
                             for e in mine],
        }
        if not real:
            if completes and any("stub" not in e for e in completes):
                detail["reason"] = PREDATES_STUB_FIELD
            elif not mine:
                detail["reason"] = f"no llm.complete for {cap or 'any capability'}"
            else:
                detail["reason"] = "every llm.complete for it ran on the stub"
        checks.append((f"real llm.complete for {cap or 'some capability'}", bool(real), detail))
    return checks


def classify_outcome(run: dict[str, Any], events: list[dict[str, Any]],
                     exp: Expectation) -> list[tuple[str, bool, dict[str, Any]]]:
    """The matrix assertions for one finished run, as (name, ok, detail)."""
    events = [e for e in events if isinstance(e, dict)]
    types = [e.get("type") for e in events]
    status = run.get("status")
    checks: list[tuple[str, bool, dict[str, Any]]] = [
        ("terminal_status", status in exp.statuses,
         {"status": status, "expected": sorted(exp.statuses), "error": run.get("error")}),
    ]
    if exp.failure_code:
        codes = [e.get("code") for e in events if e.get("type") == "run.failed"]
        checks.append((f"run.failed code = {exp.failure_code}", exp.failure_code in codes,
                       {"run_failed_codes": codes}))
    # A designed rejection that came back with a different status does not
    # fail the run: the runner records it and carries on to success
    # (runner/engine.py probe steps), so the tool must look for it.
    unexpected = [e.get("step_id") for e in events if e.get("type") == "step.unexpected_status"]
    checks.append(("no step.unexpected_status", not unexpected, {"step_ids": unexpected}))
    unmatched = [e.get("step_id") for e in events if _probe_unmatched(e)]
    checks.append(("no probe with matched=false", not unmatched, {"step_ids": unmatched}))
    if exp.llm:
        checks.append(("llm.started present", "llm.started" in types,
                       {"capabilities": exp.llm_capabilities}))
        checks.extend(_real_llm_checks(events, exp.llm_capabilities))
    checks.append(("no llm.quota_exceeded", "llm.quota_exceeded" not in types,
                   {"count": types.count("llm.quota_exceeded")}))
    checks.append(("llm_quota_exceeded false", run.get("llm_quota_exceeded") is not True,
                   {"llm_quota": run.get("llm_quota")}))
    checks.append(("llm_calls_on_mock == 0", int(run.get("llm_calls_on_mock") or 0) == 0,
                   {"llm_calls_on_mock": run.get("llm_calls_on_mock")}))
    return checks


def classify_quota_outcome(run: dict[str, Any],
                           events: list[dict[str, Any]]) -> list[tuple[str, bool, dict[str, Any]]]:
    """The ``quota`` mode assertions: the run fell back to the mock, emitted
    exactly one ``llm.quota_exceeded`` with ``resets_at``, still completed,
    and carries the fields the console badge keys on (``lib/llm-quota.ts``
    ``runLlmQuota``: ``llm_quota_exceeded === true`` on the run)."""
    qe = [e for e in events if isinstance(e, dict) and e.get("type") == "llm.quota_exceeded"]
    return [
        ("run completed", run.get("status") == "success",
         {"status": run.get("status"), "error": run.get("error")}),
        ("llm_quota_exceeded true", run.get("llm_quota_exceeded") is True,
         {"llm_quota": run.get("llm_quota")}),
        ("llm_calls_on_mock > 0", int(run.get("llm_calls_on_mock") or 0) > 0,
         {"llm_calls_on_mock": run.get("llm_calls_on_mock")}),
        ("single llm.quota_exceeded", len(qe) == 1, {"count": len(qe)}),
        ("llm.quota_exceeded has resets_at", bool(qe) and bool(qe[0].get("resets_at")),
         {"event": qe[0] if qe else None}),
    ]


def badge_text(run: dict[str, Any]) -> Optional[str]:
    """What the console badge would say (``lib/llm-quota.ts``
    ``quotaBadgeText``), with ``resets_at`` left in RFC 3339 rather than the
    viewer's locale. None when the run would show no badge."""
    if run.get("llm_quota_exceeded") is not True:
        return None
    q = run.get("llm_quota") or {}
    reason = q.get("reason")
    if q.get("resets_at"):
        why = f"resets {q['resets_at']}"
    elif reason == "quota_store_unavailable":
        why = "quota store unavailable"
    elif reason == "quota_check_failed":
        why = "quota check failed"
    else:
        why = "reset time not reported"
    n = int(run.get("llm_calls_on_mock") or 0)
    calls = f" · {n} {'call' if n == 1 else 'calls'} on mock" if n > 0 else ""
    return f"LLM quota reached: mock responses ({why}){calls}"


# ---------------------------------------------------------------------------
# Console client
# ---------------------------------------------------------------------------


def _origin(url: str) -> str:
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise SetupError(f"not an http(s) URL: {url!r}")
    return f"{p.scheme}://{p.netloc}"


@dataclass
class FollowResult:
    run_id: str
    run: dict[str, Any]
    events: list[dict[str, Any]]
    sse_events: int
    sse_complete: bool
    polled: bool
    sse_error: Optional[str] = None


class ConsoleClient:
    """The console's browser-facing API over one cookie-carrying httpx client."""

    def __init__(self, base_url: str, *, transport: Optional[httpx.BaseTransport] = None,
                 timeout: float = 30.0, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.base = _origin(base_url)
        self.origin = self.base
        self.sleep = sleep
        self.clock = clock
        self.http = httpx.Client(
            base_url=self.base, transport=transport, follow_redirects=False,
            timeout=httpx.Timeout(timeout, read=timeout),
            headers={"User-Agent": "aitp-console-e2e/1", "Accept": "application/json"},
        )

    def close(self) -> None:
        self.http.close()

    # -- auth ---------------------------------------------------------------

    def session(self) -> tuple[int, Any]:
        r = self.http.get("/api/auth/session")
        return r.status_code, _json_or_none(r)

    def login(self, password: str) -> int:
        """``POST /login`` with JSON ``{password}`` (app/login/route.ts). The
        edge checks ``Origin`` on every mutation, ``/login`` included."""
        r = self.http.post("/login", json={"password": password},
                           headers={"Origin": self.origin})
        return r.status_code

    def session_cookie(self) -> Optional[str]:
        return self.http.cookies.get(SESSION_COOKIE)

    # -- playground through the console ---------------------------------------

    def get(self, path: str) -> httpx.Response:
        return self.http.get(path)

    def scenarios(self) -> list[dict[str, Any]]:
        r = self.http.get("/api/playground/scenarios")
        r.raise_for_status()
        body = r.json()
        return list(body.get("scenarios") or [])

    def scenario(self, ref: str) -> dict[str, Any]:
        r = self.http.get("/api/playground/scenarios/" + quote(ref, safe="/@"))
        r.raise_for_status()
        return r.json()

    def launch(self, ref: str, inputs: dict[str, Any], label: Optional[str] = None
               ) -> httpx.Response:
        body: dict[str, Any] = {"scenario_ref": ref, "inputs": inputs}
        if label:
            body["run_label"] = label
        return self.http.post("/api/playground/runs", json=body,
                              headers={"Origin": self.origin})

    def run(self, run_id: str) -> dict[str, Any]:
        r = self.http.get(f"/api/playground/runs/{quote(run_id, safe='')}")
        r.raise_for_status()
        return r.json()

    def status(self, run_id: str) -> dict[str, Any]:
        r = self.http.get(f"/api/playground/runs/{quote(run_id, safe='')}/status")
        r.raise_for_status()
        return r.json()

    def stream(self, run_id: str, on_event: Optional[Callable[[dict[str, Any]], None]] = None,
               deadline: Optional[float] = None) -> tuple[list[dict[str, Any]], bool, Optional[str]]:
        """Read ``/events`` to ``stream.end``. Returns (events, complete, error);
        ``complete`` is False when the stream ended or broke before
        ``stream.end`` (a Vercel cut, a 429 from the stream gate, a timeout)."""
        events: list[dict[str, Any]] = []
        path = f"/api/playground/runs/{quote(run_id, safe='')}/events"
        try:
            with self.http.stream("GET", path, headers={"Accept": "text/event-stream"}) as r:
                if r.status_code != 200:
                    r.read()
                    return events, False, f"HTTP {r.status_code}: {r.text[:200]}"
                for evt in parse_sse_lines(r.iter_lines()):
                    if evt.get("type") == "stream.end":
                        return events, True, None
                    events.append(evt)
                    if on_event:
                        on_event(evt)
                    if deadline is not None and self.clock() > deadline:
                        return events, False, "run timeout while streaming"
        except httpx.HTTPError as exc:
            return events, False, f"{type(exc).__name__}: {exc}"
        return events, False, "stream ended without stream.end"

    def poll_until_terminal(self, run_id: str, *, interval: float, deadline: float
                            ) -> Optional[str]:
        """Poll ``/status`` until terminal or ``deadline``; returns the last
        status seen (None if none). A 429 sleeps its Retry-After, capped at
        the time left, and never outlives the deadline."""
        st: Optional[str] = None
        while True:
            try:
                st = self.status(run_id).get("status")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    left = deadline - self.clock()
                    if left <= 0:
                        return st
                    rl = classify_429(exc.response.headers, _json_or_none(exc.response))
                    self.sleep(min(left, max(interval, rl.retry_after or interval)))
                    continue
                raise
            if st in TERMINAL_STATUSES:
                return st
            if self.clock() > deadline:
                return st
            self.sleep(interval)

    def follow(self, run_id: str, *, run_timeout: float, poll_interval: float,
               on_event: Optional[Callable[[dict[str, Any]], None]] = None) -> FollowResult:
        """SSE to the end; on a cut, poll ``/status`` until terminal. Either
        way the authoritative run (and its full event list) is ``GET
        /runs/{id}``; the SSE frames are merged in only if that list is empty."""
        deadline = self.clock() + run_timeout
        sse, complete, err = self.stream(run_id, on_event=on_event, deadline=deadline)
        polled = False
        if not complete:
            polled = True
            self.poll_until_terminal(run_id, interval=poll_interval, deadline=deadline)
        run = self.run(run_id)
        events = list(run.get("events") or []) or sse
        return FollowResult(run_id, run, events, len(sse), complete, polled, err)


# ---------------------------------------------------------------------------
# Password
# ---------------------------------------------------------------------------


def read_password(env: Optional[dict[str, str]] = None,
                  runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str:
    """``CONSOLE_PASSWORD`` from env, else from Doppler. Never printed."""
    env = os.environ if env is None else env
    pw = env.get("CONSOLE_PASSWORD", "")
    if pw:
        return pw
    try:
        cp = runner(DOPPLER_CMD, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError(f"CONSOLE_PASSWORD unset and doppler failed: {type(exc).__name__}")
    pw = (cp.stdout or "").strip()
    if cp.returncode != 0 or not pw:
        # stderr may name the project or config; it never carries the value.
        raise SetupError(f"CONSOLE_PASSWORD unset and doppler exited {cp.returncode}")
    return pw


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def _probe(ev: Evidence, http: httpx.Client, method: str, url: str, expect: set[int],
           name: str, assert_it: bool = True, **kw: Any) -> Optional[int]:
    try:
        r = http.request(method, url, **kw)
        status: Optional[int] = r.status_code
        body = _json_or_none(r)
    except httpx.HTTPError as exc:
        status, body = None, {"transport_error": f"{type(exc).__name__}: {exc}"}
    if assert_it:
        ev.check(name, status in expect, method=method, url=url, status=status,
                 expected=sorted(expect), body=body)
    else:
        ev.record("report", name=name, method=method, url=url, status=status, body=body)
        print(f"  [INFO] {name} {method} {url} -> {status}")
    return status


def mode_lockdown(args: argparse.Namespace, ev: Evidence,
                  transport: Optional[httpx.BaseTransport] = None) -> None:
    """Keyless probes. Nothing here carries a key or launches a run: a
    keyless ``POST /runs`` is refused by the API-key gate before its body is
    read."""
    pg = args.playground_url.rstrip("/")
    with httpx.Client(transport=transport, timeout=args.timeout, follow_redirects=False,
                      headers={"User-Agent": "aitp-console-e2e/1"}) as http:
        print(f"playground {pg}")
        _probe(ev, http, "GET", f"{pg}/healthz", {200}, "playground /healthz open")
        for path in ("/runs", "/scenarios", "/metrics"):
            _probe(ev, http, "GET", f"{pg}{path}", {401}, f"playground GET {path} keyless 401")
        _probe(ev, http, "POST", f"{pg}/runs", {401}, "playground POST /runs keyless 401",
               json={"scenario_ref": "intra-org/spki-pinning@1.0.0", "inputs": {}})
        if args.cp_url:
            cp = args.cp_url.rstrip("/")
            print(f"control plane {cp}")
            _probe(ev, http, "GET", f"{cp}/api/health", {200}, "cp /api/health", assert_it=False)
            for path in ("/api/events/history", "/api/metrics"):
                _probe(ev, http, "GET", f"{cp}{path}", {401, 403}, f"cp GET {path} keyless 401/403")
            # Public by design (control-plane src/proxy.ts PUBLIC_PATHS): reported only.
            _probe(ev, http, "GET", f"{cp}/api/registry/agents", {200},
                   "cp /api/registry/agents (public by design)", assert_it=False)
        else:
            ev.record("report", name="cp probes skipped", reason="no --cp-url / CP_URL")
            print("  [INFO] control-plane probes skipped (no CP URL)")
        if args.console_url:
            con = args.console_url.rstrip("/")
            print(f"console {con} (anonymous)")
            r = None
            try:
                r = http.get(f"{con}/api/auth/session")
            except httpx.HTTPError as exc:
                ev.check("console /api/auth/session reachable", False, error=str(exc))
            if r is not None and r.status_code == 401:
                # Public mode off: every console route, the session one
                # included, is 401 to an anonymous caller (src/proxy.ts).
                ev.record("report", name="console not in public mode",
                          note="/api/auth/session 401: public-mode assertions skipped",
                          status=r.status_code)
                print("  [INFO] console not in public mode (session 401); "
                      "public-mode assertions skipped")
                _probe(ev, http, "GET", f"{con}/api/playground/metrics", {401},
                       "console admin /api/playground/metrics refused anonymously")
            elif r is not None:
                body = _json_or_none(r) or {}
                ev.check("console session anonymous", r.status_code == 200
                         and body.get("signedIn") is False, status=r.status_code, body=body)
                admin_expect = {403} if body.get("publicMode") else {401}
                _probe(ev, http, "GET", f"{con}/api/playground/metrics", admin_expect,
                       "console admin /api/playground/metrics refused anonymously")


def _signin(args: argparse.Namespace, client: ConsoleClient, ev: Evidence,
            password_reader: Callable[[], str]) -> None:
    """Sign in (``--auth signed-in``) or confirm anonymity, and check the
    admin route matches. Raises SetupError before any launch when the
    password is missing or sign-in fails."""
    if args.auth == "signed-in":
        password = password_reader()
        ev.add_secret(password)
        status = client.login(password)
        del password
        ev.add_secret(client.session_cookie())
        if status != 200 or not client.session_cookie():
            ev.check("console login", False, status=status)
            raise SetupError(f"console login failed (HTTP {status})")
        ev.check("console login", True, status=status, cookie=SESSION_COOKIE)
    st, body = client.session()
    body = body or {}
    want_signed = args.auth == "signed-in"
    if not ev.check("console /api/auth/session", st == 200 and body.get("signedIn") is want_signed,
                    status=st, body=body, auth=args.auth):
        raise SetupError("console session state does not match --auth")
    if not want_signed and not body.get("publicMode"):
        raise SetupError("console is not in public mode: anonymous launches are impossible")
    r = client.get("/api/playground/metrics")
    expect = 200 if want_signed else 403
    ev.check(f"admin /api/playground/metrics = {expect}", r.status_code == expect,
             status=r.status_code)


def _expectations(client: ConsoleClient, ev: Evidence, args: argparse.Namespace
                  ) -> list[Expectation]:
    try:
        listed = client.scenarios()
    except TRANSIENT_ERRORS as exc:
        ev.check("console scenario list fetched", False, reason=_err(exc))
        return []
    ev.record("scenarios", count=len(listed), refs=[s.get("ref") for s in listed])
    only = set(args.only or [])
    skip = set(args.skip or [])
    out: list[Expectation] = []
    overrides = dict(DESIGNED_OUTCOMES)
    for spec in args.expect or []:
        ref, _, statuses = spec.partition("=")
        if not statuses:
            raise SetupError(f"--expect wants REF=status[,status]: {spec!r}")
        overrides.setdefault(ref, {})
        overrides[ref] = {**overrides[ref], "statuses": set(statuses.split(","))}
    extra = args.inputs_obj or {}
    for s in listed:
        ref = s.get("ref")
        if not isinstance(ref, str) or (only and ref not in only) or ref in skip:
            continue
        try:
            detail = client.scenario(ref)
        except TRANSIENT_ERRORS as exc:
            # One bad fetch fails this ref, not the whole matrix.
            ev.check(f"{ref} scenario detail fetched", False, ref=ref, reason=_err(exc))
            continue
        exp = expectation_for(ref, detail, s, overrides)
        exp.inputs.update(extra)
        out.append(exp)
    if args.limit:
        out = out[: args.limit]
    return out


def mode_list(args: argparse.Namespace, ev: Evidence, client: ConsoleClient,
              password_reader: Callable[[], str]) -> None:
    _signin(args, client, ev, password_reader)
    exps = _expectations(client, ev, args)
    for e in exps:
        ev.record("plan", ref=e.ref, inputs=e.inputs, statuses=sorted(e.statuses), llm=e.llm,
                  llm_capabilities=e.llm_capabilities, reason=e.reason,
                  sdk_available=e.sdk_available)
        print(f"  {e.ref:52s} expect={','.join(sorted(e.statuses)):8s} llm={str(e.llm):5s} "
              f"inputs={json.dumps(e.inputs)}")
    ev.check("scenario list non-empty", bool(exps), count=len(exps))
    print(f"{len(exps)} scenario refs")


def _launch(client: ConsoleClient, ev: Evidence, args: argparse.Namespace, ref: str,
            inputs: dict[str, Any], retry_429: bool = True) -> Optional[str]:
    """POST /runs; on a 429 sleep Retry-After and retry once."""
    for attempt in (1, 2):
        r = client.launch(ref, inputs, label=f"console-e2e {args.mode}")
        body = _json_or_none(r)
        if r.status_code == 202 and isinstance(body, dict) and body.get("run_id"):
            ev.record("launch", ref=ref, status=r.status_code, run_id=body["run_id"],
                      attempt=attempt)
            return str(body["run_id"])
        if r.status_code == 429:
            rl = classify_429(r.headers, body)
            ev.record("launch_429", ref=ref, attempt=attempt, layer=rl.layer, code=rl.code,
                      limit=rl.limit, retry_after=rl.retry_after,
                      retry_after_header=rl.retry_after_header, body=body)
            wait = rl.retry_after if rl.retry_after is not None else args.pace
            if retry_429 and attempt == 1 and wait <= args.max_wait:
                print(f"  429 from {rl.layer}; sleeping {wait:.0f}s then retrying once")
                client.sleep(wait + 1)
                continue
        ev.check(f"launch {ref}", False, status=r.status_code, body=body, attempt=attempt)
        return None
    return None


def mode_matrix(args: argparse.Namespace, ev: Evidence, client: ConsoleClient,
                password_reader: Callable[[], str]) -> None:
    _signin(args, client, ev, password_reader)
    exps = _expectations(client, ev, args)
    ev.check("scenario list non-empty", bool(exps), count=len(exps))
    last_launch: Optional[float] = None
    for i, exp in enumerate(exps, 1):
        if last_launch is not None:
            wait = args.pace - (client.clock() - last_launch)
            if wait > 0:
                print(f"  pacing {wait:.0f}s")
                client.sleep(wait)
        print(f"[{i}/{len(exps)}] {exp.ref}")
        last_launch = client.clock()
        run_id: Optional[str] = None
        try:
            run_id = _launch(client, ev, args, exp.ref, exp.inputs)
            if run_id is None:
                continue
            res = client.follow(run_id, run_timeout=args.run_timeout,
                                poll_interval=args.poll_interval)
        except TRANSIENT_ERRORS as exc:
            # A transport error or a 5xx on this ref fails it; the matrix goes on.
            ev.check(f"{exp.ref} run followed", False, ref=exp.ref, run_id=run_id,
                     reason=_err(exc))
            continue
        ev.record("run", ref=exp.ref, run_id=run_id, status=res.run.get("status"),
                  error=res.run.get("error"), sse_events=res.sse_events,
                  sse_complete=res.sse_complete, polled=res.polled, sse_error=res.sse_error,
                  event_types=sorted({str(e.get("type")) for e in res.events}),
                  llm_quota_exceeded=res.run.get("llm_quota_exceeded"),
                  llm_calls_on_mock=res.run.get("llm_calls_on_mock"),
                  expected=sorted(exp.statuses), expect_llm=exp.llm, reason=exp.reason)
        for name, ok, detail in classify_outcome(res.run, res.events, exp):
            ev.check(f"{exp.ref} {name}", ok, run_id=run_id, **detail)


def mode_ratelimit(args: argparse.Namespace, ev: Evidence, client: ConsoleClient,
                   password_reader: Callable[[], str]) -> None:
    _signin(args, client, ev, password_reader)
    ref = args.ref or DEFAULT_RATELIMIT_REF
    inputs = derive_inputs(((client.scenario(ref).get("spec") or {}).get("inputs") or {})
                           .get("schema"))
    running: list[str] = []
    hit: Optional[RateLimit] = None
    for n in range(1, args.max_launches + 1):
        while len(running) >= 2:
            still = []
            for rid in running:
                try:
                    if client.status(rid).get("status") not in TERMINAL_STATUSES:
                        still.append(rid)
                except httpx.HTTPError:
                    still.append(rid)
            running = still
            if len(running) >= 2:
                client.sleep(args.poll_interval)
        r = client.launch(ref, inputs, label="console-e2e ratelimit")
        body = _json_or_none(r)
        if r.status_code == 202 and isinstance(body, dict) and body.get("run_id"):
            running.append(str(body["run_id"]))
            ev.record("launch", n=n, ref=ref, run_id=body["run_id"])
            print(f"  launch {n}: 202 {body['run_id']}")
            continue
        if r.status_code == 429:
            hit = classify_429(r.headers, body)
            ev.record("launch_429", n=n, layer=hit.layer, code=hit.code, limit=hit.limit,
                      retry_after=hit.retry_after, retry_after_header=hit.retry_after_header,
                      detail=hit.detail, body=body)
            print(f"  launch {n}: 429 layer={hit.layer} Retry-After={hit.retry_after_header}")
            break
        ev.check(f"launch {n}", False, status=r.status_code, body=body)
        break
    if not ev.check("got a 429", hit is not None, launches=args.max_launches):
        return
    assert hit is not None
    ev.check("429 has Retry-After header", hit.retry_after_header is not None,
             retry_after=hit.retry_after_header)
    ev.check("429 layer classified", hit.layer != "unknown", layer=hit.layer, code=hit.code)
    if args.expect_layer:
        ev.check("429 layer matches --expect-layer", hit.layer == args.expect_layer,
                 layer=hit.layer, expected=args.expect_layer)
    for rid in running:
        st = client.poll_until_terminal(rid, interval=args.poll_interval,
                                        deadline=client.clock() + args.run_timeout)
        ev.record("drain", run_id=rid, status=st)


def mode_quota(args: argparse.Namespace, ev: Evidence, client: ConsoleClient,
               password_reader: Callable[[], str]) -> None:
    _signin(args, client, ev, password_reader)
    ref = args.ref or DEFAULT_QUOTA_REF
    detail = client.scenario(ref)
    inputs = derive_inputs(((detail.get("spec") or {}).get("inputs") or {}).get("schema"))
    inputs.update(args.inputs_obj or {})
    run_id = _launch(client, ev, args, ref, inputs)
    if run_id is None:
        return
    res = client.follow(run_id, run_timeout=args.run_timeout, poll_interval=args.poll_interval)
    ev.record("run", ref=ref, run_id=run_id, status=res.run.get("status"),
              sse_complete=res.sse_complete, polled=res.polled,
              llm_quota_exceeded=res.run.get("llm_quota_exceeded"),
              llm_quota=res.run.get("llm_quota"),
              llm_calls_on_mock=res.run.get("llm_calls_on_mock"),
              badge_text=badge_text(res.run))
    for name, ok, d in classify_quota_outcome(res.run, res.events):
        ev.check(name, ok, run_id=run_id, **d)
    # The badge's source: the console's own /api/playground/runs/{id} JSON.
    console_run = client.run(run_id)
    ev.check("console run JSON flags the badge",
             console_run.get("llm_quota_exceeded") is True
             and int(console_run.get("llm_calls_on_mock") or 0) > 0,
             run_id=run_id, badge_text=badge_text(console_run))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--console-url", default=os.environ.get("CONSOLE_URL", DEFAULT_CONSOLE_URL),
                        help=f"console base URL (env CONSOLE_URL; default {DEFAULT_CONSOLE_URL})")
    common.add_argument("--playground-url",
                        default=os.environ.get("PLAYGROUND_URL", DEFAULT_PLAYGROUND_URL),
                        help=f"playground URL, lockdown only (env PLAYGROUND_URL; default "
                             f"{DEFAULT_PLAYGROUND_URL})")
    common.add_argument("--cp-url", default=os.environ.get("CP_URL", DEFAULT_CP_URL),
                        help=f"control-plane URL, lockdown only; '' skips its probes "
                             f"(env CP_URL; default {DEFAULT_CP_URL})")
    common.add_argument("--out", type=Path, default=None,
                        help="JSONL evidence file (default $TMPDIR/aitp-console-e2e/"
                             "<mode>-<utc>.jsonl; refused inside the repo)")
    common.add_argument("--timeout", type=float, default=30.0, help="per-request timeout, s")

    launch = argparse.ArgumentParser(add_help=False)
    launch.add_argument("--auth", choices=("anon", "signed-in"), default="anon",
                        help="anon: no session (needs console public mode). signed-in: POST "
                             "/login with CONSOLE_PASSWORD (env, else doppler "
                             "aitp-ui-console/prd)")
    launch.add_argument("--pace", type=float, default=DEFAULT_PACE_S,
                        help=f"seconds between launches (default {DEFAULT_PACE_S:g})")
    launch.add_argument("--max-wait", type=float, default=DEFAULT_MAX_WAIT_S,
                        help="longest Retry-After to sleep through before failing, s")
    launch.add_argument("--run-timeout", type=float, default=DEFAULT_RUN_TIMEOUT_S,
                        help="give up on a run after this many seconds")
    launch.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_S,
                        help="status poll interval after an SSE cut, s")
    launch.add_argument("--inputs", default=None,
                        help="JSON merged over the inputs derived from each pack's schema")

    select = argparse.ArgumentParser(add_help=False)
    select.add_argument("--only", action="append", metavar="REF", help="run only REF (repeatable)")
    select.add_argument("--skip", action="append", metavar="REF", help="skip REF (repeatable)")
    select.add_argument("--limit", type=int, default=0, help="first N refs only")
    select.add_argument("--expect", action="append", metavar="REF=STATUS[,STATUS]",
                        help="override a ref's designed terminal status(es)")

    p = argparse.ArgumentParser(
        prog="console_e2e.py",
        description=__doc__.split("\n\n")[0],
        epilog="Modes in full: see the module docstring and internal_docs/testing.md "
               "('Railway E2E through the console'). Exit 0 = all assertions held, "
               "1 = an assertion failed, 2 = setup error.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="mode", required=True, metavar="MODE")
    sub.add_parser(
        "lockdown", parents=[common],
        help="keyless probes: playground /runs,/scenarios,/metrics,POST /runs -> 401, "
             "/healthz -> 200; CP /api/events/history,/api/metrics -> 401/403; console "
             "anonymous session + admin route refused. Read-only.")
    sub.add_parser(
        "list", parents=[common, launch, select],
        help="list live scenario refs with derived inputs and designed outcome. No launches.")
    # No set_defaults(pace=...) on any subparser: the --pace action is shared through the `launch`
    # parent, so a per-subparser default would rewrite it for matrix/quota too.
    sub.add_parser(
        "matrix", parents=[common, launch, select],
        help="launch every live ref through the console (paced), follow SSE (poll on a cut), "
             "assert terminal status, no step.unexpected_status / matched=false, a real "
             "(stub=false) llm.complete per LLM capability, no "
             "llm.quota_exceeded, llm_calls_on_mock == 0. Costs LLM money.")
    rp = sub.add_parser(
        "ratelimit", parents=[common, launch],
        help="cheap sequential launches (<= 2 running) until a 429; assert Retry-After and "
             "classify the layer (console / playground per-IP / concurrency / hourly).")
    rp.add_argument("--ref", default=None, help=f"ref to launch (default {DEFAULT_RATELIMIT_REF})")
    rp.add_argument("--max-launches", type=int, default=12, help="stop after N launches")
    rp.add_argument("--expect-layer", default=None,
                    choices=("console", "playground-per-ip", "playground-concurrency",
                             "playground-hourly", "playground-token-bucket"),
                    help="also assert the 429 came from this layer")
    qp = sub.add_parser(
        "quota", parents=[common, launch],
        help="one launch with the LLM quota exhausted; assert llm_quota_exceeded, a single "
             "llm.quota_exceeded with resets_at, success, and the console run JSON badge "
             "fields.")
    qp.add_argument("--ref", default=None, help=f"ref to launch (default {DEFAULT_QUOTA_REF})")
    return p


def main(argv: Optional[list[str]] = None, *,
         transport: Optional[httpx.BaseTransport] = None,
         sleep: Callable[[float], None] = time.sleep,
         password_reader: Callable[[], str] = read_password) -> int:
    args = build_parser().parse_args(argv)
    ev: Optional[Evidence] = None
    out: Optional[Path] = None
    try:
        for name in ("console_url", "playground_url"):
            _origin(getattr(args, name))
        if args.cp_url:
            _origin(args.cp_url)
        args.inputs_obj = parse_inputs(getattr(args, "inputs", None))
        out = safe_out_path(args.out or default_out_path(args.mode))
        ev = Evidence(out)
        ev.record("start", mode=args.mode, console_url=args.console_url,
                  playground_url=args.playground_url, cp_url=args.cp_url,
                  auth=getattr(args, "auth", None), pace=getattr(args, "pace", None))
        print(f"evidence: {out}")
        if args.mode == "lockdown":
            mode_lockdown(args, ev, transport=transport)
        else:
            client = ConsoleClient(args.console_url, transport=transport, timeout=args.timeout,
                                   sleep=sleep)
            try:
                {"list": mode_list, "matrix": mode_matrix, "ratelimit": mode_ratelimit,
                 "quota": mode_quota}[args.mode](args, ev, client, password_reader)
            finally:
                client.close()
    except SetupError as exc:
        print(f"setup error: {exc}", file=sys.stderr)
        if ev is not None:
            _end(ev, out, setup_error=str(exc))
        return 2
    except TRANSIENT_ERRORS as exc:
        if ev is None:
            raise
        # Outside matrix's per-ref handling (ratelimit, quota, sign-in):
        # a failed assertion, not a traceback.
        ev.check("no unexpected transport/HTTP error", False, reason=_err(exc))
    except BaseException as exc:
        # Ctrl-C mid-matrix or an unexpected bug: still close the evidence file, then re-raise.
        if ev is not None:
            _end(ev, out, aborted=type(exc).__name__)
        raise
    assert ev is not None
    _end(ev, out)
    return 1 if ev.failures else 0


def _end(ev: Evidence, out: Optional[Path], **extra: Any) -> None:
    """The closing ``end`` record, written on every exit path once the evidence file exists."""
    n_assert = sum(1 for r in ev.records if r["kind"] == "assert")
    ev.record("end", assertions=n_assert, failures=len(ev.failures), **extra)
    print(f"{n_assert - len(ev.failures)}/{n_assert} assertions passed; evidence: {out}")


def parse_inputs(raw: Optional[str]) -> dict[str, Any]:
    """``--inputs``: a JSON object, or SetupError (exit 2) before any request."""
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SetupError(f"--inputs is not valid JSON: {exc.msg} (pos {exc.pos})") from None
    if not isinstance(val, dict):
        raise SetupError("--inputs must be a JSON object")
    return val


if __name__ == "__main__":
    sys.exit(main())
