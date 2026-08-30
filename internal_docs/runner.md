# Runner

The runner is the brain of the service. Everything else (registry,
hosting, trust, telemetry) is wiring. This page walks the
`ScenarioRunner.run()` method end-to-end and explains every step type.

Source: `src/aitp_playground/runner/engine.py`.

## Public entry point

```python
result = await runner.run(
    scenario_ref="intra-org/research-and-write@1.0.0",
    inputs={"topic": "AI agent identity"},
    run_label=None,                 # optional human label
    run_id=None,                    # caller-supplied id; else uuid4
)
```

The HTTP layer (`api/runs.create_run`) calls this from a FastAPI
background task. The caller gets a `run_id` immediately (`202`); the
runner does the actual work asynchronously and persists progress
through `RunStore`.

## Lifecycle

```
ScenarioRunner.run
├─ 1. Load scenario from registry
├─ 2. Validate inputs against scenario.spec.inputs.schema (jsonschema)
├─ 3. Per agent:
│       allocate port (port_offset honored, else monotonic)
│       resolve manifest
│       build placeholder peers (localhost URLs)
│   if any agent is identity_type: oidc:
│       mint a per-run RunOidcIssuer (Ed25519 keypair)
│       emit oidc.issuer_minted; share it via each bootstrap
│       write bootstrap.json to tmpdir
├─ 4. Per agent (sequential):
│       adapter.validate(manifest)
│       adapter.prepare_launch(...)
│       supervisor.launch(...)  ← blocks until "AITP_AGENT_READY"
│       emit agent.ready
├─ 5. TrustOrchestrator.resolve_peers(...)
│       emit trust.peers_resolved
├─ 6. if scenario.spec.trust.eager:
│       _establish_pairwise_trust (both directions, every pair)
├─ 7. For each workflow step:
│       _dispatch_step(...)  (per-type branches below)
├─ finally:
│       supervisor.kill_run(run_id)
│       release ports
│       delete bootstrap files
└─ emit run.complete OR run.failed
    (best-effort POST events to CP /events as a background task)
```

Any unhandled exception inside steps 4-7 fails the run with
`run.failed` and the exception's message as `error`. Cleanup always
runs.

## Bootstrap and peer placeholder

When the runner writes the bootstrap, it doesn't know the real peer
manifest URLs yet — that's resolved in step 5. So `bootstrap.peers`
gets filled with localhost placeholders. The agent worker can read
them for orientation, but **the real trust calls use the
peer URLs that the trust orchestrator resolved**, passed in the
`/admin/initiate-handshake` request body. Don't drive trust off
`bootstrap.peers`.

## Step dispatch

`_dispatch_step` branches on `step.type`. If `type` is omitted, the
runner infers:
- `agent` + `capability` set → `workflow`.
- otherwise → `meta`.

### `meta`
Pure narration. Emits `step.skipped` with the step's description.
Useful for scenarios where you want a labeled milestone in the event
log without doing real work (the `trust` / `discover` steps in many
scenarios are meta).

### `workflow` (default)
The bread and butter.

1. Resolve the target agent: `_find_capability_holder(capability,
   scenario, manifests, prefer=step.agent)`.
   - If `step.agent` itself offers `capability`, prefer it
     (self-execute). This keeps `agent: X, capability: Y` predictable
     when more than one agent in the scenario offers Y (e.g.
     `delegation-chain` has both `researcher` and `sub-researcher`
     offering `research.query`).
   - Otherwise pick the first scenario agent whose manifest's
     `offered_caps` contains it.
2. Resolve the input via `_resolve_step_input`:
   - `input_from` set → use that prior step's output verbatim.
   - `input_template` set → substitute `{{ inputs.<k> }}` placeholders.
   - Neither → pass the whole `inputs` dict.
3. Emit `step.started`.
4. If self-execute: POST `/admin/self-execute` on the caller.
   If cross-agent: POST `/admin/invoke` on the caller (it attaches its
   held TCT for the target).
5. The admin router wraps non-2xx into `{error:true, status_code,
   body}`. For workflow steps a non-2xx is a hard failure — the chain
   can't continue with a rejection as its result — so the runner
   raises and the run fails.
6. Record output, emit `step.complete`.

### `handshake`
Required: `initiator`, `responder`. Optional: `requested_grants`.

Runs **only the explicit direction**. Earlier versions auto-ran the
reverse too, with no scope narrowing, which silently overwrote
previously-scoped TCTs when scenarios used `requested_grants`.
Scenarios that genuinely need both directions list two handshake
steps.

POSTs `/admin/initiate-handshake` on the initiator with the responder's
manifest URL and the requested grants. Emits `trust.establishing` then
`trust.established` with `grants` and `jti` from the resulting TCT.

