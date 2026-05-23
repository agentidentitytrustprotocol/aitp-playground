# aitp-playground

Python FastAPI service that runs **Agent Identity & Trust Protocol** (AITP)
scenario demonstrations end-to-end with real LLM-powered agents.

The service:

1. Loads scenario packs from `scenarios/` (intra-org, cross-org, cross-cloud).
2. Spawns agent worker subprocesses (CrewAI, LangChain, LangGraph).
3. Each agent uses the `aitp-py` SDK to establish AITP identity and perform
   the 4-message mutual handshake with its peers.
4. The runner drives capability calls between agents and surfaces a trace of
   every protocol event for observability.

All protocol logic lives in the `aitp-py` SDK
(`/Users/ajitkoti/code/agentIdenitytrustprotocol/aitp-rs/bindings/aitp-py`).
This repo is purely a scenario-orchestration and demo harness.

## Quick start

```bash
# 1. Build the aitp-py extension into a venv (one-time):
cd ../aitp-rs/bindings/aitp-py
maturin develop --release

# 2. Run the service from this repo:
cd ../../../aitp-playground
uv sync                              # or: pip install -e .
uv run uvicorn aitp_playground.main:app --reload --port 8000

# 3. Trigger a scenario:
curl -X POST http://localhost:8000/runs \
  -H "Content-Type: application/json" \
  -d '{"scenario_ref":"intra-org/research-and-write@1.0.0",
       "inputs":{"topic":"AI agent trust protocols"}}'
```

See `CLAUDE.md` for invariants and architecture, `plans/aitp-playground-python.md`
for the original design document, and `scenarios/` for the available demos.

## Testing

```bash
# Unit + integration suite (default — fast, no subprocesses)
uv run pytest tests/

# Live end-to-end: spawns real agent workers, runs the intra-org scenario,
# and asserts the expected AITP events fired. Takes ~30-45 seconds with stub
# agents (no LLM keys required); longer with real LLMs configured.
AITP_E2E=1 uv run pytest tests/integration/test_runner.py -v
```

The `AITP_E2E=1` gate exists because subprocess spawn + handshake is slow and
flaky on resource-constrained CI; opt in explicitly when you want runner
regression coverage.

Agent dependencies are installed via the top-level `pyproject.toml` optional
extras (e.g. `pip install -e .[researcher]`). Manifests intentionally do not
carry per-agent `requirementsFile` pointers — the host adapters do not install
dependencies on spawn.

### LLM end-to-end tests (Docker)

`docker-compose.test.yml` spins up two containers — one running the playground
service with all three frameworks installed (CrewAI / LangChain / LangGraph),
one running pytest — and exercises every scenario against **real OpenAI**
calls under real AITP identity + trust. Stub fallbacks are detected and fail
the test, so a green run proves the LLM path actually executed.

No host prerequisites except Docker. The image is multi-stage: it compiles
the `aitp` Python SDK from the sibling `aitp-rs/` Rust source itself, so
you don't need maturin or a Rust toolchain on your Mac.

```bash
# 1. Provide your OpenAI key.
cp .env.example .env
$EDITOR .env      # set OPENAI_API_KEY=sk-...

# 2. Run the suite. Exit code of the `tests` container is the result.
docker compose -f docker-compose.test.yml up --build --abort-on-container-exit

# 3. Tear down when done.
docker compose -f docker-compose.test.yml down
```

The build context is the *parent* directory of this repo so the SDK source is
visible — the compose file handles that for you. First build takes ~5 minutes
on Apple Silicon (Rust compile); subsequent rebuilds are fast thanks to
BuildKit cache mounts on cargo registry + target/.

What it covers:

| Scenario | Agents (frameworks) | Trust discovery |
| --- | --- | --- |
| `intra-org/research-and-write@1.0.0` | researcher (CrewAI), writer (LangChain) | static |
| `cross-cloud/distributed-review@1.0.0` | author (CrewAI), reviewer (LangChain), approver (LangGraph) | did:web |
| `cross-org/federated-analysis@1.0.0` | researcher (CrewAI), analyzer (LangGraph) | CP registry, with static fallback |

To swap providers, set `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY=...` in
`.env`. Models are overridable via `OPENAI_MODEL` / `ANTHROPIC_MODEL`.

The same test file can be run against a non-Dockerized playground:

```bash
AITP_LLM_E2E=1 PLAYGROUND_URL=http://localhost:8000 \
  OPENAI_API_KEY=sk-... \
  uv run pytest tests/integration/test_llm_e2e.py -v
```
