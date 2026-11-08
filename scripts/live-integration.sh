#!/usr/bin/env bash
#
# live-integration.sh — run the docker compose integration suite against a
# REAL LLM provider, locally, with a hard cost ceiling. Never in CI.
#
#   scripts/live-integration.sh                       # OpenAI (gpt-4o-mini)
#   LIVE_PROVIDER=anthropic scripts/live-integration.sh
#
# What it does:
#   * refuses under CI, without Docker / Compose >= 2.17, without a gitignored
#     .env, or without the selected provider's key in .env (checked by NAME;
#     no value is ever read into this script or printed);
#   * resolves the control-plane checkout (AITP_CP_DIR, else ../aitp-cp, else
#     ../aitp-control-plane) and ../aitp-rs, and exports AITP_CP_DIR for
#     docker-compose.test.yml;
#   * brings up docker-compose.test.yml + docker-compose.live.yml under a
#     per-run project name (aitp-live-<epoch>-<pid>) and runs the tests
#     container (test_live_quota_e2e.py first, then test_llm_e2e.py and
#     test_protocol_e2e.py);
#   * tears the project down (containers, network, volumes) on EXIT, including
#     Ctrl-C / SIGTERM and failures;
#   * writes a scrubbed log (sk-… and Bearer … redacted) and fails if a raw
#     key pattern still made it into the log.
#
# Cost guard (docker-compose.live.yml): the global daily LLM quota is the
# ceiling on provider call units for the whole run, LIVE_MAX_CALLS (default
# 120, refused above 1000); 512 output tokens per call; cheapest model.
#
# Env:
#   LIVE_PROVIDER         openai | anthropic            (default openai)
#   LIVE_MAX_CALLS        provider call-unit ceiling     (default 120)
#   LIVE_OPENAI_MODEL     default gpt-4o-mini
#   LIVE_ANTHROPIC_MODEL  default claude-haiku-4-5
#   AITP_CP_DIR           control-plane checkout          (auto-detected)
#   LIVE_LOG_DIR          where the scrubbed log goes     (default $TMPDIR)
#
# Exit code: the tests container's exit code, or non-zero on a refusal.

set -euo pipefail

die() { echo "live-integration: $*" >&2; exit 2; }
note() { echo "live-integration: $*" >&2; }

# --- refuse under CI, first, before anything else -------------------------
if [ -n "${CI:-}" ] || [ -n "${GITHUB_ACTIONS:-}" ]; then
  die "refusing to run under CI: live LLM tests spend real money (CI is set)"
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# --- preflight ------------------------------------------------------------
command -v docker >/dev/null 2>&1 || die "docker not found"
docker info >/dev/null 2>&1 || die "the Docker daemon is not reachable"
compose_version="$(docker compose version --short 2>/dev/null || true)"
[ -n "$compose_version" ] || die "docker compose (v2) not found"
compose_version="${compose_version#v}"
compose_major="${compose_version%%.*}"
compose_rest="${compose_version#*.}"
compose_minor="${compose_rest%%.*}"
if ! [ "$compose_major" -gt 2 ] 2>/dev/null \
   && ! { [ "$compose_major" -eq 2 ] && [ "$compose_minor" -ge 17 ]; } 2>/dev/null; then
  die "docker compose >= 2.17 required (build.additional_contexts); found $compose_version"
fi

[ -f .env ] || die ".env not found in $ROOT (cp .env.example .env, then add the provider key)"
git check-ignore -q .env || die ".env is not gitignored; refusing to use it"

LIVE_PROVIDER="${LIVE_PROVIDER:-openai}"
case "$LIVE_PROVIDER" in
  openai) KEY_NAME=OPENAI_API_KEY ;;
  anthropic) KEY_NAME=ANTHROPIC_API_KEY ;;
  *) die "LIVE_PROVIDER must be openai or anthropic (got '$LIVE_PROVIDER')" ;;
esac
# By name only: grep -q prints nothing and the value never enters this shell.
grep -q "^${KEY_NAME}=." .env || die ".env has no ${KEY_NAME} (needed for LIVE_PROVIDER=$LIVE_PROVIDER)"

LIVE_MAX_CALLS="${LIVE_MAX_CALLS:-120}"
case "$LIVE_MAX_CALLS" in
  ''|*[!0-9]*) die "LIVE_MAX_CALLS must be a positive integer" ;;
esac
[ "$LIVE_MAX_CALLS" -ge 1 ] && [ "$LIVE_MAX_CALLS" -le 1000 ] \
  || die "LIVE_MAX_CALLS must be between 1 and 1000 (got $LIVE_MAX_CALLS)"

if [ -z "${AITP_CP_DIR:-}" ]; then
  for candidate in ../aitp-cp ../aitp-control-plane; do
    if [ -d "$candidate/drizzle" ] && [ -f "$candidate/package.json" ]; then
      AITP_CP_DIR="$candidate"
      break
    fi
  done
fi
[ -n "${AITP_CP_DIR:-}" ] || die "control-plane checkout not found (set AITP_CP_DIR, or check out ../aitp-cp or ../aitp-control-plane)"
[ -d "$AITP_CP_DIR/drizzle" ] && [ -f "$AITP_CP_DIR/package.json" ] \
  || die "AITP_CP_DIR=$AITP_CP_DIR is not a control-plane checkout (no drizzle/ or package.json)"
