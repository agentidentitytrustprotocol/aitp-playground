# Observability

Every state change in a run becomes a `RunEvent`. From that single event
stream the playground derives four views: a **live SSE feed**, a
**human-readable narration**, **Prometheus metrics**, and a **web
dashboard**. This page covers all four and the optional run persistence
behind them.

Source: `src/aitp_playground/runner/{context,store}.py`,
`src/aitp_playground/observability/{metrics,narrator}.py`,
`src/aitp_playground/api/{runs,metrics,dashboard}.py`.

## The event log is the source of truth

`RunContext.emit()` is the single choke point. Every emit does three
things:

1. Appends the `RunEvent` to `ctx.events` (returned in `RunResult` and the
   `GET /runs/{id}` record).
2. Mirrors it to `RunStore.append_event`, which powers `GET /runs/{id}`
   and wakes every SSE subscriber.
3. Feeds `observability.metrics.record_event`, which increments the
   relevant counters/gauge.

Agents also emit events — they `POST /internal/telemetry`, which appends
to the same run log. So `handshake.started`, `llm.started`, etc. (emitted
*inside* an agent subprocess) interleave with runner-emitted events in one
ordered stream.

For the full catalog of event types, see the runner's
[event-types table](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/internal_docs/runner.md#event-types) and the agent-emitted events in
[agents.md](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/internal_docs/agents.md). The taxonomy groups roughly as:

`run.*`, `agent.*`, `oidc.*`, `trust.*` / `handshake.*`, `delegation.*`,
`revocation.*` / `tct.*`, `identity.*`, `session.bundle.*`, `spki.*`,
`step.*`, `cp.*`, `llm.*`.

## Live stream (SSE)

```bash
curl -N http://localhost:8000/runs/<id>/events
```

`GET /runs/{id}/events` is a Server-Sent Events stream. On connect it
**replays the existing backlog** from the store, then streams live events
from a per-subscriber asyncio queue with **1s heartbeats** while idle. It
terminates with `data: {"type":"stream.end"}` once the run is terminal and
the queue is drained.

Backpressure: each subscriber queue is capped at 500 events; a consumer
that falls behind drops the oldest and can backfill via `GET /runs/{id}`.

## Narration

```bash
curl http://localhost:8000/runs/<id>/narrate      # text/plain, one line per event
```

`observability/narrator.py` is a **pure function** — given the event log it
returns one human-readable line per recognized event, e.g.:

```
[trust] established researcher <- writer grants=[write.content] jti=tct-…
[step]  complete write
[cp]    webhook delivered handshake.complete
```

Because it's pure, the same renderer drives both `GET /runs/{id}/narrate`
and the CLI `trace` command — any event log (live, persisted, or replayed
from the CP) narrates identically. Unrecognized event types render to an
empty string and are filtered out, so the narration stays signal-dense;
optional color events (`llm.*`, `capability.self_execute`) surface but
never dominate.

## Metrics

```bash
curl http://localhost:8000/metrics                # Prometheus text exposition (v0.0.4)
```

`observability/metrics.py` is a tiny thread-safe registry (counters + one
gauge — no histograms; this is a demo). The schema is registered up front
so `/metrics` returns a stable, complete surface even before the first run.
`record_event` maps event types onto these:

| Metric | Type | Labels | Incremented on |
| --- | --- | --- | --- |
| `aitp_playground_runs_total` | counter | `status` = success/failed/cancelled | run reaches terminal state |
| `aitp_playground_runs_active` | gauge | — | `run.started` (+1) / terminal (−1) |
| `aitp_playground_handshakes_total` | counter | `outcome` = established/failed | `trust.established` / failures |
| `aitp_playground_tcts_issued_total` | counter | — | `trust.established`, delegation redeem |
| `aitp_playground_delegations_total` | counter | `outcome` = issued/redeemed/rejected | delegation events |
| `aitp_playground_revocations_total` | counter | `source` = local/cp | `tct.revoked` / `revocation.published` |
| `aitp_playground_key_rotations_total` | counter | — | `identity.key.rotated` |
| `aitp_playground_capability_calls_total` | counter | `outcome` = success/denied | capability step complete/denied |
| `aitp_playground_step_outcomes_total` | counter | `outcome` = complete/denied/skipped | every workflow step |

Output is deterministically ordered (by metric name then labels) with
Prometheus-spec label escaping. No auth — it's a demo endpoint.

## Dashboard

```
http://localhost:8000/dashboard
```

`api/dashboard.py` serves a single self-contained HTML page (a dark "signal
console") with inline CSS/vanilla JS — no build step, no external assets.
It consumes the public endpoints already described: `/scenarios`, `/runs`,
`/metrics`, `/capabilities`, `/cp/dashboard`, and the per-run SSE stream.
It's a convenience viewer; everything it shows is available from the JSON
APIs.

## Persistence (`RUN_HISTORY_DB`)

By default `RunStore` is **in-memory only** — runs and their events vanish
on restart. Set `RUN_HISTORY_DB=<path>` and the store becomes a
`SqliteRunStore` that:

- mirrors every `upsert` and `append_event` to a SQLite file (`runs` and
  `run_events` tables), and
- rehydrates the in-memory cache from that file on startup, so
  `GET /runs`, `GET /runs/{id}`, and the SSE backlog survive a process
  restart.

Live SSE subscribers are *not* persisted (they're per-process asyncio
queues); a new subscriber after restart simply replays the persisted
backlog. The in-memory cache stays the authoritative read path — SQLite is
a durable sidecar, not a query engine.

## Quick reference

| View | Endpoint | Format |
| --- | --- | --- |
| Live events | `GET /runs/{id}/events` | text/event-stream (SSE) |
| Full record | `GET /runs/{id}` | JSON (record + events) |
| Compact status | `GET /runs/{id}/status` | JSON |
| Narration | `GET /runs/{id}/narrate` | text/plain |
| Metrics | `GET /metrics` | Prometheus text |
| Dashboard | `GET /dashboard` | HTML |

## Where to read next

- What each event means → [runner.md](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/internal_docs/runner.md#event-types)
- Agent-emitted telemetry → [agents.md](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/internal_docs/agents.md)
- CP-sourced observability (`/cp/*`, webhook deliveries) → [control-plane.md](control-plane.md#observability-projections)
