# Docker

The Dockerfile is multi-stage. Stage 1 obtains the `aitp` Python SDK wheel;
stage 2 is a slim runtime image that installs it and runs the playground.
**No host Rust toolchain or maturin required** on either path.

## `AITP_SDK_SOURCE` — which SDK is in the image

| Value | What stage 1 does | Use it for |
|-------|-------------------|------------|
| `pypi` (**default**) | Installs `aitp-sdk` at the version `uv.lock` pins | Everything: CI, the published image, the e2e stack |
| `path` | Compiles `../aitp-rs` with maturin | Testing *unreleased* SDK source |

```bash
# default — reproducible from this commit
docker build -f aitp-playground/Dockerfile -t aitp-playground .

# opt in to sibling source (requires ../aitp-rs checked out)
docker build -f aitp-playground/Dockerfile \
  --build-arg AITP_SDK_SOURCE=path -t aitp-playground .
```

**Why `pypi` is the default.** The Dockerfile used to always build from the
sibling checkout, and CI checked `aitp-rs` out at whatever `main` happened to
be. Three things followed: the image published to GHCR embedded an unreleased,
unpinned SDK; `uv.lock` did not describe what the container ran; and the e2e
suite was structurally unable to notice a pin/behaviour mismatch in either
direction. That last one is the worst — a stack that cannot see the version
under test reports green about a build nobody ships, and green reads as
coverage.

`tests/integration/test_protocol_e2e.py::test_sdk_version_matches_lock`
asserts the running container's `/capabilities` version equals the `uv.lock`
pin, so this cannot silently regress.

**What changed about image tags.** Before this, `:latest` meant "aitp-rs main
as of build time"; now it means "the pinned wheel". Older tags are not
reproducible from their commit — don't diagnose against them as if they were.

**Local dev note.** `docker compose -f docker-compose.test.yml up` now gets the
PyPI wheel. If you were relying on it picking up your `../aitp-rs` working
tree, pass `--build-arg AITP_SDK_SOURCE=path` (see
[testing.md](testing.md)).

Source: `Dockerfile`, `Dockerfile.dockerignore`,
`docker-compose.yml`, `docker-compose.dev.yml`, `docker-compose.test.yml`.

## Why the build context is the parent directory

`aitp-playground` and `aitp-rs` are sibling repos:

```
agentIdenitytrustprotocol/
├── aitp-rs/                       # Rust workspace; ships the Python SDK
│   └── bindings/aitp-py/
└── aitp-playground/               # this repo
    └── Dockerfile
```

On the `path` build, stage 1 needs to COPY from `aitp-rs/`. With the build
context set to `aitp-playground/` we couldn't reach it (`COPY` can't escape
the context). The `pypi` default reads nothing outside this repo, but the
compose files keep the parent context so both paths work unchanged:

```yaml
build:
  context: ..                          # parent directory
  dockerfile: aitp-playground/Dockerfile
```

To build manually:

```bash
cd /path/to/agentIdenitytrustprotocol
docker build -f aitp-playground/Dockerfile -t aitp-playground .
```

## The two stages

### Stage 1 — `sdk-builder` (shown: the `path` variant)

```dockerfile
FROM python:3.12-slim AS sdk-builder
RUN apt-get install curl build-essential pkg-config
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable --profile minimal
RUN pip install maturin
COPY --exclude=**/target --exclude=**/.git aitp-rs/ /build/aitp-rs/
WORKDIR /build/aitp-rs/bindings/aitp-py
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    --mount=type=cache,target=/build/aitp-rs/bindings/aitp-py/target \
    maturin build --release --out /wheels --interpreter python3.12
```

Notes:
- The `COPY --exclude=...` syntax requires the `docker/dockerfile:1.7-labs`
  frontend — declared at the top of the Dockerfile. Without it
  BuildKit can't strip `target/` and `.git/` and the layer is huge.
- BuildKit cache mounts on the cargo registry, git, and the SDK's
  `target/` keep subsequent rebuilds fast (the first build is ~5
  minutes on Apple Silicon).
- The resulting wheel lands in `/wheels`. Stage 2 copies just that.

### Stage 2 — `runtime`

