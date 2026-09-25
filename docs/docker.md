# Docker

The Dockerfile is multi-stage. Stage 1 obtains the `aitp` Python SDK wheel;
stage 2 is a slim runtime image that installs it and runs the playground.
**No host Rust toolchain or maturin required** on either path — see
[getting-started.md](getting-started.md) for the native (non-Docker) route.

## `AITP_SDK_SOURCE` — which SDK is in the image

| Value | What stage 1 does | Use it for |
|-------|-------------------|------------|
| `pypi` (**default**) | Installs `aitp-sdk` at the version `uv.lock` pins | Everything: the published image, the e2e stack |
| `path` | Compiles the sibling `aitp-rs` checkout with maturin | Testing *unreleased* SDK source |

```bash
# default — reproducible from this commit
docker build -f aitp-playground/Dockerfile -t aitp-playground .

# opt in to sibling source (requires ../aitp-rs checked out next to this repo)
docker build -f aitp-playground/Dockerfile \
  --build-arg AITP_SDK_SOURCE=path -t aitp-playground .
```

Passing `--build-arg INSTALL_EXTRAS=all-agents` on either build pulls in
CrewAI, LangChain, LangGraph, and the OpenAI/Anthropic clients so the real
LLM path runs. Default builds skip those — the deterministic stubs are
enough for many demos, and it's the largest single factor in build time.

## Compose files

### `docker-compose.yml` — vanilla

Just brings up the playground container.

```bash
docker compose up --build
```

Exposes `8000:8000` and `8100-8120:8100-8120` (the agent port range).
Reads `.env` for OpenAI/Anthropic keys.

### `docker-compose.dev.yml` — hot-reload dev

Same image but runs uvicorn with `--reload`. Bind-mounts `src/`, `agents/`,
and `scenarios/` so code changes inside the container trigger reload.

```bash
docker compose -f docker-compose.dev.yml up --build
```

Scenario YAML changes are picked up by the registry without a reload
(`REGISTRY_CACHE_TTL_MS=0` by default). Python changes trigger the uvicorn
reloader.

### `docker-compose.test.yml` — end-to-end tests

Brings up the playground (built with `INSTALL_EXTRAS=all-agents`) plus a
`tests` service that runs the integration suites against it. The tests
container waits for the playground's healthcheck before starting, and
volume-mounts `./tests` read-only so test edits don't require a rebuild.

```bash
cp .env.example .env
$EDITOR .env                # set OPENAI_API_KEY=sk-...

docker compose -f docker-compose.test.yml up --build --abort-on-container-exit
# exit code of the `tests` container = suite result

docker compose -f docker-compose.test.yml down
```

To run against Anthropic instead, set `LLM_PROVIDER=anthropic` and provide
`ANTHROPIC_API_KEY` in `.env` (model override: `ANTHROPIC_MODEL`, default
`claude-sonnet-4-6`). See [getting-started.md § Development & testing](getting-started.md#development--testing)
for what this stack actually runs.

## Common pitfalls

- **First build is slow** — expected. A cold Rust compile of the SDK (the
  `path` variant) takes several minutes on Apple Silicon. Subsequent builds
  reuse cache layers and finish in seconds for source-only changes.
- **Tests container exits 0 instantly** — check `AITP_LLM_E2E=1` and
  `OPENAI_API_KEY` are reaching the container. The LLM test tier is gated
  and skips silently without the env var.
- **Stub markers in test output** — the test detected the deterministic
  stub ran instead of the real LLM. Verify `OPENAI_API_KEY` and
  `LLM_PROVIDER` inside the *playground* container (the agent workers run
  there, not in the tests container).
- **Agent subprocess crashes inside the container** — make sure
  `all-agents` (or the right per-agent extra) was passed via
  `INSTALL_EXTRAS`. Default builds don't carry CrewAI/LangChain/LangGraph.
- **Port collisions on `8100-8120`** — if you're running native and
  containerized at the same time, change `AGENT_BASE_PORT` for one of them.

## When to rebuild

| You changed | Need to rebuild? |
| --- | --- |
| Scenario YAML | No (TTL=0; loaded on every lookup) — true for dev compose too. |
| Python source under `src/` or `agents/` | Yes for the prod image; **no** for `docker-compose.dev.yml` (bind-mounted + uvicorn reload). |
| `pyproject.toml` (deps) | Yes. |
| `Dockerfile` or `.dockerignore` | Yes. |

## Where to read next

- Want the native (non-Docker) install? → [getting-started.md](getting-started.md)
- Want the test tier breakdown? → [getting-started.md § Development & testing](getting-started.md#development--testing)
- Which SDK features the built image exposes? → [capabilities.md](capabilities.md)
