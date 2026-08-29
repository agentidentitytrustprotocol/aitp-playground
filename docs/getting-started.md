# Getting started

Local dev loop, common commands, and a first scenario run.

## Prerequisites

- Python 3.11+ (matches `pyproject.toml`'s `requires-python`).
- `uv` (recommended) or `pip`.

That's it — the AITP SDK installs from PyPI (see below), so no Rust
toolchain is needed for normal development.

> Prefer Docker? Skip to [docker.md](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/internal_docs/docker.md) — `docker compose -f
> docker-compose.test.yml up --build --abort-on-container-exit` runs the
> service plus the e2e suite end-to-end with no host toolchain.

## The SDK

The AITP SDK is a regular dependency: distribution name **`aitp-sdk`**
on PyPI, import name **`aitp`**. It ships prebuilt manylinux/macOS
wheels, so `uv sync` (or `pip install -e .`) installs it like any other
package — no `maturin`, no local build.

You only build from source when developing against **unreleased** SDK
changes in the sibling `aitp-rs` checkout. In that case add a path
override (requires a local Rust toolchain):

```toml
# pyproject.toml
[tool.uv.sources]
aitp-sdk = { path = "../aitp-rs/bindings/aitp-py" }
```

or build the extension into your active venv directly with
`maturin develop --release` from `../aitp-rs/bindings/aitp-py` — build
details (features, wheels) are the SDK's own docs:
[aitp-py README](https://github.com/agentidentitytrustprotocol/aitp-rs/blob/main/bindings/aitp-py/README.md)
and [sdk-python.md § Build](https://github.com/agentidentitytrustprotocol/aitp-rs/blob/main/docs/sdk-python.md#build).

See the comments in `pyproject.toml` for the authoritative story (and
note the floor pin: `aitp-sdk>=0.7.0` — pre-0.3 wheels speak the
wire-incompatible `aitp/0.1` protocol; 0.5.0 and 0.7.0 are later breaking
bumps for the same reason, recorded there).

## Install the service

```bash
cd aitp-playground
uv sync                              # or: pip install -e .
```

Verify the SDK is importable:

```bash
uv run python -c "import importlib.metadata as md; import aitp; print(md.version('aitp-sdk'))"
```

Optional agent extras (only needed if you want real LangChain/CrewAI/LangGraph
running on the host):

```bash
pip install -e ".[all-agents]"       # crewai + langchain + langgraph + LLM clients
# or per agent:
pip install -e ".[researcher]"
pip install -e ".[writer]"
pip install -e ".[analyzer]"
```

Without the extras the agents fall back to deterministic stubs — handshakes
and TCTs still happen, only the LLM output is canned. Great for
fast iteration on the runner.

## Configure

Copy `.env.example` → `.env` and edit. The only key the service strictly
needs for real LLM output is `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY` if
you set `LLM_PROVIDER=anthropic`). Everything else has a sensible default.

| Var | Default | Purpose |
| --- | --- | --- |
| `PORT` | `8000` | uvicorn bind port |
| `HOST` | `0.0.0.0` | uvicorn bind host |
| `SCENARIOS_DIR` | `./scenarios` | Where the registry walks |
| `REGISTRY_CACHE_TTL_MS` | `0` | `0` = reload every lookup (hot reload while authoring) |
| `AGENT_BASE_PORT` | `8100` | First port handed to spawned agents |
| `AGENT_PYTHON` | `python3` | Interpreter for agent subprocesses |
| `PLAYGROUND_BASE_URL` | `http://localhost:8000` | Where agents POST telemetry |
| `CP_BASE_URL` | _(empty)_ | Optional Control Plane base URL ([control-plane.md](control-plane.md)) |
| `CP_API_KEY` | _(empty)_ | Optional CP bearer |
| `CP_TIMEOUT_MS` | `5000` | Per-request timeout for CP calls |
| `LLM_PROVIDER` | `openai` | `openai` or `anthropic` |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | _(empty)_ | Required for real LLM output |
| `OPENAI_MODEL` | `gpt-4o-mini` | Override |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Override |
| `RUN_HISTORY_DB` | _(empty)_ | When set, persist runs + events to this SQLite file so they survive a restart. Empty = in-memory only. |
| `LOG_LEVEL` | `INFO` | Standard logging level |

See [llm-providers.md](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/internal_docs/llm-providers.md) for provider details.

## Run the service

```bash
uv run uvicorn aitp_playground.main:app --reload --port 8000
```

Hit health to confirm:

```bash
curl -s http://localhost:8000/healthz
# {"status":"ok"}
```

## First scenario run

```bash
curl -s -X POST http://localhost:8000/runs \
  -H "Content-Type: application/json" \
  -d '{"scenario_ref":"intra-org/research-and-write@1.0.0",
       "inputs":{"topic":"AI agent identity"}}'
# {"run_id":"<uuid>","status":"pending","scenario_ref":"intra-org/research-and-write@1.0.0"}
```

Watch it run:

```bash
# Poll the final state:
curl -s http://localhost:8000/runs/<uuid> | jq .

# Or stream events live (SSE):
curl -N http://localhost:8000/runs/<uuid>/events
```

Cancel a stuck run:

```bash
curl -s -X POST http://localhost:8000/runs/<uuid>/cancel
```

## Useful endpoints

| Endpoint | What |
| --- | --- |
| `GET  /healthz` | Liveness |
| `GET  /capabilities` | Installed `aitp` wheel + which optional features it exposes ([capabilities.md](capabilities.md)) |
| `GET  /packs` | List loaded scenario packs |
| `GET  /scenarios` | List all scenarios with refs |
| `GET  /scenarios/{pack}/{scenario}@{version}` | Full scenario YAML, parsed (+ template list) |
| `POST /runs` | Start a run (async; returns run_id immediately). Body accepts `template` to run a variant. |
| `GET  /runs` | List recent runs (in-memory by default; `RUN_HISTORY_DB` makes them durable) |
| `GET  /runs/{id}` | Full run record incl. outputs and events |
| `GET  /runs/{id}/status` | Just status + event count |
| `GET  /runs/{id}/events` | SSE event stream (replay + live) |
| `GET  /runs/{id}/narrate` | Human-readable narration of the event log (text/plain) |
| `GET  /runs/{id}/cp-audit` | Audit events the CP recorded for this run ([control-plane.md](control-plane.md)) |
| `GET  /runs/{id}/cp-sessions` | Handshake sessions the CP observed for this run |
| `GET  /runs/{id}/cp-deliveries` | CP webhook deliveries this run has received (requires a prior `cp_subscribe_webhook` step) |
| `POST /webhooks/cp/{run_id}` | Receiver Control Plane POSTs to during webhook fan-out (HMAC-verified) |
| `POST /runs/{id}/cancel` | Kill agent subprocesses, mark cancelled |
| `GET  /agents` | List currently-running agent processes |
| `GET  /metrics` | Prometheus metrics ([observability.md](observability.md)) |
| `GET  /dashboard` | Single-page trust console (HTML) |
| `GET  /cp/*` | Read-only Control Plane observability projections ([control-plane.md](control-plane.md)) |
| `POST /internal/telemetry` | Sink for agents — not for external use |

OpenAPI is at `http://localhost:8000/docs` while the server runs.

## Scenario authoring CLI

For dev work without spinning up the API:

```bash
uv run python -m aitp_playground.cli list

uv run python -m aitp_playground.cli validate
uv run python -m aitp_playground.cli validate scenarios/intra-org/research-and-write

uv run python -m aitp_playground.cli dry-run intra-org/research-and-write@1.0.0 \
  --inputs '{"topic":"test"}'
```

`dry-run` validates the inputs against the scenario schema and prints
the trust mode, agent list, and workflow steps without spawning
anything. Useful for catching typos before you wait for spawns.

The CLI has a few more subcommands:

```bash
uv run python -m aitp_playground.cli new intra-org/my-scenario@1.0.0   # scaffold on disk
uv run python -m aitp_playground.cli lint                              # cross-scenario refs + step graph
uv run python -m aitp_playground.cli trace intra-org/research-and-write@1.0.0 \
  --inputs '{"topic":"AI agents"}'                                     # run against a live server + narrate
uv run python -m aitp_playground.cli conformance                      # RFC fixture catalog + wheel readiness
```

`trace` drives a run against a running playground and streams the
narration; `conformance` is covered in [capabilities.md](capabilities.md).

## Common dev loops

| You're working on… | Run |
| --- | --- |
| Scenario YAML | Edit + `dry-run`; if good, hit `POST /runs`. `REGISTRY_CACHE_TTL_MS=0` so no restart needed. |
| Runner engine | `uv run pytest tests/unit/` for the fast loop; `AITP_E2E=1 uv run pytest tests/integration/test_runner.py -v` to drive real subprocesses (stub LLMs, no keys). |
| Agent worker | Restart the uvicorn server so it re-spawns subprocesses with your changes. |
| Real LLM behavior | Set `OPENAI_API_KEY` and either run a scenario or `AITP_LLM_E2E=1 uv run pytest tests/integration/test_llm_e2e.py`. |

## Development & testing

The test suite is tiered; each tier past the default is gated behind an
env var so `pytest` stays fast by default.

| Tier | Command | Gate | What it needs |
| --- | --- | --- | --- |
| Unit | `uv run pytest tests/unit/` | none (default) | Nothing — fast, in-process. |
| Scenario packs | `uv run pytest tests/scenarios/` | none (default) | Validates every YAML pack on disk. |
| Runner integration | `uv run pytest tests/integration/test_runner.py -v` | `AITP_E2E=1` | Spawns real agent subprocesses; deterministic stubs, no LLM keys. |
| Protocol e2e | `uv run pytest tests/integration/test_protocol_e2e.py -v` | `AITP_PROTOCOL_E2E=1` | Delegation/revocation/rotation under real trust. Designed to run inside the `docker-compose.test.yml` stack (real Control Plane + Postgres). |
| LLM e2e | `uv run pytest tests/integration/test_llm_e2e.py -v` | `AITP_LLM_E2E=1` | Real provider calls — needs `OPENAI_API_KEY`. Also run by the Docker test stack. |

Linting is ruff (configured in `pyproject.toml`; `ruff format` exists
but formatting is not CI-enforced):

```bash
uv run ruff check .          # what CI runs
```

How CI maps onto these (`.github/workflows/`):

- **`ci.yml`** — on every PR/push: a ruff lint job; the unit + scenario
  suites with coverage (floor: 88%) on a Python 3.11/3.13 matrix, run
  against `uv sync --locked` (which installs `aitp-sdk` from PyPI, so
  the SDK-dependent tests run rather than skip); and the `AITP_E2E=1`
  runner-integration job.
- **`docker.yml`** — builds the playground image on PRs; on `main` it
  pushes to `ghcr.io` (with the `all-agents` LLM extras baked in) and
  runs the full `docker-compose.test.yml` e2e stack (protocol e2e +
  LLM e2e when the `OPENAI_API_KEY` secret is set).
- **`notify-website.yml`** — pings the docs site to re-sync when
  `docs/**` or `README.md` change on `main`.

See [testing.md](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/internal_docs/testing.md) for the full test layout.

## Troubleshooting

- **`AITP_BOOTSTRAP_FILE not set`** — an agent is being launched without
  the env var. Almost always means you ran an agent script directly
  instead of letting the supervisor spawn it.
- **Subprocess exits before `AITP_AGENT_READY`** — supervisor prints the
  child's stderr. Most common cause: missing optional dep or broken
  import in the agent's `crew.py`/`chain.py`/`graph.py`.
- **`tct rejected: ...`** — the SDK's `verify_tct` failed. Look in the
  event log for the prior `trust.established` to confirm the jti/grants
  the caller actually holds.
- **Run hangs in `running` forever** — usually an agent didn't bind its
  port within `startupTimeoutMs` (default 30s). Check the playground
  logs for the captured stdout/stderr from the child.
- **`/runs/{id}` returns `outputs: {}`** — the run failed before
  reaching any workflow step. Check `error` and the event log; the
  failing step is usually right before `run.failed`.

When in doubt, the event log is authoritative — every state change in
the runner produces a `RunEvent`.
