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

### Event types emitted by the runner

| Type | Carries | When |
| --- | --- | --- |
| `run.started` | `scenario_ref` | After scenario load + input validation. |
| `agent.spawning` | `agent_id`, `port` | Just before `supervisor.launch`. |
| `agent.ready` | `agent_id`, `aid`, `port` | After `AITP_AGENT_READY` line seen. |
| `trust.peers_resolved` | `peers: {agent_id: manifest_url}` | After `TrustOrchestrator.resolve_peers`. |
| `trust.establishing` | `initiator`, `target` | Before `/admin/initiate-handshake`. |
| `trust.established` | `initiator`, `target`, `grants`, `jti` | After successful handshake. |
| `step.started` | `step_id`, `agent`, `capability` | Workflow step about to run. |
| `step.complete` | `step_id`, `result` | Step succeeded. |
| `step.skipped` | `step_id`, `notes` | `meta` step. |
| `step.probing_no_trust` | `initiator`, `target`, `capability` | `capability_call_no_trust` step. |
| `step.probing_with_held_tct` | … | `capability_probe` step. |
| `step.access_denied` | `target`, `capability`, `result.status_code` | Probe matched expected non-2xx. |
| `step.unexpected_status` | … | Probe got a status that didn't match `expect_status`. |
| `delegation.issuing` | `initiator`, `target`, `grants` | `delegate` step. |
| `delegation.redeeming` | `initiator`, `target` | `redeem_delegation` step. |
| `run.complete` | — | Workflow finished cleanly. |
| `run.failed` | `error` | Exception inside spawn / step. |

Agents also emit events through `/internal/telemetry` (see
[agents.md](agents.md) for the agent-emitted event list). Those get
appended to the same run record.

## Cancellation

`POST /runs/{id}/cancel`:
- No-op for runs already in a terminal state.
- Otherwise kills every subprocess for the run via
  `supervisor.kill_run`, then marks the record `cancelled`.
- The background run task usually fails its next inter-agent HTTP
  call once subprocesses die and finalizes the run as failed; the
  cancel handler's `upsert(status=cancelled)` re-promotes that to
  `cancelled`.

The cleanup path in `finally` always runs — bootstrap files are
unlinked, ports released, processes killed. None of it can raise out
of the cleanup itself.

## Things that look weird but aren't

- **`_find_capability_holder` accepts `prefer=`**. This is the
  capability-routing fix mentioned in [scenarios.md](scenarios.md).
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
