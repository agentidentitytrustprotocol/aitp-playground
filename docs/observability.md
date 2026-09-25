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

## Event types

### Runner-emitted

**Lifecycle & setup**

| Type | Carries | When |
| --- | --- | --- |
| `run.started` | `scenario_ref` | After scenario load + input validation. |
| `agent.spawning` | `agent_id`, `port` | Just before the agent subprocess is launched. |
| `agent.ready` | `agent_id`, `aid`, `port` | After the agent signals ready. |
| `oidc.issuer_minted` | issuer url, kid | When a scenario has any `identity_type: oidc` agent. |
| `trust.peers_resolved` | `peers: {agent_id: manifest_url}` | After peer discovery resolves. |
| `run.complete` | — | Workflow finished cleanly. |
| `run.failed` | `error` | Exception inside spawn / step. |

**Trust, delegation, revocation, identity**

| Type | Carries | When |
| --- | --- | --- |
| `trust.establishing` | `initiator`, `target` | Before the handshake is initiated. |
| `trust.established` | `initiator`, `target`, `grants`, `jti` | After successful handshake. |
| `manifest.verify_failed` | `step_id`, `agent_id`, `cause`, `source_url` | A trust-anchor step: the agent's own manifest failed verification before its key could be pinned. |
| `delegation.issuing` | `initiator`, `target`, `grants` | `delegate` step. |
| `delegation.redeeming` | `initiator`, `target` | `redeem_delegation` step. |
| `revocation.published` | `jti`, `to_cp` | `revoke_tct` with `via_cp`. |
| `tct.renewed` | new `jti`, `expires_at` | `renew_tct` step. |
| `tct.cache.stats` | `hits`, `misses`, `size` | `tct_cache_stats` step. |
| `identity.key.rotated` | `old_aid`, `new_aid` | `rotate_keys` step. |
| `session.bundle.exported` | `session_id`, `participant_aids` | `export_session_bundle`. |
| `session.bundle.verified` | `kind`, `active_aids`, `dropped_aids` | `verify_session_bundle`. |
| `spki.pin.checked` | `computed_hash_b64`, `is_pinned` | `spki_pin_check`. |

**Steps & faults**

| Type | Carries | When |
| --- | --- | --- |
| `step.started` | `step_id`, `agent`, `capability` | Workflow step about to run. |
| `step.complete` | `step_id`, `result` | Step succeeded. |
| `step.skipped` | `step_id`, `notes` | `meta` step, or a CP step with no CP configured. |
| `step.probing_no_trust` | `initiator`, `target`, `capability` | `capability_call_no_trust` step. |
| `step.probing_with_held_tct` | … | `capability_probe` step. |
| `step.access_denied` | `target`, `capability`, `result.status_code` | Probe matched expected non-2xx. |
| `step.unexpected_status` | … | Probe got a status that didn't match `expect_status`. |
| `step.fault_injected` | `target`, `notes` | Fault overlay activated. |
| `step.fault_complete` | fault details, captured error | Fault step finished without raising. |

**Control plane**

| Type | When |
| --- | --- |
| `cp.enroll_started` / `cp.enroll_complete` | `enroll_with_cp` step. |
| `cp.webhook.subscribed` / `cp.webhook.subscribe_failed` | `cp_subscribe_webhook` step. |
| `cp.webhook.delivered` | A verified CP webhook delivery arrived (`POST /webhooks/cp/{run_id}`). |
| `cp.trust_anchor.provisioned` | `cp_provision_trust_anchor` step. |
| `cp.delegation.tree` | `cp_delegation_tree` step. |

### Agent-emitted

Agents `POST /internal/telemetry` directly (best-effort — failures are
swallowed rather than failing the run):

| Type | Carries | When |
| --- | --- | --- |
| `handshake.started` | — | A responder accepts a hello. |
| `handshake.complete` | — | Initiator or responder closes the 4-message exchange. |
| `handshake.failed` | `error` | Bad hello or commit. |
| `delegation.issued` / `delegation.rejected` / `delegation.redeemed` | — | RFC-AITP-0006 flow milestones. |
| `tct.revoked` | — | `/admin/revoke-tct` adds a jti. |
| `identity.key.rotated` | `old_aid`, `new_aid` | `/admin/rotate-keys` replaces this agent's keypair. |
| `capability.self_execute` | — | `/admin/self-execute` runs. |
| `llm.started` / `llm.complete` | — | Wraps the LLM call — the clearest signal in the log that real work happened, not a stub. |
| `llm.failed` | `task`, `detail` | The real provider call was attempted (a key was configured) but failed — bad/expired key, quota, unreachable. Surfaces as a 502 from the capability route, `detail` is the provider's own error text. |
| `manifest.verify_failed` | `cause` (`signature_invalid \| expired \| malformed \| unknown`), `source_url` | A fetched peer manifest failed verification. |
| `tct.renewal.requested` / `tct.renewal.issued` | `jti`, plus TCT identifying fields | Holder requests a fresh TCT before the held one expires / issuer mints it. |
| `session.bundle.exported` | `session_id`, `participant_count` | RFC-AITP-0010 coordinator built a session bundle. |
| `session.bundle.verified` | `kind`, `active_count` | RFC-AITP-0010 verifier checked one. |
| `cp.enroll_succeeded` | `aid`, `registered_at` | `/admin/enroll-with-cp` completed. |
| `cp.enroll_failed` | `stage` (`enroll`/`register`), plus `status_code`/`body` or `transport` or `decode` | Either step of `/admin/enroll-with-cp` failed. |
| `revocation.list_fetched` | `jti_count`, `added`, `verified`, `issuer` | A revocation snapshot verified and was applied. |
| `revocation.refresh_failed` | `error` | The snapshot fetch itself failed (transport, not verification). |
| `revocation.verify_failed` | `cause`, `detail` | A fetched snapshot did **not** verify (forged, wrong issuer, expired, malformed). |
| `revocation.poll` | `healthy`, `changed`, `posture` (`unchecked \| current \| degraded`) | The background revocation-poll loop's own heartbeat. |
| `revocation.degraded_serve` | `reason`, `serves`, `fail_mode` | A capability call was served in `soft_fail` degraded posture (no fresh verified snapshot). |

`run.cancelled` is emitted by the runner, not an agent worker.

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

- Want the runtime that produces these events? → [architecture.md](architecture.md)
- First scenario run and the endpoint cheatsheet → [getting-started.md](getting-started.md)
- CP-sourced observability (`/cp/*`, webhook deliveries) → [control-plane.md](control-plane.md#observability-projections)
