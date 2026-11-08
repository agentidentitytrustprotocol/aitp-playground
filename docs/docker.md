# Docker

The Dockerfile is multi-stage. Stage 1 obtains the `aitp` Python SDK wheel;
stage 2 is a slim runtime image (Python 3.13) that installs it and runs the
playground.
**No host Rust toolchain or maturin required** on either path — see
[getting-started.md](getting-started.md) for the native (non-Docker) route.

## `AITP_SDK_SOURCE` — which SDK is in the image

| Value | What stage 1 does | Use it for |
|-------|-------------------|------------|
| `pypi` (**default**) | Installs the PyPI wheel at the version `uv.lock` pins | Fast local builds; from aitp-sdk 0.24 that wheel lacks the experimental features |
| `pypi-src` | Compiles that version's locked, hash-checked sdist with `AITP_SDK_FEATURES` | The published image and the compose stacks, with `AITP_SDK_FEATURES=experimental` |
| `path` | Compiles the sibling `aitp-rs` checkout with maturin | Testing *unreleased* SDK source |

```bash
# default — reproducible from this commit
docker build -f aitp-playground/Dockerfile -t aitp-playground .

# what the published image runs (TCT renewal, session bundles, multihop)
docker build -f aitp-playground/Dockerfile \
  --build-arg AITP_SDK_SOURCE=pypi-src \
  --build-arg AITP_SDK_FEATURES=experimental -t aitp-playground .

# opt in to sibling source (requires ../aitp-rs checked out next to this repo)
docker build -f aitp-playground/Dockerfile \
  --build-arg AITP_SDK_SOURCE=path -t aitp-playground .
```

Passing `--build-arg INSTALL_EXTRAS=all-agents` on either build pulls in
CrewAI, LangChain, LangGraph, and the OpenAI/Anthropic clients so the real
LLM path runs. Default builds skip those — the deterministic stubs are
enough for many demos, and it's the largest single factor in build time.

## Image tags

`docker.yml` builds `ghcr.io/agentidentitytrustprotocol/aitp-playground`
(linux/amd64, `INSTALL_EXTRAS=all-agents`, `AITP_SDK_SOURCE=pypi-src`).
Releases never rebuild: `release.yml` retags an image `docker.yml` already
built, so a release tag has the digest of the `sha-<short>` image built on
`main`. The e2e job builds its own stack from that same commit and
Dockerfile, and a release requires both jobs to have succeeded
([deployment.md § Releasing](deployment.md#releasing), DECISIONS D-33).

| Tag | Set by | Moves when |
| --- | --- | --- |
| `sha-<short>` | `docker.yml`, push to `main` | Never; one per main commit |
| `main` | `docker.yml`, push to `main` | Every push to `main` |
| `pr-<n>` | `docker.yml`, pull request | Built to validate the Dockerfile, never pushed |
| `X.Y.Z` | `release.yml` | Never; one per release |
| `X.Y` | `release.yml` | Each release in that minor line |
| `latest` | `release.yml` | Each release, or a rollback. It is the latest release, and what Railway runs |

The build passes `GIT_SHA` (the commit), which the image exposes as
`AITP_PLAYGROUND_REVISION` and `GET /healthz` reports as `revision`. It is
also the `org.opencontainers.image.revision` label. A local build without the
argument reports `"unknown"`.

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

Brings up four services: `cp-postgres` (Postgres 18, ephemeral), `aitp-cp`
(a real Control Plane on Node 24, built from `Dockerfile.cp-e2e`), the
`playground` (built with `INSTALL_EXTRAS=all-agents`) and a `tests` service
that runs the integration suites against it. The tests container waits for
the playground **and** the Control Plane healthchecks before starting, and
volume-mounts `./tests` read-only so test edits don't require a rebuild.

This stack needs two sibling checkouts: the `aitp-control-plane` repo at
`${AITP_CP_DIR:-../aitp-cp}` (CI checks it out as `../aitp-cp`; with a
`../aitp-control-plane` checkout, export `AITP_CP_DIR=../aitp-control-plane`)
and `../aitp-rs` (the CP image compiles its native binding from `aitp-rs`
source even though the playground image itself compiles the locked
`aitp-sdk` sdist from PyPI). The CP source reaches `Dockerfile.cp-e2e` as the
named build context `cpsrc` (`build.additional_contexts`), so Compose 2.17 or
newer is required.

```bash
cp .env.example .env        # no provider key: agents run their stubs
export AITP_CP_DIR=../aitp-control-plane   # only if there is no ../aitp-cp

docker compose -f docker-compose.test.yml up --build --abort-on-container-exit
# exit code of the `tests` container = suite result

docker compose -f docker-compose.test.yml down -v
```

### `docker-compose.live.yml` — the same suite against a real LLM (local only)

An override for `docker-compose.test.yml`, run only through
`scripts/live-integration.sh`. It spends real money, so it is never used in CI
(the script refuses when `CI` is set, and CI has no provider key).

```bash
scripts/live-integration.sh                          # OpenAI (gpt-4o-mini)
LIVE_PROVIDER=anthropic scripts/live-integration.sh  # Anthropic (claude-haiku-4-5)
LIVE_MAX_CALLS=60 scripts/live-integration.sh        # tighter call ceiling
```

The script checks that `.env` is gitignored and holds the selected provider's
key (by name; it never prints a value), finds the CP checkout and
`../aitp-rs`, and runs the stack under a per-run project
`aitp-live-<epoch>-<pid>` that is torn down on exit, Ctrl-C included. The
override caps spend: the global daily LLM quota (`LIVE_MAX_CALLS`, default
120) is the ceiling on provider call units, each call is capped at 512 output
tokens, and the model is the provider's cheapest. It also turns the backend
lockdown on and sets a per-IP quota of 4 for the quota test. The `tests`
container runs `test_live_quota_e2e.py` first, then `test_llm_e2e.py` and
`test_protocol_e2e.py`. The log (scrubbed of `sk-…` and `Bearer …`) is written
to `$LIVE_LOG_DIR` (default `$TMPDIR`). The script's header comment lists
every knob.

### `docker-compose.host-ports.yml` — publish ports to the host

An override layered on `docker-compose.test.yml` (used by the
aitp-ui-console integration test) that publishes the playground (`8000`) and
Control Plane ports to the host; the base test stack keeps them internal.

```bash
docker compose -f docker-compose.test.yml -f docker-compose.host-ports.yml up -d playground aitp-cp
```

The cross-domain federated stack has its own compose files under
`federated/` — see [federated/README.md](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/federated/README.md).

With a provider key in `.env`, the plain test stack calls that provider
(`LLM_PROVIDER=openai|anthropic`; model overrides `OPENAI_MODEL`,
`ANTHROPIC_MODEL`) without the live script's cost guards; prefer the live
script. See [getting-started.md § Development & testing](getting-started.md#development--testing)
for what this stack actually runs.

## Common pitfalls

- **First build is slow with `AITP_SDK_SOURCE=pypi-src` or `path`** (the
  compose files use `pypi-src`) — a cold Rust compile of the SDK takes several
  minutes on Apple Silicon. The default `pypi` build
  only downloads the wheel (plus the LLM extras if requested). Subsequent
  builds reuse cache layers and finish in seconds for source-only changes.
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
| `Dockerfile` or `Dockerfile.dockerignore` | Yes. |

## Where to read next

- Want the native (non-Docker) install? → [getting-started.md](getting-started.md)
- Want the test tier breakdown? → [getting-started.md § Development & testing](getting-started.md#development--testing)
- Which SDK features the built image exposes? → [capabilities.md](capabilities.md)
