# Agents

An agent worker is a Python subprocess that runs an `aitp.AitpAgent`,
exposes the AITP protocol routes, and registers one or more
capabilities. Workers live under `agents/`; the playground's hosting
layer launches them.

> The `aitp.AitpAgent` methods a worker calls (`from_seed`,
> `build_manifest`, `new_session`/`new_responder`, `verify_tct`,
> `build_delegation`, …) are documented in
> [aitp-rs · sdk-python.md](https://github.com/agentidentitytrustprotocol/aitp-rs/blob/main/docs/sdk-python.md).
> This page covers the **worker scaffolding around them**; for where each
> call sits in the protocol flow see [aitp-integration.md](../docs/aitp-integration.md).

## Repository layout

```
agents/
├── base/                       # Shared code injected via PYTHONPATH at spawn
│   ├── bootstrap.py            # Reads AITP_BOOTSTRAP_FILE, builds AitpAgent
│   ├── aitp_server.py          # Mounts /.well-known/* and /aitp/* routes
│   ├── agent_admin.py          # Builds /admin/* routes called by the runner
│   ├── revocation_state.py     # Local revocations + the CP snapshot, held apart
│   ├── revocation_refresh.py   # The one verifying ingest of the CP's signed list
│   ├── oidc.py                 # Agent-side OIDC helpers for OIDC-typed handshakes
│   ├── tct_claims.py           # Reads TCT/voucher claims for observability only, never trust
│   ├── telemetry.py            # POSTs events to playground /internal/telemetry
│   └── llm.py                  # Shared OpenAI/Anthropic selector
├── researcher/                 # CrewAI worker
│   ├── main.py                 # FastAPI app + capability handlers
│   ├── crew.py                 # build_crew(): real CrewAI or stub
│   └── requirements.txt
├── writer/                     # LangChain worker
│   ├── main.py
│   ├── chain.py                # run_writer(): real LangChain or stub
│   └── requirements.txt
└── analyzer/                   # LangGraph worker
    ├── main.py
    ├── graph.py                # run_analyzer(): real LangGraph or stub
    └── requirements.txt
```

The `requirements.txt` files are reference lists for humans. Workers
inherit packages from the playground's environment — `pyproject.toml`'s
optional extras (`researcher`, `writer`, `analyzer`, `all-agents`) are
the actual install path. Manifests do not carry per-agent install
pointers.

## The spawn contract

1. The runner allocates a port and writes a per-agent bootstrap JSON to
   `tempfile.gettempdir()/aitp-bootstrap/<run_id>_<agent_id>.json`.
2. `PythonAgentAdapter.prepare_launch` builds a `PreparedLaunch` with:
   - `command` from `host.python` (else `AGENT_PYTHON`).
   - `args` from `entrypoint` (`-m <module>` or `<file>`).
   - `env` = parent env + `host.env` + `AITP_BOOTSTRAP_FILE`,
     `AGENT_PORT`, `PYTHONUNBUFFERED=1`. `PYTHONPATH` is extended with
     `agents/base/` (so `from aitp_server import ...` works) and
     `agents/` (so `<package>.main` imports work).
   - `cwd` resolved from the manifest's `host.cwd` (relative to project
     root) or absolute.
3. `AgentSupervisor.launch` runs `subprocess.Popen` and tails stdout
   until it sees `AITP_AGENT_READY aid=... port=...`. That string is
   the spawn-ready signal — emit it from your FastAPI lifespan, not
   before uvicorn binds, or the supervisor will race against the first
   request.
4. After ready, the supervisor backgrounds stdout/stderr draining and
   the runner can call `/admin/*` on the agent.

If the subprocess exits before the ready line, the supervisor captures
stderr and surfaces a `RuntimeError` that bubbles up to the run failure.

## The bootstrap payload

`hosting/bootstrap.py` writes this JSON; `agents/base/bootstrap.py`
reads it.

```json
{
  "run_id": "<uuid>",
  "agent_id": "researcher",
  "port": 8100,
  "aitp": {
    "seed_hex": "<sha256 of org:run_id:agent_id>",
    "display_name": "Research Analyst",
    "handshake_endpoint": "http://localhost:8100/aitp/handshake/hello",
    "offered_caps": ["research.query"],
    "did_web_host": null,
    "ttl_secs": 3600
  },
  "peers": {
    "<other_agent_id>": { "manifest_url": "http://localhost:8101/.well-known/aitp-manifest", "did": null }
  },
  "playground": {
    "telemetry_url": "http://localhost:8000/internal/telemetry",
    "run_id": "<uuid>"
  },
  "inputs": { "topic": "..." }
}
```

