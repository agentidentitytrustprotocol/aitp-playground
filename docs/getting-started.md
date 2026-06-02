# Getting started

Local dev loop, common commands, and a first scenario run.

## Prerequisites

- Python 3.11+ (matches `pyproject.toml`'s `requires-python`).
- `uv` (recommended) or `pip`.
- The sibling `aitp-rs/bindings/aitp-py` SDK built into your active
  venv. The Dockerized flow builds it for you; for native dev you'll
  build it once with `maturin`.

> Prefer Docker? Skip to [docker.md](docker.md) — `docker compose -f
> docker-compose.test.yml up --build --abort-on-container-exit` runs the
> service plus the e2e suite end-to-end with no host toolchain.

## One-time SDK install

```bash
cd ../aitp-rs/bindings/aitp-py
maturin develop --release           # builds the Rust extension into the active venv
```

Verify:
```bash
python -c "import aitp; print(aitp.__version__ if hasattr(aitp,'__version__') else 'ok')"
```

## Install the service

```bash
cd aitp-playground
uv sync                              # or: pip install -e .
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
| `CP_BASE_URL` | _(empty)_ | Optional Control Plane base URL |
| `CP_API_KEY` | _(empty)_ | Optional CP bearer |
| `LLM_PROVIDER` | `openai` | `openai` or `anthropic` |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | _(empty)_ | Required for real LLM output |
| `OPENAI_MODEL` | `gpt-4o-mini` | Override |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Override |
| `RUN_HISTORY_DB` | _(empty)_ | When set, persist runs + events to this SQLite file so they survive a restart. Empty = in-memory only. |
| `LOG_LEVEL` | `INFO` | Standard logging level |

See [llm-providers.md](llm-providers.md) for provider details.

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
| `GET  /packs` | List loaded scenario packs |
| `GET  /scenarios` | List all scenarios with refs |
| `GET  /scenarios/{pack}/{scenario}@{version}` | Full scenario YAML, parsed |
| `POST /runs` | Start a run (async; returns run_id immediately) |
| `GET  /runs` | List recent runs (in-memory by default; `RUN_HISTORY_DB` makes them durable) |
| `GET  /runs/{id}` | Full run record incl. outputs and events |
| `GET  /runs/{id}/status` | Just status + event count |
| `GET  /runs/{id}/events` | SSE event stream (replay + live) |
| `GET  /runs/{id}/cp-deliveries` | CP webhook deliveries this run has received (requires a prior `cp_subscribe_webhook` step) |
| `POST /webhooks/cp/{run_id}` | Receiver Control Plane POSTs to during webhook fan-out (HMAC-verified) |
| `POST /runs/{id}/cancel` | Kill agent subprocesses, mark cancelled |
| `GET  /agents` | List currently-running agent processes |
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

## Common dev loops

| You're working on… | Run |
| --- | --- |
| Scenario YAML | Edit + `dry-run`; if good, hit `POST /runs`. `REGISTRY_CACHE_TTL_MS=0` so no restart needed. |
| Runner engine | `uv run pytest tests/integration/test_runner.py -v` (uses stubs). |
| Agent worker | Restart the uvicorn server so it re-spawns subprocesses with your changes. |
| Real LLM behavior | Set `OPENAI_API_KEY` and either run a scenario or `AITP_LLM_E2E=1 uv run pytest tests/integration/test_llm_e2e.py`. |

See [testing.md](testing.md) for the full test layout.

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
