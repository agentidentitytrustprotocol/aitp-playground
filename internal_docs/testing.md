# Testing

The test suite has four layers, each with its own scope and trigger.
None of them are mandatory for a green local dev loop — but the e2e
layers are what give the project teeth.

## Layout

```
tests/
├── conftest.py                    # sets PYTHONPATH and SCENARIOS_DIR
├── unit/                          # fast, in-process, no subprocesses
│   ├── _jcs_reference.py          # vendored RFC 8785 canonicalizer (test fixture, not a test)
│   ├── test_adapters.py           # adapter validation + launch prep
│   ├── test_agent_admin_enroll.py # /admin/enroll-with-cp observable on CP-unreachable
│   ├── test_agent_admin_routes.py # agent_admin.py's own-precondition rejection branches
│   ├── test_api.py                # route contracts
│   ├── test_bootstrap.py          # seed derivation + bootstrap shape
│   ├── test_capabilities.py       # SDK feature probe / GET /capabilities
│   ├── test_cli.py                # validate / dry-run / lint / conformance
│   ├── test_config_env_table.py   # Settings fields vs getting-started.md env table drift
│   ├── test_conformance.py        # RFC fixture catalog + readiness
│   ├── test_cp_client.py          # CpClient methods + graceful fallback
│   ├── test_cp_delegation_tree_timing.py # cp_delegation_tree step flush-before-query ordering
│   ├── test_cp_routes.py          # /cp/* observability proxies
│   ├── test_cp_scenarios.py       # CP-backed step types
│   ├── test_cp_webhook_receiver.py# /webhooks/cp/{run_id} HMAC verify
│   ├── test_dashboard.py          # /dashboard HTML
│   ├── test_delegation_revocation.py # redeem route consults revocation sources (RFC-AITP-0006/0011)
│   ├── test_engine_helpers.py     # ScenarioRunner's pure/branching helpers
│   ├── test_engine_run.py         # ScenarioRunner.run() and step dispatch
│   ├── test_federation.py         # did:web http/https gate, public-origin plumbing, loopback guard
│   ├── test_include_resolver.py   # !include YAML tag for scenario packs
│   ├── test_manifest_verification.py # peer ManifestEnvelope verified before fields are read
│   ├── test_metrics.py            # /metrics exposition
│   ├── test_narrator.py           # event → narration rendering
│   ├── test_port_allocator.py     # allocation / recycling
│   ├── test_registry.py           # Pydantic validation + loader
│   ├── test_revocation_freshness.py # Axis B: no fresh verified snapshot
│   ├── test_revocation_signing_convention.py # installed aitp-sdk's revocation signing input
│   ├── test_revocation_state.py   # RevocationState's two sets + snapshot semantics
│   ├── test_revocation_verify_or_discard.py # unverifiable snapshot is discarded, not merged
│   ├── test_run_store_sqlite.py   # RUN_HISTORY_DB persistence
│   ├── test_runs_api.py           # run lifecycle routes
│   ├── test_sdk_blocked_features.py # feature-gated surfaces absent → skip
│   ├── test_sdk_delegation.py     # SDK delegation behaviors
│   ├── test_sdk_floor_comment_matches_specifier.py # aitp-sdk floor comment vs pyproject specifier
│   ├── test_tct_cache_scenario.py # TCT verification cache
│   ├── test_tct_claims.py         # shared compact-JWS claims reader (agents/base/tct_claims)
│   ├── test_telemetry_api.py      # POST /internal/telemetry sink
│   ├── test_templates.py          # scenario template merge
│   ├── test_trust_orchestrator.py # TrustOrchestrator.resolve_peers branching
│   └── test_trust_resolver.py     # static / did:web / cp_registry
├── integration/
│   ├── test_federated_handshake.py # spawn-and-handshake did:web path (AITP_E2E=1)
│   ├── test_llm_e2e.py            # real LLM calls under AITP trust (AITP_LLM_E2E=1)
│   ├── test_protocol_e2e.py       # protocol scenarios + CP, no LLM (AITP_PROTOCOL_E2E=1)
│   └── test_runner.py             # spawns real agent subprocesses (AITP_E2E=1)
└── scenarios/
    └── test_scenario_packs.py     # offline registry consistency checks (no spawn, no LLM)
```

Unit tests that touch the SDK use `pytest.importorskip("aitp")` so they
skip cleanly when the wheel isn't installed (CI runs without it).

`conftest.py` adds `src/` and `agents/base/` to `sys.path` and points
`SCENARIOS_DIR` at the repo's `scenarios/` so tests don't depend on
where pytest was invoked.

## Layer 1 — unit (`tests/unit/`)

