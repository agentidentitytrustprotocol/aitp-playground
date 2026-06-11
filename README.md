# aitp-playground

Python FastAPI service that runs **Agent Identity & Trust Protocol**
(AITP) scenario demonstrations end-to-end with real LLM-powered agents.
Each scenario spins up a handful of agents that establish cryptographic
identity, complete an AITP handshake, and do real LLM work under
verifiable, scoped, revocable trust — so you can *watch* the protocol
behave (and fail closed) instead of reading about it.

```bash
cp .env.example .env                  # optional: set OPENAI_API_KEY for real LLM output
docker compose up --build             # service on :8000  →  open http://localhost:8000/dashboard
```

How it works:

1. Loads scenario packs from `scenarios/` (`intra-org`, `cross-org`,
   `cross-cloud`) — declarative YAML, no code.
2. Spawns each scenario's agents as their own Python subprocesses
   (CrewAI / LangChain / LangGraph / custom), each on its own port with
   its own identity.
3. Each agent uses the [`aitp-py`](../aitp-rs/bindings/aitp-py) SDK to
   build its identity and run the 4-message AITP handshake with peers.
4. The runner drives capability calls, delegation, revocation, and more
   between agents, and surfaces a live event stream, narration, metrics,
   and a web dashboard.

This is a demo harness, not production. **All AITP protocol logic lives
in `aitp-py`**; this repo contains no envelope signing, JCS, or
handshake state. See [docs/aitp-integration.md](docs/aitp-integration.md)
for exactly where that boundary sits.

## What it demonstrates

One service, ~20 scenarios, each isolating one AITP behavior:

| Area | Scenarios show… |
| --- | --- |
| **Identity** | pinned Ed25519/P-256 keys and OIDC (RFC-AITP-0002) ID-token binding |
| **Handshake & TCTs** | the 4-message mutual handshake; per-call capability authorization |
| **Trust gating** | a call with no/insufficient TCT is rejected (403), then succeeds after handshake; grant intersection |
| **Delegation** | single-hop and multi-hop delegation chains with scope narrowing (RFC-AITP-0006 / 0011) |
| **Revocation** | fail-closed local revocation and propagation through the Control Plane's signed list (RFC-AITP-0008) |
| **Lifecycle** | key rotation (0007), in-band TCT renewal + a verification cache (0005), session bundles (0010), SPKI pinning |
| **Discovery** | static localhost, `did:web`, and Control Plane registry — each with graceful fallback |
| **Control Plane** | optional enrollment, webhooks, trust-anchor provisioning, delegation-tree observability |
| **Resilience** | operator-injected faults (`manifest_404`, `peer_offline`) that the run survives with structured outcomes |

Everything is optional and degrades cleanly: no LLM key → deterministic
stubs (handshakes still run); no Control Plane → static fallback; an SDK
wheel built without `--features experimental` → the advanced scenarios
report "feature not available" instead of crashing. Check
`GET /capabilities` to see what your wheel exposes.

## Documentation

The reader-facing docs are under [`docs/`](docs/) (also published to the
docs site) — start with [architecture.md](docs/architecture.md):

- **[Architecture](docs/architecture.md)** — components, runtime topology,
  where AITP lives.
- **[Getting started](docs/getting-started.md)** — install, env, first
  scenario run, endpoint cheatsheet, CLI.
- **[Scenarios](docs/scenarios.md)** — YAML schema, workflow step types,
  authoring guide.
- **[AITP integration](docs/aitp-integration.md)** — where the SDK is
  called; identity, handshake, TCT, delegation, revocation, and the
  post-v0.1 surfaces (OIDC, renewal, bundles, pinning, multi-hop).
- **[Observability](docs/observability.md)** — SSE events, narration,
  Prometheus metrics, the dashboard, run persistence.
- **[Control plane](docs/control-plane.md)** — the optional CP: discovery,
  enrollment, revocation, webhooks, trust anchors.
- **[Capabilities](docs/capabilities.md)** — which SDK features the
  installed wheel exposes, graceful degradation, conformance harness.

