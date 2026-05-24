# aitp-playground

Python FastAPI service that runs **Agent Identity & Trust Protocol**
(AITP) scenario demonstrations end-to-end with real LLM-powered agents.

What it does:

1. Loads scenario packs from `scenarios/` (`intra-org`, `cross-org`,
   `cross-cloud`).
2. Spawns each scenario's agents as their own Python subprocesses
   (CrewAI / LangChain / LangGraph / custom).
3. Each agent uses the [`aitp-py`](../aitp-rs/bindings/aitp-py) SDK to
   build its identity and run the 4-message AITP handshake with peers.
4. The runner drives capability calls, delegation, and revocation
   between agents and surfaces a live event stream for observability.

This is a demo harness, not production. All AITP protocol logic lives
in `aitp-py`; this repo contains no envelope signing, JCS, or
handshake state. See [docs/aitp-integration.md](docs/aitp-integration.md)
for the boundary.

## Documentation

The full contributor docs are under [`docs/`](docs/):

- **[Architecture](docs/architecture.md)** — components, runtime
  topology, where AITP lives.
- **[Getting started](docs/getting-started.md)** — install, env, first
  scenario run, endpoint cheatsheet.
- **[Scenarios](docs/scenarios.md)** — YAML schema, workflow step
  types, authoring guide.
- **[Agents](docs/agents.md)** — agent worker pattern, adding a
  capability, adding a framework.
- **[AITP integration](docs/aitp-integration.md)** — how the SDK is
  called, identity / handshake / TCT / delegation / revocation.
- **[Runner](docs/runner.md)** — engine internals, step dispatch,
  event stream.
- **[LLM providers](docs/llm-providers.md)** — OpenAI/Anthropic
  selection, stubs, adding a provider.
- **[Docker](docs/docker.md)** — multi-stage Dockerfile, compose
  files, e2e build.
- **[Testing](docs/testing.md)** — unit / integration / scenario / live
  LLM tiers.

Sibling repos referenced throughout:

- [`agentidentitytrustprotocol/`](../agentidentitytrustprotocol/) —
  RFCs / specifications.
- [`aitp-rs/`](../aitp-rs/) — reference Rust runtime; ships the
  Python SDK from `bindings/aitp-py/`.

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
cd ../aitp-rs/bindings/aitp-py
maturin develop --release

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
├── docs/                  # contributor documentation (start here)
├── src/aitp_playground/   # FastAPI service — no AITP protocol logic here
│   ├── api/               # routes: /runs /scenarios /packs /agents /healthz
│   ├── registry/          # YAML pack loader + index
│   ├── runner/            # scenario engine + run store + SSE
│   ├── hosting/           # subprocess spawn, identity, port alloc, adapters
│   ├── trust/             # peer resolver (static / cp_registry / did_web)
│   └── cp_client/         # optional Control Plane client
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

# Live LLM end-to-end (one-command via Docker, see above).
```

Full details: [docs/testing.md](docs/testing.md).

## License

See [LICENSE](LICENSE).
