#!/usr/bin/env bash
#
# demo-e2e-run.sh — drive a single scenario end to end against a running
# playground and pretty-print the input, every intermediate step output, and
# the final output, plus the AITP trust handshakes and the real-LLM call
# timeline.
#
# This is a demonstration / inspection helper. It performs NO assertions —
# for the pass/fail suite use docker-compose.test.yml (tests/integration).
#
# Usage:
#   scripts/demo-e2e-run.sh [TOPIC] [SCENARIO_REF]
#
#   TOPIC         Free-text topic fed to the scenario (default below).
#   SCENARIO_REF  pack/scenario@version (default: the 3-agent research →
#                 write → analyse pipeline, which shows two intermediate
#                 outputs and one final output).
#
# How it reaches the playground (in priority order):
#   1. If PLAYGROUND_URL is set, curl is run on the host against that URL
#      (use this when you publish the playground port, e.g. -p 8000:8000).
#   2. Otherwise it execs curl INSIDE the compose service, so it works even
#      though docker-compose.test.yml publishes no host ports.
#
# Env overrides:
#   PLAYGROUND_URL     e.g. http://localhost:8000 (host-published deployments)
#   COMPOSE_FILE       compose file to exec into     (default docker-compose.test.yml)
#   COMPOSE_SERVICE    service running the playground (default playground)
#   POLL_TIMEOUT_SECS  max seconds to wait for a terminal run (default 300)
#
# Requires: python3 (host), and either a reachable PLAYGROUND_URL or a running
# `docker compose` stack with the playground service up.
#
set -euo pipefail

TOPIC="${1:-Why verifiable agent identity matters for multi-agent AI systems}"
SCENARIO_REF="${2:-intra-org/research-and-write@1.1.0}"

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.test.yml}"
COMPOSE_SERVICE="${COMPOSE_SERVICE:-playground}"
POLL_TIMEOUT_SECS="${POLL_TIMEOUT_SECS:-300}"

# Run this repo's root regardless of where the script is invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

# ── transport ────────────────────────────────────────────────────────────────
# pg_curl <curl-args...> — issue a curl against the playground, on the host if
# PLAYGROUND_URL is set, otherwise inside the compose service container.
if [[ -n "${PLAYGROUND_URL:-}" ]]; then
  BASE="$PLAYGROUND_URL"
  pg_curl() { curl -fsS "$@"; }
  echo "→ talking to playground at ${BASE} (host)"
else
  BASE="http://localhost:8000"
  pg_curl() { docker compose -f "$COMPOSE_FILE" exec -T "$COMPOSE_SERVICE" curl -fsS "$@"; }
  echo "→ talking to playground via 'docker compose -f ${COMPOSE_FILE} exec ${COMPOSE_SERVICE}'"
fi

INPUTS_JSON="$(TOPIC="$TOPIC" python3 -c 'import json,os;print(json.dumps({"topic":os.environ["TOPIC"]}))')"
REQ_BODY="$(SCENARIO_REF="$SCENARIO_REF" INPUTS_JSON="$INPUTS_JSON" python3 -c \
  'import json,os;print(json.dumps({"scenario_ref":os.environ["SCENARIO_REF"],"inputs":json.loads(os.environ["INPUTS_JSON"])}))')"

# ── kick off the run ─────────────────────────────────────────────────────────
echo "→ POST ${BASE}/runs  scenario=${SCENARIO_REF}"
RESP="$(pg_curl -X POST "${BASE}/runs" -H 'Content-Type: application/json' -d "$REQ_BODY")"
RUN_ID="$(RESP="$RESP" python3 -c 'import json,os;print(json.loads(os.environ["RESP"],strict=False)["run_id"])')"
echo "→ run_id=${RUN_ID}"