`peers` is a **placeholder** map written at spawn time — every
local URL. The runner resolves the real URLs after spawn and uses them
when it triggers the actual handshake; the bootstrap's `peers` is
mostly there so agents can introspect what they're in a scenario with.
Don't drive trust off it.

## Anatomy of a worker

Every worker follows the same structure (see `agents/researcher/main.py`):

```python
import uvicorn
from fastapi import FastAPI, Request

from agent_admin import build_admin_router
from revocation_state import RevocationState
from aitp_server import AitpServer, ready_lifespan
from bootstrap import create_agent, get_manifest_json, load_bootstrap
from telemetry import emit_event

from .crew import build_crew

# 1) Read the bootstrap and construct the AITP identity.
bootstrap = load_bootstrap()
PORT = int(bootstrap["port"])
agent = create_agent(bootstrap)              # aitp.AitpAgent.from_seed(...)
manifest_json = get_manifest_json(agent, bootstrap)

# 2) Shared revocation state: AitpServer (enforcement) and the admin
#    router's /admin/revoke-tct + /admin/refresh-revocations (mutation)
#    reference the same object. It keeps locally-revoked jtis and the
#    CP-derived snapshot APART, unioned only at enforcement — see
#    agents/base/revocation_state.py for why one flat set could not.
_revocation = RevocationState()

# 3) Mount the AITP protocol routes. Built BEFORE the app, so the
#    lifespan below can own the background revocation poll.
server = AitpServer(
    agent=agent,
    manifest_json=manifest_json,
    port=PORT,
    bootstrap=bootstrap,
    did_web_host=bootstrap["aitp"].get("did_web_host"),
    did_web_scheme=bootstrap["aitp"].get("did_web_scheme", "http"),
    revocation=_revocation,
)

# 4) FastAPI app. The lifespan does a blocking first revocation refresh,
#    THEN signals AITP_AGENT_READY, then starts the poll.
app = FastAPI(
    title=f"agent-{bootstrap['agent_id']}",
    lifespan=ready_lifespan(aid=agent.aid, port=PORT, server=server),
)
app.include_router(server.router)

async def do_research(payload, commissioned_by="self"):
    ...  # business logic; emits llm.started / llm.complete events

# 5) Mount the admin routes.
_held_tcts: dict[int, str] = {}              # peer_port -> TCT envelope JSON
app.include_router(build_admin_router(
    agent=agent, bootstrap=bootstrap,
    held_tcts=_held_tcts, revocation=_revocation,
    issued_tcts=server._issued_tcts,
    capabilities={"research.query": do_research},
    manifest_provider=server._fresh_manifest_json,
))

# 6) The capability endpoint. Verify the TCT, then run the same handler.
@app.post("/capabilities/research.query")
async def research_query(request: Request):
    tct_json = request.headers.get("x-aitp-tct", "")
    identity = server.verify_capability_tct(tct_json, "research.query")
    body = await request.body()
    payload = json.loads(body) if body else None
    return await do_research(payload, commissioned_by=identity.peer_aid)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
```

Two important things to notice:

- The capability handler is registered **twice**: once in
  `build_admin_router(capabilities={...})` (so `/admin/self-execute`
  can call it for the self-execute path) and once at
  `/capabilities/<name>` (where peers POST after handshake). Both
  paths share the same async function, which keeps behavior consistent
  whether the call comes from this agent itself or from a peer with a
  TCT.
- `_held_tcts` and `_revocation` are module-level, so every request in
  this process sees the same state. `_held_tcts` is mutated by the admin
  router. `_revocation` is mutated from both sides — `/admin/revoke-tct`
  adds a local revocation, and `/admin/refresh-revocations` plus the
  background poll replace the CP snapshot wholesale — and read by
  `AitpServer` on every capability call. It is deliberately **not** one
  flat set: local revocations survive a snapshot replacement, and a
  snapshot is the issuer's complete current deny-set rather than an
  increment, so the union happens only at enforcement.

## Routes mounted per worker

From `AitpServer`:
- `GET  /.well-known/aitp-manifest` — JSON manifest from
  `aitp.AitpAgent.build_manifest`.
- `GET  /.well-known/did.json` — DID document, only when
  `did_web_host` is set.
- `POST /aitp/handshake/hello` — process the initiator's first message,
  return ack + `X-Aitp-Session-Id`.
- `POST /aitp/handshake/commit` — close the responder side, return ack;
  emits `handshake.complete`.
- `POST /aitp/delegation/redeem` — accept a `DelegationToken`, verify
  it against our AID, mint a fresh TCT bound to the delegatee's key.
- `POST /admin/rotate-keys` — replace this agent's keypair and
  republish its manifest under the new identity. `AitpServer` mounts
  this `/admin/*` route directly (not via `build_admin_router` below) —
  key material lives here, not in the admin router.
