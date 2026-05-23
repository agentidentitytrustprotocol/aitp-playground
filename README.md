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
