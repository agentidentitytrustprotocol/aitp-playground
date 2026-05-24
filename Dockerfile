# syntax=docker/dockerfile:1.7-labs
#
# Multi-stage build:
#   stage 1 — sdk-builder — compiles the aitp Python SDK (Rust extension)
#             from the sibling aitp-rs repo using maturin.
#   stage 2 — runtime     — slim Python image that imports the SDK wheel
#             produced by stage 1 and runs the FastAPI playground.
#
# The build context is the *parent* directory of this repo so the sibling
# aitp-rs/ source tree is visible. The compose files set context: .. for you.
# To build manually:
#
#   cd /path/to/agentIdenitytrustprotocol
#   docker build -f aitp-playground/Dockerfile -t aitp-playground .
#
# No host Rust toolchain or maturin is required.

# ============================================================================
# Stage 1 — build the aitp wheel (native arch).
# ============================================================================
FROM python:3.12-slim AS sdk-builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates build-essential pkg-config && \
    rm -rf /var/lib/apt/lists/*

# Rust toolchain (the SDK depends on path crates throughout aitp-rs).
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --default-toolchain stable --profile minimal --no-modify-path

RUN pip install --no-cache-dir maturin

# Pull in the entire aitp-rs source tree, minus the giant Rust target/ dirs
# and any .git history. --exclude needs the dockerfile/1.7-labs frontend.
COPY --exclude=**/target --exclude=**/.git \
     aitp-rs/ /build/aitp-rs/

WORKDIR /build/aitp-rs/bindings/aitp-py

# BuildKit cache mounts keep cargo registry + target between rebuilds.
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    --mount=type=cache,target=/build/aitp-rs/bindings/aitp-py/target \
    maturin build --release --out /wheels --interpreter python3.12 && \
    ls -la /wheels

# ============================================================================
# Stage 2 — runtime image.
# ============================================================================
FROM python:3.12-slim AS runtime

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

COPY --from=sdk-builder /wheels/aitp-*.whl /tmp/wheels/

COPY aitp-playground/pyproject.toml ./
COPY aitp-playground/src/ ./src/
COPY aitp-playground/agents/ ./agents/
COPY aitp-playground/scenarios/ ./scenarios/

RUN pip install --upgrade pip && \
    pip install /tmp/wheels/aitp-*.whl && \
    if [ -n "$INSTALL_EXTRAS" ]; then \
        pip install -e ".[$INSTALL_EXTRAS]"; \
    else \
        pip install -e .; \
    fi

ENV PYTHONPATH=/app/src:/app/agents/base

EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=20 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "aitp_playground.main:app", "--host", "0.0.0.0", "--port", "8000"]