- `GET  /admin/tct-cache-stats` — RFC-AITP-0005 verification-cache
  counters (`enabled` is `False` on a wheel without `TctStore`).

From `build_admin_router`:
- `POST /admin/initiate-handshake` — fetch peer manifest, drive a
  full 4-message handshake, store the resulting TCT keyed by peer port.
- `POST /admin/invoke` — POST `/capabilities/<name>` on the peer using
  the held TCT. Wraps non-2xx into `{error:true, status_code, body}`.
- `POST /admin/self-execute` — run a locally-registered capability,
  no peer call, no TCT.
- `POST /admin/delegate` — issue a `DelegationToken` from a held TCT.
- `POST /admin/redeem-delegation` — POST a `DelegationToken` to a peer's
  redeem endpoint, store the returned TCT in `held_tcts`.
- `POST /admin/revoke-tct` — add a jti to this agent's **local**
  revocations. Never cleared by a control-plane refresh.
- `POST /admin/refresh-revocations` — fetch the CP's signed revocation
  snapshot, verify it against the pinned `CP_AID`, and put it into force
  (replacing any previous snapshot). Discarded wholesale if it does not
  verify.
- `GET  /admin/held-tct` — return the TCT this agent currently holds
  from a peer, keyed by peer port.
- `POST /admin/renew-tct` — request a fresh TCT (new jti, same subject
  and grants) from the issuer before the held one expires.
- `POST /admin/export-session-bundle` — RFC-AITP-0010 coordinator side:
  package the TCTs this agent has issued into a `SessionBundleEnvelope`.
- `POST /admin/verify-session-bundle` — RFC-AITP-0010 verifier side:
  verify a `SessionBundleEnvelope` and report active/dropped AIDs.
- `POST /admin/process-renewal` — issuer side of TCT renewal: verify
  the request, mint a fresh `TctEnvelope`.
- `POST /admin/enroll-with-cp` — self-enroll this agent into the
  control plane's registry (two-step: `/enroll` for a token, then
  `/agents` to register).

Per worker:
- `POST /capabilities/<name>` — one per `offered_caps` entry.

## Adding a new capability to an existing agent

Three things:
1. Add the capability name to the manifest's `aitp.offered_caps` (or
   author a new manifest variant — that's what
   `researcher-extended.yaml` is, it adds `research.deep` to the same
   worker).
2. Write the async handler in the worker (or in its `crew.py` /
   `chain.py` / `graph.py` and import it).
3. Register it in `build_admin_router(capabilities={...})` **and** add a
   matching `@app.post("/capabilities/<name>")` route in `main.py` that
   calls `server.verify_capability_tct(tct_json, "<name>")` first.

Both registrations point at the same coroutine — the admin one is for
self-execute, the HTTP one is for peer calls.

## Adding a new agent worker

Goal: a new `summarizer` agent.

1. Create `agents/summarizer/`:
   - `main.py` — copy `agents/writer/main.py` as a template.
   - `summary.py` — the business logic (real LLM via
     `agents.base.llm.build_chat_model`, plus a deterministic stub).
   - `__init__.py`.
   - `requirements.txt` — informational.
2. Add a manifest at `scenarios/_shared/agents/summarizer.yaml`:

   ```yaml
   apiVersion: aitp.dev/v1
   kind: AgentManifest
   metadata:
     id: summarizer
     name: Summarizer
     framework: langchain         # or langgraph / crewai / custom
     version: 1.0.0
   spec:
     entrypoint: { type: python_module, value: summarizer.main }
     host: { python: python3, cwd: agents/summarizer, startupTimeoutMs: 30000 }
     aitp:
       offered_caps: [summarize.text]
       display_name: Summarizer
       identity_type: pinned_key
       ttl_secs: 3600
     did_web: false
   ```
3. Add to `pyproject.toml`'s `all-agents` and (optionally) a
   `summarizer` extra so others can `pip install -e .[summarizer]`.
4. Reference it from a scenario:

   ```yaml
   agents:
     - id: summarizer
       ref: _shared/agents/summarizer
       port_offset: 2
   workflow:
     steps:
       - id: summarize
         agent: summarizer
         capability: summarize.text
         input_from: write
   ```

## Adding a new framework

A new framework only matters at the **manifest** level — the worker is
always a Python subprocess. The adapter is already generic (`PythonAgentAdapter`),
and `build_default_adapter_registry()` registers one per framework name
(`crewai`, `langchain`, `langgraph`, `custom`).

To add a fifth framework:
1. Open `src/aitp_playground/registry/models.py` — add the name to
   `AgentManifestMeta.framework` and to `AgentFramework` in
   `hosting/adapters/base.py`.
2. Register a `PythonAgentAdapter(<name>, strict=True)` in
   `build_default_adapter_registry()`.
