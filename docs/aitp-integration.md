# AITP integration

How the playground talks to the AITP SDK, where each protocol moment
lives in the codebase, and why the boundaries are drawn the way they
are.

## The hard rule

All AITP protocol logic — keygen, manifest construction, the handshake
state machine, TCT issuance and verification, delegation, revocation
semantics — lives in the `aitp` Python SDK (built from
`aitp-rs/bindings/aitp-py`). Nothing in this repo parses an envelope,
canonicalizes JSON, or signs anything. If a future change makes you want
to, the SDK is what needs the new API.

The repo only imports `aitp` from inside agent workers
(`agents/base/bootstrap.py`, `agents/base/aitp_server.py`,
`agents/base/agent_admin.py`). The playground service itself never
imports the SDK.

> **This page documents the playground side only** — *where* and *why* the
> SDK is called from this repo. For the SDK call signatures and the protocol
> semantics behind them, read the authoritative sources instead of relying
> on the summaries here:
> - The `aitp` Python API, call by call, with RFC sections + feature flags →
>   [aitp-rs · sdk-python.md](https://github.com/agentidentitytrustprotocol/aitp-rs/blob/main/docs/sdk-python.md).
> - The normative wire protocol →
>   [AITP RFCs](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/blob/main/rfcs/README.md).
>
> Each RFC reference below links the specific spec; if a summary here and an
> RFC ever disagree, the RFC wins.

## Where the SDK is actually called

| SDK call | Caller | Purpose |
| --- | --- | --- |
| `aitp.AitpAgent.from_seed(bytes)` | `agents/base/bootstrap.py` | Build the agent identity from a deterministic seed. |
| `agent.build_manifest(...)` | `agents/base/bootstrap.py` | Construct the AitpManifest JSON served from `/.well-known/aitp-manifest`. |
| `agent.new_responder()` + `process_hello` / `process_commit` | `agents/base/aitp_server.py` | The responder side of the 4-message handshake. |
| `agent.new_session()` + `build_hello`, `process_hello_ack`, `complete` | `agents/base/agent_admin.py` (in `/admin/initiate-handshake`) | The initiator side. |
| `agent.verify_tct(tct_json, required_grant, expected_audience=...)` | `agents/base/aitp_server.py` (`verify_capability_tct`) | Per-call authorization on `/capabilities/<name>`. |
| `agent.build_delegation(held_tct, delegatee_aid, pk, scope, ttl)` | `agents/base/agent_admin.py` (`/admin/delegate`) | Mint a DelegationToken from a held TCT. |
| `aitp.verify_delegation(token_json, my_aid)` | `agents/base/aitp_server.py` (`/aitp/delegation/redeem`) | Verify a presented DelegationToken before issuing a fresh TCT. |
| `agent.issue_tct_for_delegatee(verified)` | `agents/base/aitp_server.py` (`/aitp/delegation/redeem`) | Mint the redeemed TCT bound to the delegatee's key. |

That covers the **core v0.1 surface**. The post-v0.1 / experimental
surfaces below add a handful more calls — all gated behind
`experimental-*` Cargo features and detected at runtime
([capabilities.md](capabilities.md)):

| SDK call | Caller | Purpose |
| --- | --- | --- |
| `agent.new_session(jwks=…, trust_anchors=…)` / `agent.new_responder(jwks=…, …)` | `agent_admin.py`, `aitp_server.py` | OIDC-aware handshake sessions — preload a `JwksProvider` so OIDC peers can be verified. |
| `aitp.JwksProvider(...)` + `aitp.compute_aid_jkt(aid)` | `agents/base/oidc.py`, `trust/oidc_issuer.py` | Verify OIDC ID tokens; bind a token to the agent's key via the `cnf.jkt` claim. |
| `agent.verify_tct_cached(tct_json, grant, store, …)` | `aitp_server.py` (`verify_capability_tct`) | TCT verification with an `aitp.TctStore` cache on the hot path. |
| `agent.build_renewal_request(tct_json)` / `agent.process_renewal_request(req, …)` | `agent_admin.py` (`/admin/renew-tct`, `/admin/process-renewal`) | RFC-AITP-0005 §10 in-band TCT renewal (holder + issuer sides). |
| `aitp.SessionBundleBuilder(agent)` + `aitp.verify_session_bundle(env, aid)` | `agent_admin.py` (`/admin/export…`, `/admin/verify-session-bundle`) | RFC-AITP-0010 session-bundle export + verify. |
| `aitp.verify_delegation_experimental_multihop(token, aid)` | `aitp_server.py` (`/aitp/delegation/redeem`) | RFC-AITP-0011 multi-hop delegation verify (replaces `verify_delegation` when enabled). |
| `aitp.AitpAgent.generate(suite=…)` + `agent.build_manifest(...)` | `aitp_server.py` (`/admin/rotate-keys`) | RFC-AITP-0007 key rotation — fresh keypair + republished manifest. |
| `aitp.compute_spki_hash(der)` + `aitp.SpkiPinVerifier(...)` | engine (`spki_pin_check` step) | SPKI client-cert pin computation + verification. |

Everything else is HTTP plumbing or telemetry. The boundary still holds:
no envelope is parsed, canonicalized, or signed outside the SDK.

## Identity

Each agent's keypair is derived from a deterministic seed:

```
seed_hex = SHA256("<org>:<run_id>:<agent_id>")    # hosting/identity.py
```

- Same run + same agent_id → same AID across restarts. This is the
  reason scenarios can re-run cleanly and tests can assert on AIDs.
- `org: external` agents are derived under a separate namespace, so
  cross-org scenarios produce AIDs that genuinely look like they come
  from a different org.

The seed lands in `bootstrap.aitp.seed_hex`; the worker reads it and
calls `aitp.AitpAgent.from_seed(bytes.fromhex(seed_hex))`.

## Manifest

`agent.build_manifest(...)` returns the JSON served from
`/.well-known/aitp-manifest`. The worker passes:
- `display_name` from the manifest YAML (`spec.aitp.display_name`).
- `handshake_endpoint` = `http://localhost:<port>/aitp/handshake/hello`.
- `offered_caps` from the manifest YAML.
- `ttl_secs` from the manifest YAML.

The wire schema is owned by the SDK
([RFC-AITP-0003](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/blob/main/rfcs/RFC-AITP-0003-manifest.md)).
The playground only fishes `offered_capabilities`, `handshake_endpoint`,
`aid`, and `identity_hint.public_key` out of it when constructing requests.

## Peer discovery (`TrustOrchestrator.resolve_peers`)

Resolves `{agent_id: {manifest_url, did, source?}}` based on the
scenario's `spec.trust.discovery`. The discovery models themselves
(`did:web`, registry lookup) are described in the spec's
[discovery guide](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/blob/main/docs/discovery.md);
the `cp_registry` request/response contract is the CP's
[integration-playground.md](https://github.com/agentidentitytrustprotocol/aitp-control-plane/blob/main/docs/integration-playground.md).
What follows is how the playground *applies* them:

### `static`
Default. Returns `http://localhost:<port>/.well-known/aitp-manifest`
for every peer. Used by most scenarios.

### `did_web`
For each agent that has `did_web_host` set:
1. Build `did:web:<host>` (URL-encoded port).
2. GET `<scheme>://<host>/.well-known/did.json`.
3. Find the `AitpManifest` service entry; append `/.well-known/aitp-manifest`
   to its `serviceEndpoint`.
4. On any failure, fall back to localhost.

The DID document itself is served by the agent's `AitpServer` when
`did_web_host` was passed in the bootstrap (the cross-cloud scenarios
embed `localhost:8101` and friends to keep it on-machine).

### `cp_registry`
For agents marked `org: external`:
1. Query `GET <CP_BASE_URL>/registry/agents?capability=<hint>` where
   the hint is the first workflow capability the runner sees for that
   agent.
2. If the CP responds with anything, take the first result's
   `handshake_endpoint` and derive the manifest URL.
3. If the CP is disabled, empty, or fails, fall back to localhost
   (`source: "static_fallback"`).

CP unavailability is never fatal.

## Handshake

The 4-message mutual handshake is
[RFC-AITP-0004](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/blob/main/rfcs/RFC-AITP-0004-mutual-handshake.md);
the SDK calls that drive it are in
[sdk-python.md § Mutual handshake](https://github.com/agentidentitytrustprotocol/aitp-rs/blob/main/docs/sdk-python.md#mutual-handshake-rfc-aitp-0004).
What's playground-specific is the **HTTP plumbing around those calls** —
which `/admin` and `/aitp` route carries each message:

```
Initiator (caller)                                Responder (callee)
  /admin/initiate-handshake POST ----.            
                                      \           
  session = agent.new_session()        \          
  GET peer /.well-known/aitp-manifest  ─\─────►   200 manifest JSON
  hello = session.build_hello(manifest, grants)
  POST /aitp/handshake/hello (hello) ─────────►   responder = agent.new_responder()
                                                  ack, sid = responder.process_hello(hello)
                                       ◄──────── 200 ack + X-Aitp-Session-Id
                                                  (stash responder under sid)
  commit = session.process_hello_ack(ack, sid)
  POST /aitp/handshake/commit (commit) ───────►   responder.process_commit(commit)
                                                  → (final_ack, tct_json)
                                                  emit handshake.complete (responder)
                                       ◄──────── 200 final_ack
  tct_json = session.complete(final_ack)
  held_tcts[peer_port] = tct_json
  emit handshake.complete (initiator)
```

Only the **initiator** receives a TCT it can present back to the
responder. To run the reverse direction the runner triggers
`/admin/initiate-handshake` on the other agent. The
`_establish_pairwise_trust` helper does both directions for every pair
when `spec.trust.eager: true`.

For scenarios that opt out of eager handshakes, explicit `handshake`
steps run one direction at a time and can carry `requested_grants` to
scope the TCT.

## TCTs and capability authorization

When a peer call hits `/capabilities/<name>`, the worker calls
`server.verify_capability_tct(tct_json, "<name>")`:

```python
# agents/base/aitp_server.py
def verify_capability_tct(self, tct_json, required_grant):
    if not tct_json: raise 403 "missing X-AITP-TCT"
    tct_obj = json.loads(tct_json)["tct"]
    jti = tct_obj.get("jti", "")
    if jti and jti in self.revoked_jtis:
        raise 403 f"tct revoked: jti={jti}"
    declared_audience = tct_obj.get("audience")
    return self.agent.verify_tct(
        tct_json, required_grant,
        expected_audience=declared_audience,
    )
```

Two checks:

1. **Local revocation short-circuit** (playground choice). The spec
   ([RFC-AITP-0008](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/blob/main/rfcs/RFC-AITP-0008-revocation.md))
   places the revocation check *after* signature verification so a forged
   jti can't probe the deny set. The demo checks first — every jti in our
   deny set was observed via a prior handshake, so the early-out is safe
   and cheaper here. (See [Revocation](#revocation-rfc-aitp-0008) below.)
2. **SDK `verify_tct`, presented-TCT mode.** The playground passes the
   TCT's own declared `audience` as `expected_audience` — the
   resource-server check for a TCT a peer presented in `X-AITP-TCT`. The
   two verification models (holder-receipt vs presented-TCT) and what the
   audience asserts are documented in
   [sdk-python.md § TCT verification](https://github.com/agentidentitytrustprotocol/aitp-rs/blob/main/docs/sdk-python.md#tct-verification-rfc-aitp-0005-9)
   and [RFC-AITP-0005](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/blob/main/rfcs/RFC-AITP-0005-tct.md).

Any failure produces a 403. The two parse failures (missing token,
malformed JSON) are reported distinctly so debugging is easier.

## Held TCTs

Each agent process holds a dict `held_tcts: {peer_port: tct_json}`
populated by `/admin/initiate-handshake` and by
`/admin/redeem-delegation`. The map is module-scoped — all requests
in this process see the same set.

`/admin/invoke` looks up `held_tcts[peer_port]` and attaches it as
`X-AITP-TCT` on the request to `/capabilities/<name>`. If the held
TCT was revoked or expired, the peer's `verify_tct` will 403; the
admin router wraps that into `{error:true, status_code: 403, body}`
so probe steps can observe it without crashing the run.

## Delegation (RFC-AITP-0006)

Delegation semantics — scope narrowing, the `cnf` key binding, redemption —
are [RFC-AITP-0006](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/blob/main/rfcs/RFC-AITP-0006-delegation.md)
and [sdk-python.md § Delegation](https://github.com/agentidentitytrustprotocol/aitp-rs/blob/main/docs/sdk-python.md#delegation-rfc-aitp-0006).
The playground-specific part is the HTTP choreography across two agents:

Single-hop delegation flow:

```
delegator (researcher)            delegatee (sub-researcher)        verifier (writer)
  holds TCT_AB issued by                                            
  writer for {write.content}                                        
  /admin/delegate                                                   
    build_delegation(TCT_AB,                                        
      sub.aid, sub.public_key,                                      
      scope=[write.content], ttl)                                   
    → DelegationToken (DT)                                          
  ── returns DT ──►          (DT in hand)
                              /admin/redeem-delegation              
                                POST DT to writer /aitp/delegation/redeem ──►
                                                                    verify_delegation(DT, writer.aid)
                                                                    issue_tct_for_delegatee(verified)
                                                                    → TCT_BC bound to sub's key
                              ◄── fresh TCT_BC ──────────────────── 
                              held_tcts[writer_port] = TCT_BC      
                              /admin/invoke (writer, write.content)
                                presents TCT_BC ──────────────────► verify_capability_tct(TCT_BC, "write.content")
                                                                    ✓
```

Playground-relevant notes (the SDK enforces the rules; the playground just
sequences the calls):
- `/admin/delegate` feeds the delegator's *held* TCT into
  `build_delegation`; the SDK rejects a `scope` wider than that TCT's
  grants, so a scenario can only narrow.
- `/aitp/delegation/redeem` only issues if the presenting party matches
  the token's `delegator` — an agent can't redeem a chain it never
  authored.
- The redeemed TCT lands in the delegatee's `held_tcts[target_port]`, so
  the next `/admin/invoke` from delegatee → target presents it
  automatically.

## Revocation (RFC-AITP-0008)

Revocation is local to the issuer:

1. The runner's `revoke_tct` step finds the jti of the TCT `issuer`
   granted to `audience` by walking the event log
   (`ScenarioRunner._find_tct_jti`) and POSTs it to the issuer's
   `/admin/revoke-tct`.
2. The issuer adds it to `revoked_jtis` (mutates the same set
   `AitpServer` consults).
3. Subsequent capability calls that present that jti hit the local
   revocation short-circuit and 403.

By default no revocation list is published over the wire — local deny-set
fail-closed is all the base demo needs
([RFC-AITP-0008](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/blob/main/rfcs/RFC-AITP-0008-revocation.md)
defines the signed-list distribution model). The `revoke_tct` step's
`via_cp: true` mode *does* exercise the full path — publish to the Control
Plane and have the audience pull the CP's signed
`/.well-known/aitp-revocation-list`; see
[control-plane.md](control-plane.md#cp-backed-workflow-steps).

## Post-v0.1 experimental surfaces

Each surface is gated behind an SDK `experimental-*` Cargo feature and
reported by `GET /capabilities`; scenarios degrade cleanly when the wheel
lacks one ([capabilities.md](capabilities.md)). The SDK mechanics for all
of these are in
[sdk-python.md § Experimental surface](https://github.com/agentidentitytrustprotocol/aitp-rs/blob/main/docs/sdk-python.md);
below is only **what the playground wires up** and **which scenario shows
it**.

| Surface | Playground wiring (the part that's ours) | Scenario | Spec |
| --- | --- | --- | --- |
| **OIDC identity** | The engine mints a per-run **mock OIDC issuer** (`trust/oidc_issuer.py`) and threads its key material through every bootstrap; OIDC agents sign via an `oidc_mint_jwt` callback, pinned-key agents still get a `JwksProvider` to verify OIDC peers. Real deployments swap in an external IdP. | `intra-org/oidc-identity` (+ `p256-suite` template) | [RFC-AITP-0002](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/blob/main/rfcs/RFC-AITP-0002-identity.md) · [sdk-python.md](https://github.com/agentidentitytrustprotocol/aitp-rs/blob/main/docs/sdk-python.md#oidc-identity-rfc-aitp-0002) |
| **Key rotation** | `/admin/rotate-keys` regenerates the key + republishes the manifest; `verify_capability_tct`'s **issuer-AID guard** then rejects TCTs minted under the old AID before the SDK is consulted. | `intra-org/key-rotation` | [RFC-AITP-0007](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/blob/main/rfcs/RFC-AITP-0007-key-resolution.md) |
| **TCT renewal** | Holder's `/admin/renew-tct` → issuer's `/admin/process-renewal`; the holder swaps its held TCT in place. | `intra-org/tct-renewal` | [RFC-AITP-0013](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/blob/main/rfcs/RFC-AITP-0013-tct-renewal-extension.md) |
| **TCT verification cache** | When `aitp.TctStore` exists, `verify_capability_tct` routes through `verify_tct_cached`; `tct_cache_stats` exposes hit/miss counters. | `intra-org/tct-cache-perf` | [RFC-AITP-0005](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/blob/main/rfcs/RFC-AITP-0005-tct.md) |
| **Session bundles** | A coordinator's `/admin/export-session-bundle` packages the TCTs it issued; a verifier's `/admin/verify-session-bundle` returns the `BundleOutcome`. | `intra-org/session-bundle` | [RFC-AITP-0010](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/blob/main/rfcs/RFC-AITP-0010-session-trust-bundle.md) |
| **Multi-hop delegation** | The redeem endpoint swaps `verify_delegation` for `verify_delegation_experimental_multihop` when available. | `intra-org/delegation-multihop` | [RFC-AITP-0011](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/blob/main/rfcs/RFC-AITP-0011-multihop-delegation.md) |
| **SPKI pinning** | A pure-SDK `spki_pin_check` step — no agent involved. | `intra-org/spki-pinning` | [sdk-python.md](https://github.com/agentidentitytrustprotocol/aitp-rs/blob/main/docs/sdk-python.md) |

## What you can ignore (boundary check)

If you find yourself wanting to do any of these in this repo, the SDK
should be doing it instead:

- Parse a TCT envelope to inspect grants (the runner only reads `jti`
  for revocation lookup; everything else routes through `verify_tct`).
- Canonicalize JSON for signing.
- Verify a signature.
- Build any AITP message by hand.
- Track handshake state across multiple requests (the responder map
  in `AitpServer._sessions` is keyed by `session_id` from the SDK,
  not state we own).

The playground exists to drive scenarios; the SDK exists to enforce
the protocol. Keep the wall solid.
