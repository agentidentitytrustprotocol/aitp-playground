# syntax=docker/dockerfile:1.7-labs
#
# Multi-stage build:
#   stage 1 — sdk-builder — obtains the aitp Python SDK wheel: from PyPI at
#             the version uv.lock pins (default), by compiling that version's
#             locked sdist, or by compiling the sibling aitp-rs source tree.
#   stage 2 — runtime     — slim Python image that installs that wheel and
#             runs the FastAPI playground.
#
# ── AITP_SDK_SOURCE: which SDK does this image actually contain? ────────────
#
#   pypi (default) — install aitp-sdk at the version resolved in uv.lock.
#   pypi-src       — compile that same version from its locked, hash-checked
#                    sdist, with AITP_SDK_FEATURES. The published image uses
#                    this (D-26): from aitp-sdk 0.24 the PyPI wheels omit the
#                    `experimental` features (TCT renewal, session bundles,
#                    multi-hop delegation), and only a source build has them.
#   path           — build from ../aitp-rs source with maturin.
#
# AITP_SDK_FEATURES (default empty) is passed to maturin as `--features` by
# `pypi-src` and `path`; `pypi` ignores it. With it set, the build fails
# unless the wheel exposes the experimental surface, so a featureless build
# can't ship silently.
#
# `pypi` is the default because it makes the image *reproducible from its own
# commit*. Previously this Dockerfile always built from the sibling checkout,
# and CI checked that sibling out at whatever `main` happened to be — so the
# published image embedded an unreleased, unpinned SDK, `uv.lock` did not
# describe what the container ran, and the e2e suite could not observe a
# pin/behaviour mismatch in either direction. A test stack that cannot see the
# version under test does not report weak coverage; it reports misleading
# coverage.
#
# Use `path` when you genuinely mean "test unreleased SDK source" — local
# development against an aitp-rs working tree, or aitp-rs validating its own
# `main` against a live playground:
#
#   docker build --build-arg AITP_SDK_SOURCE=path ...
#
# The build context is the *parent* directory of this repo so the sibling
# aitp-rs/ source tree is visible (needed only for `path`). The compose files
# set context: .. for you. To build manually:
#
#   cd /path/to/agentIdenitytrustprotocol
#   docker build -f aitp-playground/Dockerfile -t aitp-playground .
#
# No host Rust toolchain or maturin is required on either path; `path`
# installs one inside the builder stage.

ARG AITP_SDK_SOURCE=pypi
ARG AITP_SDK_FEATURES=""

# ============================================================================
# Stage 1a — fetch the pinned wheel from PyPI (the default).
# ============================================================================
FROM python:3.13-slim AS sdk-builder-pypi

# Only the lockfile — the pinned version is the entire input to this stage.
COPY aitp-playground/uv.lock /tmp/uv.lock

# `--only-binary=:all:` makes a source fallback a hard build failure rather
# than a silent Rust compile: if aitp-sdk ever stops publishing a wheel for
# this image's platform, we want to know at build time, loudly, not to
# discover it as a mysteriously slow build.
RUN set -eu; \
    version="$(python -c "\
import tomllib, sys; \
lock = tomllib.load(open('/tmp/uv.lock','rb')); \
pkgs = [p for p in lock['package'] if p['name'] == 'aitp-sdk']; \
sys.exit('aitp-sdk not found in uv.lock') if not pkgs else None; \
sys.exit(f'uv.lock has {len(pkgs)} aitp-sdk entries (resolution markers?); refusing to guess which one this image should run') if len(pkgs) > 1 else None; \
print(pkgs[0]['version'])")"; \
    echo "Installing aitp-sdk==${version} (pinned by uv.lock)"; \
    pip download "aitp-sdk==${version}" \
        --only-binary=:all: --no-deps -d /wheels; \
    ls -la /wheels

