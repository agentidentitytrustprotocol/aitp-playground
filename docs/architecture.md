# Architecture

## What this service is

`aitp-playground` is a FastAPI application that orchestrates demonstrations
of the **Agent Identity & Trust Protocol** (AITP). A scenario is a YAML
file that declares a set of agents and a workflow that runs against them.
When a scenario is started, the playground:

1. Spawns each agent as its own Python subprocess on a dedicated port.
2. Waits for each subprocess to signal ready.
3. Resolves how peers find each other (static, did:web, or Control Plane
   discovery).
4. Drives AITP handshakes between agent pairs via each agent's `/admin`
   API.
5. Executes the scenario's workflow steps — capability calls, probes,
   delegation, revocation — and records every event.

The runner itself contains no AITP protocol code. All protocol logic
(keygen, manifests, handshake, TCT verify, delegation, revocation) is
in the `aitp` Python SDK, which the agent workers import directly. The
playground is purely the harness around them.

## Runtime topology

```
                 ┌──────────────────────────────────────┐
HTTP client ───▶ │ aitp-playground (port 8000)          │
                 │                                      │
                 │  ┌──── API (FastAPI routers) ─────┐  │
                 │  │ /runs   /scenarios   /packs    │  │
                 │  │ /agents /healthz /capabilities │  │
                 │  │ /metrics /dashboard  /cp/*     │  │
                 │  │ /webhooks/cp/{run}   (← CP)    │  │
                 │  │ /internal/telemetry  (← agents)│  │
                 │  └────────────────────────────────┘  │
                 │  ┌──── Runner ────────────────────┐  │
                 │  │ ScenarioRunner.run()           │  │
                 │  │  ├─ RegistryService            │  │
                 │  │  ├─ TrustOrchestrator          │  │
                 │  │  ├─ AgentSupervisor (subproc)  │  │
                 │  │  ├─ BootstrapBuilder           │  │
                 │  │  ├─ AdapterRegistry            │  │
                 │  │  └─ RunStore (events, SSE)     │  │
                 │  └────────────────────────────────┘  │
                 │  ┌──── CpClient (optional) ───────┐  │
                 │  │  best-effort registry + events │  │
                 │  └────────────────────────────────┘  │
                 └──────────────────────────────────────┘
                          │ spawn (subprocess.Popen)
                          ▼
   ┌───────────────────────────────────────────────────────────┐
   │ Agent worker A — port 8100                                │
   │   FastAPI app                                             │
   │     ├─ aitp_server.AitpServer                             │
   │     │   /.well-known/aitp-manifest                        │
   │     │   /.well-known/did.json   (when did_web_host set)   │
   │     │   /aitp/handshake/hello, /aitp/handshake/commit     │
   │     │   /aitp/delegation/redeem                           │
   │     ├─ agent_admin.build_admin_router                     │
   │     │   /admin/initiate-handshake  /admin/invoke          │
   │     │   /admin/self-execute        /admin/delegate        │
   │     │   /admin/redeem-delegation   /admin/revoke-tct      │
   │     │   /admin/rotate-keys         /admin/renew-tct       │
   │     │   /admin/process-renewal     /admin/enroll-with-cp  │
   │     │   /admin/export-session-bundle  …/verify-…          │
   │     └─ /capabilities/<name>   (per-agent worker)          │
   │                                                           │
   │   aitp.AitpAgent — identity, handshake, TCT, delegation   │
   └───────────────────────────────────────────────────────────┘

   ┌───────────────────────────────────────────────────────────┐
   │ Agent worker B — port 8101 ... same layout                │
   └───────────────────────────────────────────────────────────┘
```

Every blob inside an agent worker is the SDK's responsibility or thin
HTTP plumbing. The "knows about AITP" line lives at the
`aitp_server.AitpServer` / `agent_admin` boundary; everything outside it
just speaks HTTP.

## Components

### Registry (`src/aitp_playground/registry/`)
Walks `scenarios_dir` on startup, validates with Pydantic, and serves
scenario + manifest lookups. `_shared/agents/*.yaml` are loaded once;
scenario packs are discovered as `<pack>/<scenario>/<version>/scenario.yaml`.
A `!include` YAML tag is supported via `include_resolver.py` for splitting
large scenarios. The registry is read-only at runtime; set
`REGISTRY_CACHE_TTL_MS=0` to reload on every lookup (the default).

### Hosting (`src/aitp_playground/hosting/`)
- `port_allocator.py` — hands out unique ports starting from
  `AGENT_BASE_PORT` (default 8100). Honors `port_offset` on each agent
  spec; falls back to monotonic allocation on collision.
- `identity.py` — derives a deterministic 32-byte seed from
  `(org, run_id, agent_id)`. Same scenario → same AIDs across runs.
  Cross-org scenarios mark agents `org: external` so their AIDs land in
  a separate namespace.
- `bootstrap.py` — writes a per-agent JSON file containing seed, port,
  peer placeholders, telemetry URL, and scenario inputs. The agent
  process reads `AITP_BOOTSTRAP_FILE` to find it.
- `adapters/` — `PythonAgentAdapter` is the only adapter today; it's
  registered once per framework (`crewai`, `langchain`, `langgraph`,
  `custom`). Adapter responsibilities: validate the manifest, build the
  process env, and produce a `PreparedLaunch`.
- `supervisor.py` — `subprocess.Popen` per agent; reads stdout until the
  worker emits `AITP_AGENT_READY aid=... port=...`, then backgrounds
  stdout/stderr draining. Kills on cleanup or `/runs/{id}/cancel`.