Default test target. Fast, no subprocesses, no network. Exercises:
- Pydantic validation in the registry loader.
- API contracts (routes return what we say they return).
- The CLI's `validate` and `dry-run`.
- `PortAllocator` allocation/recycling.
- `BootstrapBuilder` seed derivation and peer placeholder structure.
- Adapter validation and launch prep.
- `TrustOrchestrator.resolve_peers` for static, did:web, and CP
  discovery.
- SDK-level delegation behaviors (these `pytest.importorskip("aitp")`
  so they skip cleanly without the SDK installed).
- The CP surface — `CpClient` fallbacks, `/cp/*` proxies, the webhook
  receiver's HMAC check, and the CP-backed step types.
- Observability — `/metrics` exposition, the narrator, the dashboard,
  and `RUN_HISTORY_DB` SQLite persistence.
- Feature detection + conformance — `GET /capabilities`, blocked-feature
  handling, and the fixture catalog.

Run:

```bash
uv run pytest tests/unit/
# or just one:
uv run pytest tests/unit/test_runs_api.py -v
```

Coverage is configured via `coverage[toml]` in the dev extras and enforced in
CI (`ci.yml`'s `Tests` job runs `tests/unit` + `tests/scenarios` under
`coverage run`, then two separate `coverage report` gates over that one
measurement — not one aggregate, so a regression in `src/` can't hide behind
`agents/base`'s lower floor or vice versa; see `DECISIONS.md` D-17):

- `src/*` — fail under 88%
- `agents/base/*` — fail under 54%

Add `--cov` locally if you want a report before pushing.

## Layer 2 — integration (`tests/integration/`)

Two files, both gated behind env vars so they stay off the default
suite.

### `test_runner.py` — `AITP_E2E=1`

The full runner happy-path. Spawns real subprocesses for the
`intra-org/research-and-write@1.0.0` scenario, drives the handshake,
runs both workflow steps with the deterministic stubs (no LLM keys
required), and asserts:
- run reaches `success` within 60s,
- `agent.ready`, `trust.peers_resolved`, `trust.established`,
  `step.complete`, `run.complete` all appear in the event log,
- both step ids land in `outputs`.

Run:

```bash
AITP_E2E=1 uv run pytest tests/integration/test_runner.py -v
```

This is the cheapest way to validate the runner without LLM costs. It
takes ~30-45 seconds because subprocess spawn + handshake is real.

### `test_protocol_e2e.py` — `AITP_PROTOCOL_E2E=1`

Exercises the protocol-heavy scenarios end-to-end against a running
playground (and, where relevant, a Control Plane) **without** needing LLM
keys — the stubs supply capability output while the real focus is the AITP
flow: delegation, revocation, key rotation, renewal, session bundles, SPKI
pinning, OIDC identity, and the CP-backed steps. Reads `PLAYGROUND_URL`,
`CP_URL`, and `CP_API_KEY` from the environment. Intended to run inside the
`tests` container of the Dockerized stack, where `AITP_PROTOCOL_E2E=1` and
the service URLs are pre-wired — the compose `tests` service runs this file
alongside `test_llm_e2e.py`.

### `test_llm_e2e.py` — `AITP_LLM_E2E=1` + `OPENAI_API_KEY`

The full integration test: real CrewAI / LangChain / LangGraph
calling OpenAI under real AITP identity and trust, across all three
scenarios:

| Ref | Agents (frameworks) | Discovery |
| --- | --- | --- |
| `intra-org/research-and-write@1.0.0` | researcher (CrewAI), writer (LangChain) | static |
| `cross-cloud/distributed-review@1.0.0` | author (CrewAI), reviewer (LangChain), approver (LangGraph) | did:web |
| `cross-org/federated-analysis@1.0.0` | researcher (CrewAI), analyzer (LangGraph) | CP registry (with static fallback) |

For each scenario it asserts:
- run reaches `success` within 300s,
- required trust events fired (`agent.ready`,
  `trust.peers_resolved`, `trust.established`),
- `llm.started` and `llm.complete` appear (proves at least one real
  LLM call ran),
- step outputs are non-empty,
- step outputs **do not** contain the agent stub markers — that's how
  we detect a silent fallback to the deterministic stub.

Stub markers (from `agents/researcher/crew.py`,
`agents/writer/chain.py`, `agents/analyzer/graph.py`):

| Agent | Marker |
| --- | --- |
| researcher | `continues to attract significant research interest` |
| writer | `# Article (` |
| analyzer | `Misaligned grants between issuer and consumer` |

Run against a locally-running playground:

```bash
# Terminal 1
uv run uvicorn aitp_playground.main:app --reload --port 8000

# Terminal 2
AITP_LLM_E2E=1 PLAYGROUND_URL=http://localhost:8000 \
  OPENAI_API_KEY=sk-... \
  uv run pytest tests/integration/test_llm_e2e.py -v
```

…or, more typically, inside the Dockerized stack — see [docker.md](docker.md).

### When the Dockerized e2e stack runs

| Event | Runs? | Why |
|-------|-------|-----|
| Push to `main` | yes | standing regression check |
| PR touching `uv.lock` | yes | the SDK pin moved — this is the cross-implementation check, and it has to happen **before** merge |
| Any other PR | no | ~6 minutes and two sibling repo checkouts; `build-and-push` already validates the Dockerfile and `ci.yml` runs the unit suite |

The filter is `uv.lock` alone: uv mirrors a declared specifier into the lock's
`requires-dist` metadata, so any dependency edit in `pyproject.toml` necessarily
moves `uv.lock` too — while `[tool.ruff]` and the LLM extras, which live in the
same file and change for unrelated reasons, do not.

The pin trigger exists because `auto-merge.yml` runs on `pull_request` and delegates to
`aitp-ci`'s shared auto-merge: a green bump PR merges unattended. Post-merge e2e *detects* a
signer/verifier disagreement; pre-merge e2e *prevents* one. A `paths-filter` is used rather
than a branch-name match on the bump bot's convention, because it also catches a hand-edited
pin.

**`PENDING.md` P7, closed 2026-08-28:** `docker-compose e2e` is now one of `main`'s
required status checks (added via the `required_status_checks` sub-resource `PATCH`;
see `DECISIONS.md` D-12's reversal and D-13), so a PR that touches `uv.lock` is blocked
from merging until this job goes green.

If this job goes red because the control-plane half of a coordinated flip has not shipped,
that is the design working — sequence the rollout rather than disabling the job.

The stack builds the playground image on the default `AITP_SDK_SOURCE=pypi`
path, so it exercises the `aitp-sdk` version `uv.lock` pins — and
`test_sdk_version_matches_lock` asserts exactly that, rather than trusting it. To run the stack against an unreleased `../aitp-rs` working tree
instead, add `--build-arg AITP_SDK_SOURCE=path` (or set it in the compose
`args:` block):

```bash
cp .env.example .env && $EDITOR .env    # set OPENAI_API_KEY
docker compose -f docker-compose.test.yml up --build --abort-on-container-exit
```

`PLAYGROUND_URL` inside the compose stack is set to
`http://playground:8000` (compose DNS), and `AITP_LLM_E2E=1` is in
the tests container's environment.

To run against Anthropic instead, set `LLM_PROVIDER=anthropic` and
provide `ANTHROPIC_API_KEY`. The model can be overridden with
`ANTHROPIC_MODEL` (default `claude-sonnet-4-6`).

## Layer 3 — scenarios (`tests/scenarios/`)

Three files exist as placeholders for per-scenario assertion tests
beyond what the runner integration covers. They currently all
`pytest.skip` with `Live e2e — enable with AITP_E2E=1 after maturin
develop` — they aren't running anything useful today and are awaiting
a follow-up that exercises each scenario's *specific* event log
shape (rejection codes, grant intersection, redeemed AID, etc.).

If you add a new scenario that needs richer assertions than the LLM
e2e provides, this is the place.

## Which suite for which change

| You changed… | Run |
| --- | --- |
| A registry / API / hosting helper | `uv run pytest tests/unit/` |
| Scenario YAML | `uv run python -m aitp_playground.cli validate scenarios/<path>` then a `dry-run` |
| The runner engine | unit + `AITP_E2E=1 uv run pytest tests/integration/test_runner.py -v` |
| An agent worker (stub path) | unit + `AITP_E2E=1 uv run pytest tests/integration/test_runner.py -v` |
| An agent worker (real LLM path) | the Dockerized e2e — `docker compose -f docker-compose.test.yml up --build --abort-on-container-exit` |
| Dockerfile / compose | the Dockerized e2e |

## Asyncio mode

`pyproject.toml` sets `asyncio_mode = "auto"`. Tests with `async def`
signatures don't need a decorator.

## Importing from the repo in tests

Tests can `from aitp_playground.<...> import ...` because
`conftest.py` adds `src/` to `sys.path`. They can also
`from aitp_server import ...` etc. because it adds `agents/base/` too.
This mirrors how `PythonAgentAdapter._build_env` rigs `PYTHONPATH`
for spawned agents.

## Gating cheatsheet

| Suite | Trigger | Need OpenAI key? | Need spawn? | Wall time |
| --- | --- | --- | --- | --- |
| `tests/unit/` | default | no | no | seconds |
| `tests/integration/test_runner.py` | `AITP_E2E=1` | no (stubs) | yes | ~30-45s |
| `tests/integration/test_protocol_e2e.py` | `AITP_PROTOCOL_E2E=1` | no (stubs) | yes (Docker recommended) | minutes |
| `tests/integration/test_llm_e2e.py` | `AITP_LLM_E2E=1` | yes | yes (Docker recommended) | minutes |
| `tests/scenarios/*` | currently skipped | — | — | — |