Deeper internals and ops mechanics — for hacking on the repo, not on the
docs site — live under
[`internal_docs/`](https://github.com/agentidentitytrustprotocol/aitp-playground/tree/main/internal_docs):
the runner engine, the agent-worker pattern, LLM providers, Docker, and the
test suite.

Sibling repos — the **source of truth** for everything the playground only
orchestrates. The docs here link out to these rather than restating them:

- [`agentidentitytrustprotocol`](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol)
  — the normative AITP RFCs and registries.
- [`aitp-rs`](https://github.com/agentidentitytrustprotocol/aitp-rs) —
  reference Rust runtime; ships the Python SDK from `bindings/aitp-py/`.
  Start with its [Python SDK guide](https://github.com/agentidentitytrustprotocol/aitp-rs/blob/main/docs/sdk-python.md).
- [`aitp-control-plane`](https://github.com/agentidentitytrustprotocol/aitp-control-plane)
  — the optional Control Plane the playground can talk to; see its
  [API docs](https://github.com/agentidentitytrustprotocol/aitp-control-plane/blob/main/docs/README.md).

## Quick start

Two paths.

### Docker (no host toolchain)

The Dockerfile is multi-stage and builds the `aitp` SDK from the
sibling Rust source for you. The compose files set the build context
to the parent directory so the sibling repo is visible.

```bash
cp .env.example .env
$EDITOR .env                          # set OPENAI_API_KEY=sk-... (optional for stub runs)

# Just run the service:
docker compose up --build

# Or run the full LLM end-to-end test suite (three scenarios, real
# OpenAI, real AITP trust). Exit code of the `tests` container is the
# result.
docker compose -f docker-compose.test.yml up --build --abort-on-container-exit
```

First image build is ~5 minutes on Apple Silicon (Rust cold compile);
subsequent rebuilds are seconds thanks to BuildKit cache mounts.

### Native (requires Rust + maturin once)

```bash
# 1. Build the aitp-py extension into your active venv (one-time).
#    Add `--features experimental` to enable the post-v0.1 surfaces
#    (TCT renewal, session bundles, SPKI pinning, the TCT verification
#    cache, multi-hop delegation verify). Scenarios needing a feature the
#    wheel was built without degrade cleanly — check GET /capabilities to
#    see what the installed wheel exposes.
cd ../aitp-rs/bindings/aitp-py
maturin develop --release --features experimental

# 2. Install the service.
cd ../../../aitp-playground
uv sync                               # or: pip install -e .

# 3. Run.
uv run uvicorn aitp_playground.main:app --reload --port 8000

# 4. Trigger a scenario.
curl -X POST http://localhost:8000/runs \
  -H "Content-Type: application/json" \
  -d '{"scenario_ref":"intra-org/research-and-write@1.0.0",
       "inputs":{"topic":"AI agent trust protocols"}}'

# 5. Watch live events (SSE) or poll:
curl -N http://localhost:8000/runs/<run_id>/events
curl    http://localhost:8000/runs/<run_id> | jq .
```

Agent extras (CrewAI / LangChain / LangGraph + the OpenAI/Anthropic
clients) are optional; without them the agents fall back to
deterministic stubs and AITP handshakes still run end-to-end. Install
when you want real LLM output:

```bash
pip install -e ".[all-agents]"
```

See [docs/getting-started.md](docs/getting-started.md) for the full
env reference and the endpoint cheatsheet.

## Repo map

```
aitp-playground/
├── docs/                  # reader-facing docs (published to the docs site)
├── internal_docs/         # contributor & build docs (not published)
├── src/aitp_playground/   # FastAPI service — no AITP protocol logic here
│   ├── api/               # routes: /runs /scenarios /agents /capabilities /metrics /dashboard /cp/* /webhooks
│   ├── registry/          # YAML pack loader + index + templates
│   ├── runner/            # scenario engine + run store (+ optional SQLite) + SSE
│   ├── hosting/           # subprocess spawn, identity, port alloc, adapters
│   ├── trust/             # peer resolver + did:web + per-run OIDC issuer
│   ├── observability/     # metrics + event narrator
│   ├── cp_client/         # optional Control Plane client
│   ├── capabilities.py    # SDK feature probe (GET /capabilities)
│   └── conformance.py     # RFC fixture catalog + readiness
├── agents/                # agent subprocess workers
│   ├── base/              # shared aitp_server / bootstrap / telemetry / llm
│   ├── researcher/        # CrewAI worker
│   ├── writer/            # LangChain worker
│   └── analyzer/          # LangGraph worker
├── scenarios/             # YAML scenario packs (registry on disk)
└── tests/                 # unit / integration / scenario / e2e
```

## Tests

```bash
# Default unit suite — fast, in-process.
uv run pytest tests/unit/

# Runner integration — spawns real subprocesses, no LLM keys needed.
AITP_E2E=1 uv run pytest tests/integration/test_runner.py -v

# Protocol e2e — delegation/revocation/rotation/etc. under real trust,
# still no LLM keys (best run inside the Docker stack).
AITP_PROTOCOL_E2E=1 uv run pytest tests/integration/test_protocol_e2e.py -v

# Live LLM end-to-end (one-command via Docker, see above).
```

Full details: [internal_docs/testing.md](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/internal_docs/testing.md).

## License

See [LICENSE](LICENSE).