### `capability_call_no_trust`
Required: `agent`, `target_agent`, `capability`. Optional:
`expect_status`.

POST `/capabilities/<name>` on the target with **no** `X-AITP-TCT`
header. The target's `verify_capability_tct` rejects with 403 (or
401 if the request was malformed earlier in the stack). The runner
records:

```json
{ "status_code": 403, "rejected": true, "expected_status": 403, "matched": true, "body": {...} }
```

Emits `step.access_denied` when `expect_status` matched, else
`step.unexpected_status`. Used by `trust-gate` to observe the
unauthenticated rejection.

### `capability_probe`
Required: `agent`, `target_agent`, `capability`. Optional:
`expect_status`.

Like `workflow`, but tolerates non-2xx — goes through
`/admin/invoke` (so the caller's held TCT is presented), then inspects
the inner status code without failing the run. Used by
`scoped-capabilities` to assert that an out-of-scope grant produces a
403, and by `revocation-demo` to observe the 403 after revocation.

### `revoke_tct`
Required: `issuer`, `audience`.

Walks the event log backwards looking for the most recent
`trust.established` where `initiator == audience` and `target ==
issuer` (i.e., audience initiated toward issuer, so issuer was the
one who minted the TCT). Takes that event's `jti` and POSTs it to
`issuer`'s `/admin/revoke-tct`. Emits `step.complete` with the
revoked jti.

If no matching prior handshake event is found, the step fails
explicitly — the scenario didn't run the handshake it implies.

### `delegate`
Required: `delegator`, `delegatee`, `via_peer`, `scope` (non-empty).
Optional: `ttl_secs`.

POSTs `/admin/delegate` on the delegator with:
- `held_tct_peer_port`: the port for whose handshake the delegator
  holds the TCT (passed as `via_peer.port`).
- `delegatee_manifest_url`: peer-resolved URL for the delegatee.
- `scope`: capabilities to delegate (must be `⊆` the held TCT's grants;
  the SDK enforces this).
- `ttl_secs`: optional override.

Records the delegation token JSON; emits `delegation.issuing` then
`step.complete`.

### `redeem_delegation`
Required: `delegatee`, `target`, `via_delegation`.

Looks up the prior `delegate` step's output (must contain
`delegation_token`) and POSTs `/admin/redeem-delegation` on the
delegatee with the target's redeem URL (derived from the target's
manifest URL by swapping `/.well-known/aitp-manifest` →
`/aitp/delegation/redeem`).

The delegatee stores the resulting TCT in its `held_tcts[target_port]`,
so a subsequent `workflow` step where `delegatee` calls a capability
on `target` will present the redeemed TCT.

### Identity, renewal, and bundle steps