# ============================================================================
# Rust + maturin toolchain shared by the two source builds below.
# ============================================================================
FROM python:3.13-slim AS rust-builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates build-essential pkg-config && \
    rm -rf /var/lib/apt/lists/*

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --default-toolchain stable --profile minimal --no-modify-path

RUN pip install --no-cache-dir maturin

# ============================================================================
# Stage 1b — compile the locked aitp-sdk sdist (the published image).
# ============================================================================
FROM rust-builder AS sdk-builder-pypi-src

ARG AITP_SDK_FEATURES

COPY aitp-playground/uv.lock /tmp/uv.lock

# The sdist URL and sha256 come from uv.lock, the same pin `pypi` installs,
# and `sha256sum -c` refuses a download that doesn't match it. The sdist
# bundles every aitp-rs crate the binding needs, aitp-experimental included.
RUN set -eu; \
    python -c "\
import tomllib, sys; \
lock = tomllib.load(open('/tmp/uv.lock','rb')); \
pkgs = [p for p in lock['package'] if p['name'] == 'aitp-sdk']; \
sys.exit('aitp-sdk not found in uv.lock') if not pkgs else None; \
sys.exit(f'uv.lock has {len(pkgs)} aitp-sdk entries; refusing to guess') if len(pkgs) > 1 else None; \
sd = pkgs[0].get('sdist') or sys.exit('uv.lock has no sdist for aitp-sdk ' + pkgs[0]['version']); \
print(sd['url']); print(sd['hash'].removeprefix('sha256:'))" > /tmp/sdist; \
    url="$(sed -n 1p /tmp/sdist)"; sha="$(sed -n 2p /tmp/sdist)"; \
    echo "Building aitp-sdk from ${url} (features: ${AITP_SDK_FEATURES:-default})"; \
    curl -fsSL "$url" -o /tmp/aitp-sdk.tar.gz; \
    echo "${sha}  /tmp/aitp-sdk.tar.gz" | sha256sum -c -; \
    mkdir -p /build/sdist && tar -xzf /tmp/aitp-sdk.tar.gz -C /build/sdist --strip-components=1

WORKDIR /build/sdist

RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    --mount=type=cache,target=/build/sdist/bindings/aitp-py/target \
    maturin build --release --out /wheels --interpreter python3.13 \
        ${AITP_SDK_FEATURES:+--features "$AITP_SDK_FEATURES"} && \
    ls -la /wheels

# ============================================================================
# Stage 1c — build the aitp wheel from sibling source (opt-in).
# ============================================================================
FROM rust-builder AS sdk-builder-path

ARG AITP_SDK_FEATURES

# Pull in the entire aitp-rs source tree, minus the giant Rust target/ dirs
# and any .git history. --exclude needs the dockerfile/1.7-labs frontend.
COPY --exclude=**/target --exclude=**/.git \
     aitp-rs/ /build/aitp-rs/

WORKDIR /build/aitp-rs/bindings/aitp-py

# BuildKit cache mounts keep cargo registry + target between rebuilds.
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    --mount=type=cache,target=/build/aitp-rs/bindings/aitp-py/target \
    maturin build --release --out /wheels --interpreter python3.13 \
        ${AITP_SDK_FEATURES:+--features "$AITP_SDK_FEATURES"} && \
    ls -la /wheels

# ============================================================================
# Stage 1 — whichever of the three above AITP_SDK_SOURCE selected.
# ============================================================================
FROM sdk-builder-${AITP_SDK_SOURCE} AS sdk-builder

# ============================================================================
# Stage 2 — runtime image.
# ============================================================================
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates build-essential && \
    rm -rf /var/lib/apt/lists/*

# Build-time toggle for optional dep groups
# (e.g. "all-agents" for the LLM e2e tests; "all-agents,dev" for tests).
ARG INSTALL_EXTRAS=""
ARG AITP_SDK_FEATURES

COPY --from=sdk-builder /wheels/aitp_sdk-*.whl /tmp/wheels/

COPY aitp-playground/pyproject.toml ./
# Carried into the image so the running container can be asserted against
# the pin it was built from — see tests/integration/test_protocol_e2e.py::
# test_sdk_version_matches_lock. Without it, "the image runs the pinned
# wheel" is a claim nothing checks.
COPY aitp-playground/uv.lock ./
COPY aitp-playground/src/ ./src/
COPY aitp-playground/agents/ ./agents/
COPY aitp-playground/scenarios/ ./scenarios/

RUN pip install --upgrade pip && \
    pip install /tmp/wheels/aitp_sdk-*.whl && \
    if [ -n "$INSTALL_EXTRAS" ]; then \
        pip install -e ".[$INSTALL_EXTRAS]"; \
    else \
        pip install -e .; \
    fi && \
    if [ -n "$AITP_SDK_FEATURES" ]; then \
        python -c "import aitp; \
missing = [n for n in ('SessionBundleBuilder', 'verify_session_bundle', 'verify_delegation_multihop') if not hasattr(aitp, n)] \
    + [n for n in ('build_renewal_request', 'process_renewal_request') if not hasattr(aitp.AitpAgent, n)]; \
assert not missing, f'AITP_SDK_FEATURES is set but the wheel lacks {missing}'"; \
    fi

ENV PYTHONPATH=/app/src:/app/agents/base

EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=20 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "aitp_playground.main:app", "--host", "0.0.0.0", "--port", "8000"]
