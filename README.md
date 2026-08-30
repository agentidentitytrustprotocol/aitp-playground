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
3. Each agent uses the `aitp` Python SDK (published to PyPI as
   [`aitp-sdk`](https://pypi.org/project/aitp-sdk/), built from
   [`aitp-rs/bindings/aitp-py`](https://github.com/agentidentitytrustprotocol/aitp-rs/tree/main/bindings/aitp-py))
   to build its identity and run the 4-message AITP handshake with peers.
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
| **Revocation** | fail-closed local revocation and propagation through the Control Plane's list (RFC-AITP-0008). The CP signs the snapshot and the consuming agent verifies it against the pinned `CP_AID` before applying any entry; an unverifiable snapshot is discarded |
| **Lifecycle** | key rotation (0007), in-band TCT renewal (0013), a TCT verification cache, session bundles (0010), SPKI pinning |
| **Discovery** | static localhost, `did:web`, and Control Plane registry — each with graceful fallback |
| **Control Plane** | optional enrollment, webhooks, trust-anchor provisioning, delegation-tree observability |
| **Resilience** | operator-injected faults (`manifest_404`, `peer_offline`) that the run survives with structured outcomes |

Beyond the intra-org `/runs` flow above, `POST /hosted-agents` and friends
spawn a long-lived agent and drive a *cross-domain* handshake/invoke against
a peer hosted by a different instance of this service — see
[architecture.md](docs/architecture.md#hosted-agents-srcaitp_playgroundapihostedpy)
for the full route list.

Everything is optional and degrades cleanly: no LLM key → deterministic
stubs (handshakes still run); no Control Plane → static fallback. Since
`aitp-sdk` 0.4.0 the full SDK surface (renewal, session bundles, SPKI
pinning, TCT cache, multi-hop delegation) ships by default; if you run an
older or custom `--no-default-features` wheel, the advanced scenarios
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
  reference Rust runtime; ships the Python SDK from `bindings/aitp-py/`
  (published to PyPI as [`aitp-sdk`](https://pypi.org/project/aitp-sdk/)).
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

# Or run the full end-to-end stack — Postgres + a real Control Plane +
# the playground + a tests container that runs the protocol e2e suite
# and (with an OPENAI_API_KEY) the live LLM e2e suite. Exit code of the
# `tests` container is the result.
docker compose -f docker-compose.test.yml up --build --abort-on-container-exit
```

First image build is ~5 minutes on Apple Silicon (Rust cold compile);
subsequent rebuilds are seconds thanks to BuildKit cache mounts.

A prebuilt image is published on every push to `main`:
`ghcr.io/agentidentitytrustprotocol/aitp-playground:latest`. It is built
with the `all-agents` LLM extras baked in, so the deployed container
calls a real provider whenever `LLM_PROVIDER` and the matching
`*_API_KEY` are set at runtime (and falls back to deterministic stubs
otherwise).

### Native (Python only — the SDK comes from PyPI)

```bash
# 1. Install the service. `uv sync` pulls every dependency including the
#    AITP SDK — distribution name `aitp-sdk`, import name `aitp` — which
#    ships prebuilt manylinux/macOS wheels, so no Rust toolchain is
#    needed. (Only when developing against unreleased SDK source do you
#    add a [tool.uv.sources] path override and build with maturin — see
#    the comments in pyproject.toml.)
uv sync                               # or: pip install -e .

# 2. Run.
uv run uvicorn aitp_playground.main:app --reload --port 8000

# 3. Trigger a scenario.
curl -X POST http://localhost:8000/runs \
  -H "Content-Type: application/json" \
  -d '{"scenario_ref":"intra-org/research-and-write@1.0.0",
       "inputs":{"topic":"AI agent trust protocols"}}'

# 4. Watch live events (SSE) or poll:
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
│   ├── base/              # shared aitp_server / bootstrap / telemetry / llm / revocation (revocation_state.py, revocation_refresh.py)
│   ├── researcher/        # CrewAI worker
│   ├── writer/            # LangChain worker
│   └── analyzer/          # LangGraph worker
├── scenarios/             # YAML scenario packs (registry on disk)
├── scripts/               # e2e inspection helpers (demo-e2e-run.sh)
├── federated/             # cross-domain federated demo stack — see federated/README.md
└── tests/                 # unit / integration / scenario / e2e
```

## Tests & CI

```bash
# Default suites — fast, in-process, no subprocesses.
uv run pytest tests/unit/             # unit tests
uv run pytest tests/scenarios/        # scenario-pack validation

# Runner integration — spawns real agent subprocesses, no LLM keys needed.
AITP_E2E=1 uv run pytest tests/integration/test_runner.py -v

# Protocol e2e — delegation/revocation/rotation/etc. under real trust,
# still no LLM keys (runs inside the docker-compose.test.yml stack).
AITP_PROTOCOL_E2E=1 uv run pytest tests/integration/test_protocol_e2e.py -v

# Live LLM end-to-end — needs OPENAI_API_KEY (one-command via Docker, see above).
AITP_LLM_E2E=1 uv run pytest tests/integration/test_llm_e2e.py -v

# Lint (ruff).
uv run ruff check .
```

CI (`.github/workflows/ci.yml`) runs ruff lint, the unit + scenario
suites with a coverage floor on Python 3.11 and 3.13 (installing
`aitp-sdk` from PyPI via `uv sync --locked`, so the SDK-dependent tests
run too), and the `AITP_E2E=1` subprocess integration suite.
`docker.yml` builds/pushes the image to ghcr.io and runs the
docker-compose e2e stack on `main`. See
[docs/getting-started.md](docs/getting-started.md#development--testing)
for the tier-by-tier map, with deeper detail in
[internal_docs/testing.md](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/internal_docs/testing.md).

## License

See [LICENSE](LICENSE).