```dockerfile
FROM python:3.12-slim AS runtime
WORKDIR /app
RUN apt-get install curl ca-certificates build-essential
ARG INSTALL_EXTRAS=""
COPY --from=sdk-builder /wheels/aitp_sdk-*.whl /tmp/wheels/
COPY aitp-playground/pyproject.toml ./
COPY aitp-playground/src/ ./src/
COPY aitp-playground/agents/ ./agents/
COPY aitp-playground/scenarios/ ./scenarios/
RUN pip install /tmp/wheels/aitp_sdk-*.whl && \
    if [ -n "$INSTALL_EXTRAS" ]; then pip install -e ".[$INSTALL_EXTRAS]"; \
    else pip install -e .; fi
ENV PYTHONPATH=/app/src:/app/agents/base
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=20 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1
CMD ["uvicorn", "aitp_playground.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Notes:
- `INSTALL_EXTRAS` is a build arg. Passing `INSTALL_EXTRAS=all-agents`
  pulls in CrewAI, LangChain, LangGraph, OpenAI, and Anthropic so the
  real LLM path runs. Default builds skip those — the stubs are
  enough for many demos.
- `PYTHONPATH` is set to match what the host adapters compute, so
  agent subprocesses inside the container find
  `agents/base/aitp_server.py` etc.
- The HEALTHCHECK is what `docker-compose.test.yml` uses to gate the
  tests container.

## The `.dockerignore` story

`Dockerfile.dockerignore` sits next to the Dockerfile and is auto-picked
up by BuildKit (alternative to `.dockerignore` at the context root,
which would be too coarse here since the context is the parent dir).

The big wins:
- `aitp-rs/target/` — ~9.5 GB of compiled Rust artifacts. Without this
  exclusion BuildKit hashes them before it can even start the build.
- `aitp-rs/**/.venv/` — some interop tests carry a venv with a broken
  python symlink that breaks the COPY.
- `aitp-playground/.git/`, `__pycache__/`, `.pytest_cache/`, etc.

If you add a new top-level directory in the parent that BuildKit
shouldn't see, add it here.

## Compose files

### `docker-compose.yml` — vanilla
Production-ish. Just brings up the playground container.

```bash
docker compose up --build
```

Exposes `8000:8000` and `8100-8120:8100-8120` (the agent port range).
Reads `.env` for OpenAI/Anthropic keys.

### `docker-compose.dev.yml` — hot-reload dev
Same image but runs uvicorn with `--reload`. Bind-mounts `src/`,
`agents/`, and `scenarios/` so code changes inside the container
trigger reload.

```bash
docker compose -f docker-compose.dev.yml up --build
```

Note: scenario YAML changes are picked up by the registry without
reload (TTL is 0 by default). Python changes trigger the uvicorn
reloader.

### `docker-compose.test.yml` — end-to-end LLM tests
Two services:
- `playground` — built with `INSTALL_EXTRAS=all-agents`, has all three
  frameworks installed.
- `tests` — built with `INSTALL_EXTRAS=all-agents,dev`, runs
  `pytest tests/integration/test_llm_e2e.py -v -s`.

The tests container depends on `playground: service_healthy`, so it
waits until the HEALTHCHECK passes (curl `/healthz`) before starting.
Tests run inside their own container and talk to the playground at
`http://playground:8000`.

```bash
cp .env.example .env
$EDITOR .env                # set OPENAI_API_KEY=sk-...

docker compose -f docker-compose.test.yml up --build --abort-on-container-exit
# exit code of the `tests` container = suite result

docker compose -f docker-compose.test.yml down
```

The tests volume-mount `./tests` into the container read-only so test
edits don't require a rebuild.

## Common pitfalls

- **`COPY --exclude` errors** → make sure the first line of the
  Dockerfile is `# syntax=docker/dockerfile:1.7-labs` and your Docker
  has BuildKit enabled. Modern Docker defaults to BuildKit; if you've
  disabled it, `export DOCKER_BUILDKIT=1`.
- **First build is huge / slow** → expected. Rust cold compile of the
  SDK is several minutes on Apple Silicon. Subsequent builds reuse
  cache mounts and finish in seconds for source-only changes.
- **`could not find aitp_sdk-*.whl`** → stage 1 failed; scroll up past the
  failed COPY for the maturin error. Usually a missing system lib
  during the Rust compile.
- **Tests container exits 0 instantly** → check `AITP_LLM_E2E=1` and
  `OPENAI_API_KEY` are reaching the container. The test is gated and
  will skip silently without the env var.
- **Stub markers in test output** → the test detected the
  deterministic stub ran instead of the real LLM. Verify
  `OPENAI_API_KEY` and `LLM_PROVIDER` inside the *playground*
  container (the workers run there, not in the tests container).
- **Agent subprocess crashes inside container** → make sure
  `all-agents` (or the right per-agent extra) was passed via
  `INSTALL_EXTRAS`. Default builds don't carry CrewAI/LangChain/LangGraph.
- **Port collisions on `8100-8120`** → if you're running native and
  containerised at the same time, change `AGENT_BASE_PORT` for one
  of them.

## When to rebuild

| You changed | Need to rebuild? |
| --- | --- |
| Scenario YAML | No (TTL=0; loaded on every lookup) — true for dev compose too. |
| Python source under `src/` or `agents/` | Yes for the prod image; **no** for `docker-compose.dev.yml` (bind-mounted + uvicorn reload). |
| `pyproject.toml` (deps) | Yes. |
| `Dockerfile` or `.dockerignore` | Yes. |
| Rust SDK source in sibling `aitp-rs/` | Yes — stage 1's COPY layer invalidates and maturin reruns (cache mounts keep it incremental). |