These dispatch the same way — resolve the named agent(s), POST the
matching `/admin/*` route, emit a domain event then `step.complete`. Most
are gated behind an SDK `experimental-*` feature and degrade cleanly when
the wheel lacks it (see [capabilities.md](../docs/capabilities.md)). Field-level
reference lives in [scenarios.md](../docs/scenarios.md#workflow-steps); the engine
behavior:

| `type` | Drives | Emits |
| --- | --- | --- |
| `rotate_keys` | agent's `/admin/rotate-keys` — new keypair + republished manifest; old-AID TCTs then fail the issuer-AID guard | `identity.key.rotated` |
| `renew_tct` | holder's `/admin/renew-tct` → issuer's `/admin/process-renewal`; held TCT swapped in place | `tct.renewed` |
| `tct_cache_stats` | agent's `/admin/tct-cache-stats` | `tct.cache.stats` |
| `export_session_bundle` | coordinator's `/admin/export-session-bundle` over participants' issued TCTs | `session.bundle.exported` |
| `verify_session_bundle` | verifier's `/admin/verify-session-bundle` on a prior export | `session.bundle.verified` |
| `spki_pin_check` | pure-SDK `compute_spki_hash` + `SpkiPinVerifier` (no agent involved) | `spki.pin.checked` |

### Control-plane steps

These only do CP work and emit `step.skipped` when `CP_BASE_URL` is unset.
See [control-plane.md](../docs/control-plane.md#cp-backed-workflow-steps) for the
full flow.

| `type` | Drives | Emits |
| --- | --- | --- |
| `enroll_with_cp` | agent's `/admin/enroll-with-cp` (mint token → register) | `cp.enroll_started`, `cp.enroll_complete` |
| `cp_subscribe_webhook` | `CpClient.create_webhook` pointing at this run's receiver | `cp.webhook.subscribed` / `…subscribe_failed` |
| `cp_provision_trust_anchor` | `upsert_pinned_key` + optional `upsert_trust_anchor` | `cp.trust_anchor.provisioned` |
| `cp_delegation_tree` | `CpClient.fetch_delegations` (recursive `root_jti`) | `cp.delegation.tree` |

### Fault injection

Any `handshake`, `workflow`, or `capability_probe` step can carry a
`fault:` block (`kind: manifest_404 | peer_offline`). The runner mutates
the call's target before issuing it, captures the resulting error as a
structured step output, and **does not** raise — later steps can branch on
the outcome. Emits `step.fault_injected` then `step.fault_complete`. See
[scenarios.md](../docs/scenarios.md#fault-injection) and
`intra-org/fault-injection`.

## Event stream

Every state change emits a `RunEvent` via `RunContext.emit`. Events
land in two places:
1. `ctx.events: list[RunEvent]` — used by `RunResult` and the
   in-memory `RunStore` record.
2. `RunStore.append_event` — same store powers `GET /runs/{id}` and
   the SSE stream at `GET /runs/{id}/events`.

The SSE endpoint replays the existing backlog from the store, then
pulls from a per-subscriber asyncio queue with 1s heartbeats. It
terminates with `data: {"type":"stream.end"}` once the run is in
a terminal state and the queue is empty. Subscribers that fall behind
500 events drop new events (they can backfill via `GET /runs/{id}`).

### Event types

Runner-emitted events. Agents emit a further set through
`/internal/telemetry` (`handshake.*`, `llm.*`, `delegation.*`,
`tct.revoked`, `revocation.list_fetched`, …) that interleave into the same
log — see [agents.md](agents.md). The narrator
([observability.md](../docs/observability.md#narration)) renders both sets.

**Lifecycle & setup**

| Type | Carries | When |
| --- | --- | --- |
| `run.started` | `scenario_ref` | After scenario load + input validation. |
| `agent.spawning` | `agent_id`, `port` | Just before `supervisor.launch`. |
| `agent.ready` | `agent_id`, `aid`, `port` | After `AITP_AGENT_READY` line seen. |
| `oidc.issuer_minted` | issuer url, kid | When a scenario has any `identity_type: oidc` agent. |
| `trust.peers_resolved` | `peers: {agent_id: manifest_url}` | After `TrustOrchestrator.resolve_peers`. |
| `run.complete` | — | Workflow finished cleanly. |
| `run.failed` | `error` | Exception inside spawn / step. |

**Trust, delegation, revocation, identity**

| Type | Carries | When |
| --- | --- | --- |
| `trust.establishing` | `initiator`, `target` | Before `/admin/initiate-handshake`. |
| `trust.established` | `initiator`, `target`, `grants`, `jti` | After successful handshake. |
| `manifest.verify_failed` | `step_id`, `agent_id`, `cause`, `source_url` | `cp_provision_trust_anchor` step: the agent's own manifest failed `aitp.verify_manifest_json` before its key could be pinned into the CP trust store. |
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
| `step.skipped` | `step_id`, `notes` | `meta` step / CP step with no CP. |
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

Agents also emit events through `/internal/telemetry` (see
[agents.md](agents.md) for the agent-emitted event list). Those get
appended to the same run record.

## Cancellation

`POST /runs/{id}/cancel`:
- No-op for runs already in a terminal state.
- Otherwise kills every subprocess for the run via
  `supervisor.kill_run`, marks the record `cancelled`, and emits
  `run.cancelled`.
- The background run task usually fails its next inter-agent HTTP
  call once subprocesses die — but `_finalize_failure` checks the
  store for an already-`cancelled` status before upserting `failed`
  and before emitting `run.failed`, so the cancel handler's upsert
  (which runs first, over HTTP, before the background task's next
  call can fail) is what sticks. `GET /runs/{id}` reports `cancelled`
  deterministically, not "either terminal outcome is acceptable".

The cleanup path in `finally` always runs — bootstrap files are
unlinked, ports released, processes killed. None of it can raise out
of the cleanup itself.

## Things that look weird but aren't

- **`_find_capability_holder` accepts `prefer=`**. This is the
  capability-routing fix mentioned in [scenarios.md](../docs/scenarios.md).
  Without it, `agent: sub-researcher, capability: research.query`
  could route to `researcher` simply because it appeared first in
  `spec.agents`.
- **Reverse handshake is not auto-mirrored** in `handshake` steps. It
  was previously, with no scope narrowing — which broke
  `scoped-capabilities` by overwriting the writer's narrowly-scoped
  TCT with a fully-granted one. If you need both directions, list two
  steps.
- **`_establish_pairwise_trust` does mirror** because eager trust is
  meant to leave every agent holding a TCT for every other. It's only
  used when `spec.trust.eager: true`.
- **Workflow non-2xx is a hard failure**; probes go through a
  dedicated path. The chain can't continue with a 403 as its "result".
- **Background CP event ingest** is fire-and-forget after `run.complete`.
  The asyncio task is tracked on the runner so it can be observed if
  needed (currently used only to keep references alive).
