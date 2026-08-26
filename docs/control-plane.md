# Control Plane integration

The **Control Plane** (CP) is an optional, external service
(`aitp-control-plane`, a Next.js app) that the playground can talk to for
agent discovery, audit ingestion, revocation propagation, webhook
fan-out, and trust-anchor configuration. Everything here is **best-effort
and optional** — the service runs fully without a CP, and every CP call
has a graceful fallback. This page is the map of what the playground does
with a CP when one is wired up.

> The hard invariant: **no CP call is ever load-bearing.** When
> `CP_BASE_URL` is empty (the default), discovery returns `[]`, ingest is
> a no-op, observability proxies return `cp_enabled: false`, and CP-only
> workflow steps emit `step.skipped`. See
> [architecture.md](architecture.md) for where this sits.

> **The CP is a separate service with its own docs — this page does not
> restate them.** Endpoint request/response shapes, auth, idempotency, the
> event envelope, and the database schema live in the control-plane repo:
> [api.md](https://github.com/agentidentitytrustprotocol/aitp-control-plane/blob/main/docs/api.md) ·
> [events.md](https://github.com/agentidentitytrustprotocol/aitp-control-plane/blob/main/docs/events.md) ·
> [data-model.md](https://github.com/agentidentitytrustprotocol/aitp-control-plane/blob/main/docs/data-model.md).
> The stable contract the playground actually depends on (and what the CP
> may/may not change without coordination) is the CP's
> [integration-playground.md](https://github.com/agentidentitytrustprotocol/aitp-control-plane/blob/main/docs/integration-playground.md).
> This page is only the **playground side**: which playground feature calls
> which CP endpoint, and what happens when the CP is absent.

## Enabling it

```bash
CP_BASE_URL=http://localhost:4000     # the CP's base URL; empty disables everything here
CP_API_KEY=<bearer>                   # optional; sent as Authorization: Bearer on write/observability calls
CP_TIMEOUT_MS=5000                    # per-request timeout (default 5000)
```

`CpClient.enabled` is simply `bool(settings.cp_base_url)`. The Dockerized
e2e suite (`docker-compose.test.yml`) brings up a Postgres + CP + playground
stack and points `CP_BASE_URL` at the in-network CP container — see
[docker.md](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/internal_docs/docker.md).

## Two clients, one CP

CP traffic originates from **two** places, and it's worth keeping them
straight:

1. **`CpClient`** (`src/aitp_playground/cp_client/client.py`) — the
   *service* talks to the CP for discovery, event ingest, revocation
   publish, webhook management, and all the read-only observability
   projections surfaced under `/cp/*`.
2. **Agent workers** (`agents/base/agent_admin.py`) — an agent talks to
   the CP *directly* for self-enrollment (`enroll_with_cp`) and to pull
   the revocation list (`refresh-revocations`) — the CP signs it, and the
   playground does not yet verify that signature (`PENDING.md` P8). The playground
   never enrolls on an agent's behalf; it pokes the agent's `/admin`
   route and the agent makes the call itself (the same boundary rule as
   the AITP protocol — see [aitp-integration.md](aitp-integration.md)).

## CP endpoints the playground uses

Every method below lives on `CpClient` and degrades to the documented
fallback when the CP is disabled or any HTTP error occurs. This table is
the playground's **dependency surface** — for each endpoint's exact
request/response shape, auth, and filters, follow it into the CP's
[api.md](https://github.com/agentidentitytrustprotocol/aitp-control-plane/blob/main/docs/api.md).

| `CpClient` method | CP endpoint | Used by | Fallback |
| --- | --- | --- | --- |
| `discover_by_capability(cap)` | `GET /api/registry/agents?capability=` | `cp_registry` discovery | `[]` → static localhost |
| `ingest_events(events)` | `POST /api/events` | end of every run (background); `cp_delegation_tree` (awaited, mid-run) | no-op, logged |
| `publish_revocation(jti, reason)` | `POST /api/revocation/entries` | `revoke_tct` with `via_cp` | `False` |
| `fetch_revocation_list()` | `GET /.well-known/aitp-revocation-list` | (agent side mirrors this) | `[]` |
| `create_webhook(url, events, secret)` | `POST /api/webhooks` | `cp_subscribe_webhook` | `None` |
| `delete_webhook(id)` | `DELETE /api/webhooks/{id}` | cleanup | `True` (404 = success) |
| `fetch_events_history(...)` | `GET /api/events/history` | `GET /runs/{id}/cp-audit` | `[]` |
| `fetch_sessions(...)` | `GET /api/sessions` | `GET /runs/{id}/cp-sessions` | `[]` |
| `fetch_tcts(...)` | `GET /api/tcts` | `GET /cp/tcts` | `[]` |
| `fetch_delegations(...)` | `GET /api/delegations` | `GET /cp/delegations`, `cp_delegation_tree` | `[]` |
| `replay_session(id, ...)` | `GET /api/sessions/{id}/replay` | `GET /cp/sessions/{id}/replay` | `[]` |
| `fetch_dashboard_overview(window)` | `GET /api/dashboard/overview` | `GET /cp/dashboard` | `{}` |
| `fetch_dashboard_agents()` | `GET /api/dashboard/agents` | `GET /cp/agents` | `[]` |
| `list_trust_anchors(ns)` | `GET /api/trust-anchors` | `GET /cp/trust-anchors` | `[]` |
| `list_pinned_keys(ns)` | `GET /api/pinned-keys` | `GET /cp/pinned-keys` | `[]` |
| `upsert_trust_anchor(...)` | `POST /api/trust-anchors` | `cp_provision_trust_anchor` | `None` |
| `upsert_pinned_key(...)` | `POST /api/pinned-keys` | `cp_provision_trust_anchor` | `None` |

The list-fetch methods are tolerant of envelope shape — they accept both
`{items: [...]}` / `{events: [...]}` and a bare top-level list, so they
keep working across CP response-shape tweaks.

## Discovery (`cp_registry`)

When a scenario sets `spec.trust.discovery: cp_registry`, the
`TrustOrchestrator` resolves peers marked `org: external` through the CP:

1. Derive a capability hint — the first workflow capability the runner
   sees targeted at that agent.
2. `GET /api/registry/agents?capability=<hint>`.
3. If the CP returns anything, take the first result's
   `handshake_endpoint`, derive the manifest URL, and tag the peer
   `source: cp_registry`.
4. On empty result, disabled CP, or any error, fall back to
   `http://localhost:<port>` and tag `source: static_fallback`.

`cross-org/federated-analysis@1.0.0` is the worked example. Run it without
a CP and it transparently falls back to static localhost — the handshake
still completes.

## CP-backed workflow steps

These step types only do CP work; each emits `step.skipped` when the CP is
disabled. Full field reference is in [scenarios.md](scenarios.md#workflow-steps).

| Step type | What it does | Demo scenario |
| --- | --- | --- |
| `enroll_with_cp` | Agent self-enrolls: `POST /api/registry/enroll` to mint a one-time bearer token, then `POST /api/registry/agents` with that token + its manifest. | `intra-org/external-enrollment` |
| `revoke_tct` (`via_cp: true`) | Local revoke **plus** `POST /api/revocation/entries`, then the audience pulls the updated list from `/.well-known/aitp-revocation-list`. The CP signs that snapshot; the playground does not yet check the signature (`aitp-playground#46`, `PENDING.md` P8) — so today the audience trusts the transport, not the issuer. | `intra-org/revocation-via-cp` |
| `cp_subscribe_webhook` | `POST /api/webhooks` pointing at this run's `/webhooks/cp/{run_id}` receiver; stores the returned secret on the run record for HMAC verification. | `intra-org/webhook-subscription` |
| `cp_provision_trust_anchor` | `upsert_pinned_key` + optional `upsert_trust_anchor` (OIDC issuer) for an agent under a namespace, then reads them back. | `intra-org/cp-trust-anchor-provisioning` |
| `cp_delegation_tree` | Flushes the run's events to the CP (awaiting the ingest, so the projection is populated mid-run), then walks a delegator's chain via `GET /api/delegations` (CP's recursive `root_jti` query) to show the chain as the CP observed it. | `intra-org/cp-delegation-tree` |

## Webhooks (reverse fan-out)

`cp_subscribe_webhook` is the only place the CP calls *back into* the
playground. The flow:

1. The step registers a webhook at the CP whose URL is
   `<PLAYGROUND_BASE_URL>/webhooks/cp/{run_id}`.
2. The CP responds with a webhook `id` and a `secret`. The playground
   stores the secret on the run record (and strips it from API responses).
3. As CP-side events occur, the CP `POST`s them to
   `/webhooks/cp/{run_id}` with headers `X-Aitp-Signature: sha256=<hex>`,
   `X-Aitp-Event`, `X-Aitp-Delivery`.
4. The receiver (`api/webhooks.py`) verifies the HMAC-SHA256 signature
   against the stored secret (constant-time compare). Valid deliveries
   append a `cp.webhook.delivered` event to the run log; missing/invalid
   signatures return `401`, unknown runs `404`.

Inspect what arrived with `GET /runs/{id}/cp-deliveries` (the run's
webhook config, secret stripped, plus the delivered events).

## Observability projections

Two families of read-only endpoints surface what the CP knows. All return
`cp_enabled: false` with an empty payload when no CP is configured — safe
to call unconditionally from a dashboard.

**Run-scoped** (filtered to one run):

| Endpoint | CP source | Shows |
| --- | --- | --- |
| `GET /runs/{id}/cp-audit` | `/api/events/history?run_id=` | audit events the CP recorded for this run |
| `GET /runs/{id}/cp-sessions` | `/api/sessions?run_id=` | handshake sessions for this run |
| `GET /runs/{id}/cp-deliveries` | (local) | webhook deliveries received for this run |

**Entity-scoped** (across the CP, not one run):

| Endpoint | CP source | Shows |
| --- | --- | --- |
| `GET /cp/tcts` | `/api/tcts` | TCTs the CP has observed (filter by issuer/subject/audience/capability/session) |
| `GET /cp/delegations` | `/api/delegations` | delegation chains (`root_jti` walks the whole tree) |
| `GET /cp/sessions/{sid}/replay` | `/api/sessions/{sid}/replay` | ordered event stream for one session |
| `GET /cp/dashboard` | `/api/dashboard/overview` | aggregate CP metrics for a time window |
| `GET /cp/agents` | `/api/dashboard/agents` | per-agent CP metrics |
| `GET /cp/trust-anchors` | `/api/trust-anchors` | configured OIDC issuer bindings |
| `GET /cp/pinned-keys` | `/api/pinned-keys` | registered pinned keys |

## Event ingest

After every run reaches `run.complete`, the runner fires a background task
that `POST`s the full run event log to the CP's `/api/events`
(`CpClient.ingest_events`). This is fire-and-forget: a CP outage never
fails a run, and the task is only tracked so its reference stays alive.

What gets ingested is the **store's** event log for the run, not just the
orchestrator's own events. That distinction matters: the agent
subprocesses deliver their canonical events (`handshake.complete`,
`delegation.issued`/`delegation.redeemed`, `tct.*`) to
`POST /internal/telemetry`, which lands them in the store — and those are
exactly the events the CP projects sessions, TCTs, and delegations from.
(The `delegation.issued` telemetry also carries the signed delegation
token, so the CP's delegation projection can parse `jti`/`src_jti`.) The
one mid-run exception is the `cp_delegation_tree` step, which flushes the
run's events to the CP and awaits the ingest *before* querying the
delegation projection — otherwise the query would race the post-run batch
and always see an empty tree. CP projections are idempotent, so the
post-run batch re-sending those events creates no duplicates.

Which event types the CP recognizes vs. merely stores, and which it fans
out to webhooks, is the CP's
[events.md](https://github.com/agentidentitytrustprotocol/aitp-control-plane/blob/main/docs/events.md).

## What lives where (boundary check)

- The playground **never** signs revocation lists, mints CP bearer tokens,
  or canonicalizes CP payloads — agents and the CP own that. The
  playground only orchestrates *when* those calls happen.
- It also does not yet **verify** the revocation snapshot it consumes. See
  `aitp-playground#46`; it needs `aitp.verify_revocation_list`, arriving in
  `aitp-sdk` 0.6.0 (`aitp-rs#90`, still open).
- The CP is a separate repo (`aitp-control-plane`); its API contract is
  the source of truth for the endpoint shapes above. The
  envelope-tolerant parsing in `CpClient` exists precisely so small CP
  shape changes don't break the demo. **That tolerance becomes a liability
  the moment the signature is checked** — a response shape the parser
  accepts but the verifier did not sign over is a downgrade path. Tighten
  the parse to the exact RFC-AITP-0008 §1.5 envelope in the same change that
  turns verification on, rather than leaving two accepted shapes.

## Where to read next

- The CP itself (API, events, data model, ops, deploy) → [aitp-control-plane · docs](https://github.com/agentidentitytrustprotocol/aitp-control-plane/blob/main/docs/README.md)
- The stable playground↔CP contract → [integration-playground.md](https://github.com/agentidentitytrustprotocol/aitp-control-plane/blob/main/docs/integration-playground.md)
- Trust discovery internals → [aitp-integration.md](aitp-integration.md#peer-discovery-trustorchestratorresolve_peers)
- Webhook/event surfacing in the UI → [observability.md](observability.md)
- The CP-backed scenarios end-to-end → [scenarios.md](scenarios.md#scenarios-in-the-box)