3. (Optional) write framework-specific helpers in `agents/<name>/...`.

If the new framework needs a different launch shape (different runtime,
container, etc.), write a new `AgentHostAdapter` subclass — see the
abstract base in `hosting/adapters/base.py`.

## Telemetry

`agents/base/telemetry.emit_event(type, bootstrap, **fields)` POSTs to
`bootstrap["playground"]["telemetry_url"]`. The playground appends it
to the run's event log. Failures are swallowed — telemetry is
best-effort.

Conventions used by current workers:
- `handshake.started` — when a responder accepts a hello.
- `handshake.complete` — when initiator or responder closes the
  4-message exchange.
- `handshake.failed` — bad hello or commit; carries `error`.
- `delegation.issued` / `delegation.rejected` / `delegation.redeemed` —
  RFC-AITP-0006 flow milestones.
- `tct.revoked` — when `/admin/revoke-tct` adds a jti.
- `identity.key.rotated` — when `/admin/rotate-keys` replaces this
  agent's keypair. Carries `old_aid`, `new_aid`.
- `capability.self_execute` — when `/admin/self-execute` runs.
- `llm.started` / `llm.complete` — wraps the LLM call so the run log
  shows when real work happened.
- `manifest.verify_failed` — a fetched peer manifest failed
  `aitp.verify_manifest_json`. Carries `cause`
  (`signature_invalid | expired | malformed | unknown` — from the
  SDK's `.code`, never guessed at from message text) and `source_url`.
- `tct.renewal.requested` / `tct.renewal.issued` — holder requests a
  fresh TCT before the held one expires / issuer mints it. Both carry
  `jti` and enough of the TCT to identify it (`tct_event(...)`);
  `.requested` also carries `peer_port`, `.issued` also carries
  `subject`.
- `session.bundle.exported` — RFC-AITP-0010 coordinator built a
  `SessionBundleEnvelope`. Carries `session_id`, `participant_count`.
- `session.bundle.verified` — RFC-AITP-0010 verifier checked one.
  Carries `kind` (the SDK's `BundleOutcome.kind`) and `active_count`.
- `cp.enroll_succeeded` — `/admin/enroll-with-cp` completed. Carries
  `aid`, `registered_at`.
- `cp.enroll_failed` — either step of `/admin/enroll-with-cp` failed.
  Always carries `stage` (`"enroll"` or `"register"`); a non-2xx CP
  response also carries `status_code` + `body`, a transport failure
  (connection refused, DNS, timeout) carries `transport`, and a 2xx
  response that is not JSON carries `decode`. Never aliases a
  transport failure as a status-code failure or vice versa — the same
  fetch-vs-verify discipline `revocation.*` below applies.
- `revocation.list_fetched` — a snapshot verified and was applied.
  Carries `jti_count`, `added`, `verified`, `issuer`. Suppressed on the
  poll loop's `quiet` calls (routine, not worth an event every tick).
- `revocation.refresh_failed` — the snapshot fetch itself failed
  (transport, not verification). Carries `error`. Also suppressed
  under `quiet` — an ordinary CP outage fires this once per poll tick
  otherwise.
- `revocation.verify_failed` — a fetched snapshot did NOT verify
  (forged, wrong issuer, expired, malformed body, or the SDK/config
  cannot check one at all). Carries `cause` and `detail`. **Never**
  suppressed by `quiet`, unlike the two events above — a discard means
  the CP answered with something that does not verify, which is the
  signal `quiet` exists to preserve, not silence. See `DECISIONS.md`
  D-14. The `verify_failed` / `refresh_failed` split is deliberate:
  collapsing "the CP didn't answer" and "the CP answered with garbage"
  is how a signing-convention break gets triaged as a network blip.
- `revocation.poll` — the background poll loop's own heartbeat, layered
  on top of `verify_failed`/`refresh_failed` above (not a replacement
  for them — `healthy` alone cannot distinguish a down CP from a
  forged snapshot). Carries `healthy`, `changed`, `posture`
  (`unchecked | current | degraded`). Rate-limited to state changes
  plus a low-frequency heartbeat, not emitted on every tick.
- `revocation.degraded_serve` — Axis B served a call in `soft_fail`
  degraded posture. Carries `reason`, `serves` (a running count),
  `fail_mode`. Rate-limited to the first occurrence and every 100th —
  once degraded, every call would otherwise take this path.

`run.cancelled` is emitted by the **runner**, not an agent worker — see
the runner's own event catalog, not this list.

The runner emits a separate, overlapping set (`trust.establishing`,
`step.started`, `step.complete`, etc.). Together they form the
event stream returned by `GET /runs/{id}` and `GET /runs/{id}/events`.