# ── poll until terminal ──────────────────────────────────────────────────────
# mktemp template semantics differ across macOS/Linux; -t with a simple prefix
# is portable and avoids a stray suffix landing after a forced extension.
RAW_FILE="$(mktemp -t aitp-run)"
deadline=$(( $(date +%s) + POLL_TIMEOUT_SECS ))
status=""
while [[ $(date +%s) -lt $deadline ]]; do
  # Capture the body to a file: the run record embeds raw newlines inside LLM
  # text, which strict JSON parsers reject — read it back with strict=False.
  pg_curl "${BASE}/runs/${RUN_ID}" > "$RAW_FILE"
  status="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]),strict=False).get("status",""))' "$RAW_FILE" 2>/dev/null || true)"
  printf '   status=%s\r' "${status:-?}"
  case "$status" in
    success|failed|cancelled) echo; break ;;
  esac
  sleep 2
done

if [[ "$status" != "success" && "$status" != "failed" && "$status" != "cancelled" ]]; then
  echo "✗ run did not reach a terminal state within ${POLL_TIMEOUT_SECS}s (last status=${status:-none})" >&2
  exit 1
fi

# ── render input / intermediate / final ──────────────────────────────────────
RAW_FILE="$RAW_FILE" TOPIC="$TOPIC" SCENARIO_REF="$SCENARIO_REF" python3 <<'PY'
import json, os

body = json.loads(open(os.environ["RAW_FILE"]).read(), strict=False)
events = body.get("events") or []
outputs = body.get("outputs") or {}

def rule(title):
    print("\n" + "=" * 78 + "\n" + title + "\n" + "=" * 78)

rule("RUN METADATA")
print("scenario :", body.get("scenario_ref"))
print("run_id   :", body.get("run_id"))
print("status   :", body.get("status"))
if body.get("error"):
    print("error    :", body["error"])

rule("INPUT")
print("topic    :", os.environ["TOPIC"])

rule("TRUST & IDENTITY (AITP handshakes + capability grants)")
TRUST = {"agent.ready", "trust.peers_resolved", "trust.established",
         "handshake.started", "handshake.complete"}
for e in events:
    t = e.get("type", "")
    if t not in TRUST:
        continue
    line = f"[{t}] agent={e.get('agent_id') or '-'}"
    if e.get("grants"):
        line += f" grants={e['grants']}"
    if e.get("peer_aid"):
        line += f" peer={e['peer_aid'][:28]}…"
    print(line)

rule("LLM CALL TIMELINE (real provider calls)")
started = {}
for e in events:
    if e.get("type") == "llm.started":
        started[e.get("agent_id")] = e.get("ts")
    elif e.get("type") == "llm.complete":
        a = e.get("agent_id")
        dur = (e.get("ts", 0) or 0) - (started.get(a, e.get("ts", 0)) or 0)
        print(f"  {str(a):<12} task={str(e.get('task','-')):<10} {dur:5.1f}s")

# Pull the main human-readable text out of a step's output dict.
TEXT_KEYS = ("findings", "article", "analysis", "summary",
             "content", "text", "result", "output")
def render(step, value, label):
    rule(f"{label}   (step: '{step}')")
    if isinstance(value, dict):
        for k in TEXT_KEYS:
            v = value.get(k)
            if isinstance(v, str) and v.strip():
                print(f"[{k}]\n{v}")
                break
        else:
            print(json.dumps(value, indent=2))
        meta = {k: v for k, v in value.items()
                if k not in TEXT_KEYS and (not isinstance(v, str) or len(v) < 200)}
        if meta:
            print("\n[metadata]", json.dumps(meta))
    else:
        print(value)

steps = list(outputs.keys())  # preserves workflow order
for i, step in enumerate(steps):
    last = (i == len(steps) - 1)
    label = "FINAL OUTPUT" if last else f"INTERMEDIATE OUTPUT #{i + 1}"
    render(step, outputs[step], label)

print("\n" + "-" * 78)
print(f"raw run record: {os.environ['RAW_FILE']}")
PY
