# Agents

An agent worker is a Python subprocess that runs an `aitp.AitpAgent`,
exposes the AITP protocol routes, and registers one or more
capabilities. Workers live under `agents/`; the playground's hosting
layer launches them.

## Repository layout

```
agents/
├── base/                       # Shared code injected via PYTHONPATH at spawn
│   ├── bootstrap.py            # Reads AITP_BOOTSTRAP_FILE, builds AitpAgent
│   ├── aitp_server.py          # Mounts /.well-known/* and /aitp/* routes
│   ├── agent_admin.py          # Builds /admin/* routes called by the runner
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
from aitp_server import AitpServer, ready_lifespan
from bootstrap import create_agent, get_manifest_json, load_bootstrap
from telemetry import emit_event

from .crew import build_crew

# 1) Read the bootstrap and construct the AITP identity.
bootstrap = load_bootstrap()
PORT = int(bootstrap["port"])
agent = create_agent(bootstrap)              # aitp.AitpAgent.from_seed(...)
manifest_json = get_manifest_json(agent, bootstrap)

# 2) FastAPI app whose lifespan signals AITP_AGENT_READY.
app = FastAPI(
    title=f"agent-{bootstrap['agent_id']}",
    lifespan=ready_lifespan(aid=agent.aid, port=PORT),
)

# 3) Shared revocation set: both AitpServer (verifier) and
#    /admin/revoke-tct (mutator) reference the same set.
_revoked_jtis: set[str] = set()
_held_tcts: dict[int, str] = {}              # peer_port -> TCT envelope JSON

# 4) Mount AITP protocol + admin routes.
server = AitpServer(agent=agent, manifest_json=manifest_json, port=PORT,
                    bootstrap=bootstrap,
                    did_web_host=bootstrap["aitp"].get("did_web_host"),
                    revoked_jtis=_revoked_jtis)
app.include_router(server.router)

async def do_research(payload, commissioned_by="self"):
    ...  # business logic; emits llm.started / llm.complete events

app.include_router(build_admin_router(
    agent=agent, bootstrap=bootstrap,
    held_tcts=_held_tcts, revoked_jtis=_revoked_jtis,
    capabilities={"research.query": do_research},
))

# 5) The capability endpoint. Verify the TCT, then run the same handler.
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
- `_held_tcts` and `_revoked_jtis` are module-level. They're mutated by
  the admin router and read by the AITP server; all requests in this
  process see the same maps.

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
- `POST /admin/revoke-tct` — add a jti to `revoked_jtis`.

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
- `capability.self_execute` — when `/admin/self-execute` runs.
- `llm.started` / `llm.complete` — wraps the LLM call so the run log
  shows when real work happened.

The runner emits a separate, overlapping set (`trust.establishing`,
`step.started`, `step.complete`, etc.). Together they form the
event stream returned by `GET /runs/{id}` and `GET /runs/{id}/events`.