### Runner (`src/aitp_playground/runner/`)
- `engine.py` — `ScenarioRunner.run()` is the single entry point. It
  loads the scenario, validates inputs against the inline JSON Schema,
  spawns agents, resolves peers, optionally runs eager pairwise
  handshakes, then walks `workflow.steps`. See [runner.md](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/internal_docs/runner.md).
- `context.py` — `RunContext` accumulates `RunEvent`s; every emit also
  fans out to `RunStore` so SSE subscribers see it live.
- `store.py` — in-memory pub/sub with per-run event queues. The SSE
  endpoint (`GET /runs/{id}/events`) replays the backlog then streams
  live events with 1s heartbeats. When `RUN_HISTORY_DB=<path>` is set,
  the store is a `SqliteRunStore` that mirrors every write to that
  SQLite file and rehydrates the in-memory cache from it on startup
  — runs survive a process restart. Empty (the default) is in-memory
  only.
- `result.py` — `RunResult` returned to background-task callers.

### Observability (`src/aitp_playground/observability/`)
Every `RunContext.emit` also feeds `metrics.record_event` and is
renderable by `narrator`. `metrics.py` is a tiny thread-safe Prometheus
registry behind `GET /metrics`; `narrator.py` is a pure event→text
renderer behind `GET /runs/{id}/narrate` and the CLI `trace`. The
single-file trust console at `GET /dashboard` consumes these plus the JSON
APIs. See [observability.md](observability.md).

### Trust (`src/aitp_playground/trust/`)
`TrustOrchestrator.resolve_peers()` returns `{agent_id: {manifest_url, did}}`
keyed by the scenario's `discovery` mode:
- `static` — peers find each other at `http://localhost:<port>`.
- `did_web` — each peer's `did:web:<host>` resolves through
  `/.well-known/did.json` to find the manifest URL. The agent serves its
  own DID doc from its `AitpServer` when `did_web_host` is set.
- `cp_registry` — query the optional Control Plane for agents matching
  a capability; on any failure, fall back to static localhost.

The orchestrator only chooses **where** to look. The handshake itself
is initiated by the runner POSTing `/admin/initiate-handshake` to the
agent, which uses the SDK.

`oidc_issuer.py` mints a per-run mock OIDC issuer (Ed25519 keypair) when a
scenario contains an `identity_type: oidc` agent. The issuer's private seed
and public JWK ride along in each agent's bootstrap so OIDC agents can mint
ID tokens and every agent can verify OIDC peers. Real deployments would
point at an external IdP instead — see
[aitp-integration.md](aitp-integration.md#post-v01-experimental-surfaces).

### Control Plane client (`src/aitp_playground/cp_client/`)
`CpClient` is fully optional. When `CP_BASE_URL` is empty:
- `discover_by_capability()` returns `[]`.
- `ingest_events()` is a no-op.
On any HTTP error it logs and degrades. No CP call is ever load-bearing.

### Agent workers (`agents/`)
Each agent is a self-contained FastAPI app launched as a subprocess.
The shared bootstrap files in `agents/base/` are added to `PYTHONPATH`
by the adapter so workers can `from aitp_server import AitpServer`,
`from agent_admin import build_admin_router`, etc.

Per worker the layout is identical:
1. `load_bootstrap()` reads `AITP_BOOTSTRAP_FILE`.
2. `create_agent()` builds an `aitp.AitpAgent` from the seed.
3. `AitpServer` mounts the AITP protocol routes.
4. `build_admin_router()` mounts the `/admin` routes used by the runner.
5. The worker registers its `/capabilities/<name>` handlers and starts
   uvicorn. The lifespan emits `AITP_AGENT_READY` once the port is bound.

See [agents.md](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/internal_docs/agents.md) for how to add a new worker.

## Data flow for one capability call

```
runner
  └─ POST /admin/invoke (on caller)
        └─ caller looks up held TCT for the target's port
              POST /capabilities/<name> (on target)
                 headers: X-AITP-TCT: <held tct envelope>
                 └─ target.AitpServer.verify_capability_tct(...)
                       └─ aitp.verify_tct(...)  ✓ or 403
                 └─ capability handler runs
                       └─ result returned as JSON
        └─ /admin/invoke wraps non-2xx into {error:true, status_code, body}
runner
  └─ records step output, emits step.complete (or fails the run)
```

Telemetry events emitted from inside an agent (`handshake.started`,
`handshake.complete`, `llm.started`, etc.) POST to
`/internal/telemetry` on the playground, which appends them to the
run's event log.

## Invariants

- **SDK is the only AITP code.** No envelope signing, no JCS, no handshake
  state machine in this repo. If you need one, fix the SDK.
- **CP is always optional.** Every CP call has a graceful fallback. Scenarios
  with `discovery: cp_registry` work without a CP — they fall back to
  static localhost.
- **Agents call the SDK themselves.** The runner never calls AITP methods
  on behalf of an agent; it pokes them via `/admin` HTTP.
- **Scenarios are read-only at runtime.** The registry is rebuilt from
  disk; nothing in `scenarios/` is written by the service.
- **One process per agent per run.** No in-process sharing; each agent has
  its own identity and `held_tcts`.

## Where to read next

- Want to run it? → [getting-started.md](getting-started.md)
- Want to add a scenario? → [scenarios.md](scenarios.md)
- Want to know how a step actually executes? → [runner.md](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/internal_docs/runner.md)
- Want to understand TCTs and handshake? → [aitp-integration.md](aitp-integration.md)
- Want events / metrics / the dashboard? → [observability.md](observability.md)
- Wiring the Control Plane? → [control-plane.md](control-plane.md)
- Which SDK features are installed? → [capabilities.md](capabilities.md)