AITP_CP_DIR="$(cd "$AITP_CP_DIR" && pwd)"
[ -d ../aitp-rs/bindings/aitp-node ] || die "../aitp-rs not found (Dockerfile.cp-e2e builds the napi binding from it)"

export AITP_CP_DIR LIVE_PROVIDER LIVE_MAX_CALLS

# --- stale projects from crashed runs -------------------------------------
stale="$( { docker ps -a --filter label=com.docker.compose.project \
    --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null
  docker volume ls --filter label=com.docker.compose.project \
    --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null; } \
  | grep '^aitp-live-' | sort -u || true)"
if [ -n "$stale" ]; then
  note "WARNING: stale live projects from earlier runs (not touched):"
  # shellcheck disable=SC2086 # one project name per word
  printf '  %s\n' $stale >&2
  note "remove with: docker compose -p <name> -f docker-compose.test.yml -f docker-compose.live.yml down -v --remove-orphans"
fi

# --- run ------------------------------------------------------------------
P="aitp-live-$(date +%s)-$$"
LOG_DIR="${LIVE_LOG_DIR:-${TMPDIR:-/tmp}}"
mkdir -p "$LOG_DIR"
LOG="${LOG_DIR%/}/$P.log"
: >"$LOG"
chmod 600 "$LOG"
COMPOSE=(docker compose -p "$P" -f docker-compose.test.yml -f docker-compose.live.yml)

# sk-… (OpenAI sk-/sk-proj-, Anthropic sk-ant-) and bearer tokens.
scrub() {
  sed -E -u \
    -e 's/sk-[A-Za-z0-9_*-]{6,}/sk-[redacted]/g' \
    -e 's/([Bb][Ee][Aa][Rr][Ee][Rr])[[:space:]]+[A-Za-z0-9._~+\/=-]{8,}/\1 [redacted]/g'
}

UP_PID=""
SCRUB_PID=""
FIFO_DIR=""
TORN_DOWN=0
# shellcheck disable=SC2329 # invoked by the EXIT trap
teardown() {
  local rc=$?
  # Best effort from here on: one failing docker call must not skip the rest.
  set +e
  trap - EXIT INT TERM
  [ "$TORN_DOWN" = 1 ] && exit "$rc"
  TORN_DOWN=1
  if [ -n "$UP_PID" ] && kill -0 "$UP_PID" 2>/dev/null; then
    note "stopping docker compose up (pid $UP_PID)"
    kill -INT "$UP_PID" 2>/dev/null || true
    for _ in $(seq 1 30); do kill -0 "$UP_PID" 2>/dev/null || break; sleep 1; done
    kill -KILL "$UP_PID" 2>/dev/null || true
  fi
  # Let the scrubber drain the FIFO into the log before anything reads the log.
  [ -z "$SCRUB_PID" ] || wait "$SCRUB_PID" 2>/dev/null
  [ -z "$FIFO_DIR" ] || rm -rf "$FIFO_DIR"
  if [ "$rc" -ne 0 ]; then
    { echo "=== container logs (tail 300, scrubbed) ==="
      "${COMPOSE[@]}" logs --no-color --tail=300 2>&1 || true; } | scrub >>"$LOG"
  fi
  note "tearing down project $P"
  "${COMPOSE[@]}" down -v --remove-orphans >>"$LOG" 2>&1 || note "WARNING: down failed; check: docker ps -a --filter name=$P"
  left="$(docker ps -aq --filter "label=com.docker.compose.project=$P"; \
          docker volume ls -q --filter "label=com.docker.compose.project=$P")"
  [ -z "$left" ] || note "WARNING: resources of $P remain after teardown"
  # Defence in depth: the log must hold no raw key.
  if grep -Eq 'sk-(proj-|ant-)?[A-Za-z0-9_-]{20,}' "$LOG"; then
    : >"$LOG"
    note "ERROR: a raw key pattern reached the log; the log was emptied"
    rc=3
  fi
  note "log: $LOG"
  exit "$rc"
}
trap teardown EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

note "project $P, provider $LIVE_PROVIDER, call ceiling $LIVE_MAX_CALLS, CP $AITP_CP_DIR"
note "log: $LOG"

# In the background so a signal to this script interrupts `wait` at once (a
# foreground child would delay the trap until it exits). Output goes through a
# FIFO, not a process substitution, so the scrubber can be waited for: the
# summary grep below must read a complete log.
FIFO_DIR="$(mktemp -d "${TMPDIR:-/tmp}/aitp-live-fifo.XXXXXX")"
mkfifo "$FIFO_DIR/out"
scrub <"$FIFO_DIR/out" | tee -a "$LOG" &
SCRUB_PID=$!
"${COMPOSE[@]}" up --build --abort-on-container-exit --exit-code-from tests \
  >"$FIFO_DIR/out" 2>&1 &
UP_PID=$!
set +e
wait "$UP_PID"
rc=$?
wait "$SCRUB_PID"
set -e
UP_PID=""
SCRUB_PID=""

echo "=== pytest summary ==="
grep -E '\| (FAILED|ERROR|SKIPPED) |\| =+ .*(passed|failed|error|skipped)' "$LOG" | tail -40 || true
exit "$rc"
