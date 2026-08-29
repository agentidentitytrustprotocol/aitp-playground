# AITP integration

How the playground talks to the AITP SDK, where each protocol moment
lives in the codebase, and why the boundaries are drawn the way they
are.

## The hard rule

All AITP protocol logic — keygen, manifest construction, the handshake
state machine, TCT issuance and verification, delegation, revocation
semantics — lives in the `aitp` Python SDK (PyPI distribution
`aitp-sdk`, built from `aitp-rs/bindings/aitp-py`). Nothing in this repo
parses an envelope,
canonicalizes JSON, or signs anything. If a future change makes you want
to, the SDK is what needs the new API.

The repo imports `aitp` almost exclusively from inside agent workers
(`agents/base/bootstrap.py`, `agents/base/aitp_server.py`,
`agents/base/agent_admin.py`, `agents/base/oidc.py`). The playground
service touches the SDK in exactly two places, neither of which holds
protocol state: the feature probe (`capabilities.py`, behind
`GET /capabilities`) and the pure-SDK `spki_pin_check` step in the
engine.

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
| `agent.verify_tct(tct_token, required_grant, expected_audience=…, revoked_jtis=…)` | `agents/base/aitp_server.py` (`verify_capability_tct`) | Per-call authorization on `/capabilities/<name>`. |
| `agent.build_delegation(held_tct, delegatee_aid, pk, scope, ttl)` | `agents/base/agent_admin.py` (`/admin/delegate`) | Mint a DelegationToken from a held TCT. |
| `aitp.verify_delegation(token_json, my_aid, revoked_jtis)` | `agents/base/aitp_server.py` (`/aitp/delegation/redeem`) | Verify a presented DelegationToken before issuing a fresh TCT. The deny-set argument is RFC-AITP-0006 §4 step 7 — a revoked source jti MUST refuse a fresh TCT. |
| `agent.issue_tct_for_delegatee(verified)` | `agents/base/aitp_server.py` (`/aitp/delegation/redeem`) | Mint the redeemed TCT bound to the delegatee's key. |

That covers the **core surface**. The post-v0.1 surfaces below add a
handful more calls — all shipped by default since `aitp-sdk` 0.4.0, but
still probed at runtime so older or `--no-default-features` wheels
degrade cleanly ([capabilities.md](capabilities.md)):

| SDK call | Caller | Purpose |
| --- | --- | --- |
| `agent.new_session(jwks=…, trust_anchors=…)` / `agent.new_responder(jwks=…, …)` | `agent_admin.py`, `aitp_server.py` | OIDC-aware handshake sessions — preload a `JwksProvider` so OIDC peers can be verified. |
| `aitp.JwksProvider(...)` + `aitp.compute_aid_jkt(aid)` | `agents/base/oidc.py`, `trust/oidc_issuer.py` | Verify OIDC ID tokens; bind a token to the agent's key via the `cnf.jkt` claim. |
| `agent.verify_tct_cached(tct_token, grant, store, …)` | `aitp_server.py` (`verify_capability_tct`) | TCT verification with an `aitp.TctStore` cache on the hot path. |
| `agent.build_renewal_request(tct_token)` / `agent.process_renewal_request(req, …)` | `agent_admin.py` (`/admin/renew-tct`, `/admin/process-renewal`) | RFC-AITP-0013 in-band TCT renewal (holder + issuer sides). |
| `aitp.SessionBundleBuilder(agent)` + `aitp.verify_session_bundle(env, aid)` | `agent_admin.py` (`/admin/export…`, `/admin/verify-session-bundle`) | RFC-AITP-0010 session-bundle export + verify. |
| `aitp.verify_manifest_json(envelope)` | `agent_admin.py` (`/admin/initiate-handshake`, `/admin/delegate`), `runner/engine.py` (`cp_provision_trust_anchor`) | Verify a peer `ManifestEnvelope` before reading the AID or endpoint out of it. |
| `aitp.verify_delegation_multihop(token, aid, max_hops, revoked_jtis)` | `aitp_server.py` (`/aitp/delegation/redeem`) | RFC-AITP-0011 multi-hop delegation verify (replaces `verify_delegation` when enabled). `revoked_jtis` is consulted once for the root voucher's `src_jti` and once per hop (RFC-AITP-0011 §6). |
| `aitp.AitpAgent.generate(suite=…)` + `agent.build_manifest(...)` | `aitp_server.py` (`/admin/rotate-keys`) | RFC-AITP-0007 key rotation — fresh keypair + republished manifest. |
| `aitp.compute_spki_hash(der)` + `aitp.SpkiPinVerifier(...)` | engine (`spki_pin_check` step) | SPKI client-cert pin computation + verification. |

Everything else is HTTP plumbing or telemetry. The boundary is: **nothing
security-relevant is decided outside the SDK** — no envelope is canonicalized
or signed here, and every trust decision routes through an SDK verify call.

That is deliberately narrower than "no envelope is parsed outside the SDK",
which this repo said until it stopped being true. JSON *is* read outside the
SDK in three places, and each is fine only because the SDK made the decision
first or the value is not load-bearing:

- peer manifests are `json.loads`'d after `verify_manifest_json` has verified
  the envelope (`agent_admin.py`, `runner/engine.py`);
- TCT claims are decoded unverified by `tct_claims.decode_claims` for a precise
  403, an issuer guard, and the declared audience — `verify_tct` is still the
  gate (see "What you can ignore" below);
- the revocation snapshot is parsed only after `aitp.verify_revocation_list`
  has verified the envelope against the pinned issuer AID
  (`refresh_revocations()` in `agents/base/revocation_refresh.py`).

Parsing is not the property that matters; *deciding* is.

**The revocation snapshot has one ingest, and it verifies.** The snapshot
served at `/.well-known/aitp-revocation-list` is fetched and checked in exactly
one place — `refresh_revocations()` in `agents/base/revocation_refresh.py`.
Three callers reach it: the `/admin/refresh-revocations` route, the start-up
refresh, and the background poll — the latter two in-process by design, never
looping back over HTTP. It calls `aitp.verify_revocation_list`
against the AID pinned in `CP_AID` before reading a single entry; a snapshot
that fails to verify is **discarded**, and the previously verified one stays
current (RFC-AITP-0008 §1.5). Absent a pin there is no expected issuer to check
against, so the snapshot is discarded too — the fail-closed direction. The
deny-set is no longer only as trustworthy as the transport that delivered it.

`RevocationState` (`agents/base/revocation_state.py`) only *holds* that
decision; it verifies nothing itself, deliberately, so it cannot become a
second hand-rolled trust boundary.

`CpClient` has no counterpart. It carried a `fetch_revocation_list()` that
parsed the envelope signature-blind, and it was deleted rather than taught to
verify: nothing in `src/` called it, and two ingest paths for the same signed
artifact is the condition that let the signature-blind version survive in the
first place. If service-side code ever needs the deny-set, it goes through the
verifying path — it does not grow a second one.

Peer **manifest signatures** were the same shape of gap and are now checked at
all three sites that ingest one: the handshake (`/admin/initiate-handshake`),
delegation (`/admin/delegate`), and the runner before it pins a key into the
CP's trust store (`runner/engine.py`). Each calls `aitp.verify_manifest_json`
before reading any field out of the envelope.

Note precisely what that establishes: the envelope was minted by the holder of
the AID it declares. It is **self-certifying, not trust-anchored** — it does not
prove that AID is the peer you meant. Two consequences worth keeping straight:

- For `did:web`, the DID document supplies the manifest **endpoint**
  (`serviceEndpoint`), not a key or an AID (`trust/resolver.py`). So the chain
  is DID → endpoint → whatever manifest that endpoint serves; and the federated
  stack resolves that first hop over plain HTTP under
  `AITP_DIDWEB_INSECURE_HOSTS`.
- At the runner's CP trust-anchor site the expected AID **is** known — the
  runner launched the agent — and simply is not compared yet. That is a missing
  one-line check, not an inherent limit of `verify_manifest_json`. Tracked as
  `PENDING.md` P1.

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
1. Query `GET <CP_BASE_URL>/api/registry/agents?capability=<hint>` where
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
                                                  → (final_ack, tct_token)
                                                  emit handshake.complete (responder)
                                       ◄──────── 200 final_ack
  tct_token = session.complete(final_ack)
  held_tcts[peer_port] = tct_token
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

Under protocol `aitp/0.2` the `X-AITP-TCT` header carries an opaque
**compact-JWS token**. When a peer call hits `/capabilities/<name>`, the
worker calls `server.verify_capability_tct(tct_token, "<name>")`, which
in essence does:

```python
# agents/base/aitp_server.py (condensed)
def verify_capability_tct(self, tct_token, required_grant):
    if not tct_token: raise 403 "missing X-AITP-TCT"
    claims = decode_claims(tct_token)        # unverified claims, for precise 403s
    jti = claims.get("jti", "")
    if jti and self.revocation.is_revoked(jti):
        source = "local" if self.revocation.is_locally_revoked(jti) else "cp-snapshot"
        raise 403 f"tct revoked ({source}): jti={jti}"
    self._enforce_revocation_freshness()     # 403 unless fail_mode == "soft_fail"
    if claims.get("iss") and claims["iss"] != self.agent.aid:
        raise 403 "tct issuer mismatch"      # e.g. issued pre-key-rotation
    declared_audience = claims.get("aud") or claims.get("sub")
    return self.agent.verify_tct(            # or verify_tct_cached(...) with a TctStore
        tct_token, required_grant,
        expected_audience=declared_audience,
        revoked_jtis=self.revocation.effective_jtis,
    )
```

Four checks before/inside the SDK call:

1. **Local revocation short-circuit** (playground choice). The spec
   ([RFC-AITP-0008](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/blob/main/rfcs/RFC-AITP-0008-revocation.md))
   places the revocation check *after* signature verification so a forged
   jti can't probe the deny set. The demo checks first — every jti in our
   deny set was observed via a prior handshake, so the early-out is safe
   and gives a precise 403. It's fail-closed either way: the same
   union — `revocation.effective_jtis`, local revocations ∪ the verified
   CP snapshot — is also passed into the SDK as `revoked_jtis`, which
   re-checks it after signature verification. The 403 names its source
   (`local` vs `cp-snapshot`), because "we revoked this" and "the control
   plane says someone revoked this" send an operator to different places.
   (See [Revocation](#revocation-rfc-aitp-0008) below.)
2. **Revocation-freshness guard.** Checked *after* the deny-set, so a
   genuine revocation keeps its own reason. If revocation verification is
   configured but there is no fresh verified snapshot, the call is refused
   under the default `fail_mode="fail_closed"`; `soft_fail` proceeds on the
   last verified deny-set and logs. This is only about the *absence* of a
   fresh snapshot — an unverifiable one was already discarded at ingest and
   no mode here can resurrect it.
3. **Issuer-AID guard.** TCTs this resource server issued must declare
   *its* AID as `iss`. After a key rotation the AID changes, so TCTs
   minted under the old key fail here before the signature path runs.
4. **SDK `verify_tct`, presented-TCT mode.** The playground passes the
   TCT's own declared `aud` (defaulting to `sub`) as `expected_audience` —
   the resource-server check for a TCT a peer presented in `X-AITP-TCT`.
   The signature check against the issuer key derived from `iss` is the
   security gate. The two verification models (holder-receipt vs
   presented-TCT) and what the audience asserts are documented in
   [sdk-python.md § TCT verification](https://github.com/agentidentitytrustprotocol/aitp-rs/blob/main/docs/sdk-python.md#tct-verification-rfc-aitp-0005-9)
   and [RFC-AITP-0005](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/blob/main/rfcs/RFC-AITP-0005-tct.md).
   When the wheel exposes `aitp.TctStore`, the same call routes through
   `verify_tct_cached` so repeated verifications hit the cache.

Any failure produces a 403. The two parse failures (missing token,
malformed token) are reported distinctly so debugging is easier.

## Held TCTs

Each agent process holds a dict `held_tcts: {peer_port: tct_token}`
(compact-JWS strings) populated by `/admin/initiate-handshake` and by
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
                                                                    verify_delegation(DT, writer.aid,
                                                                      deny_set)
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

Revocation starts local to the issuer, and a signed snapshot can add to it:

1. The runner's `revoke_tct` step finds the jti of the TCT `issuer`
   granted to `audience` by walking the event log
   (`ScenarioRunner._find_tct_jti`) and POSTs it to the issuer's
   `/admin/revoke-tct`.
2. The issuer records it as a **local** revocation on the shared
   `RevocationState` that `AitpServer` enforces against. Local revocations
   are held apart from the CP-derived snapshot and are never cleared by a
   refresh — a CP snapshot cannot un-revoke what an operator revoked here.
3. Subsequent capability calls that present that jti hit the local
   revocation short-circuit and 403.

By default no revocation list is published over the wire — local deny-set
fail-closed is all the base demo needs
([RFC-AITP-0008](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/blob/main/rfcs/RFC-AITP-0008-revocation.md)
defines the signed-list distribution model). The `revoke_tct` step's
`via_cp: true` mode exercises the **data** path — publish to the Control Plane
and have an unrelated peer pull `/.well-known/aitp-revocation-list` into its
deny-set. The snapshot's signature *is* checked before any entry is applied. What the
scenario still does not show, as its own summary says, is a step driving a call
whose outcome depends on the CP-derived deny-set — the final 403 comes from the
issuer's *local* set. See
[control-plane.md](control-plane.md#cp-backed-workflow-steps).

## Post-v0.1 experimental surfaces

These surfaces ship **by default** on the published `aitp-sdk` wheel
(since 0.4.0); each is still probed and reported by `GET /capabilities`,
and scenarios degrade cleanly when an older or `--no-default-features`
wheel lacks one ([capabilities.md](capabilities.md)). The SDK mechanics
for all of these are in
[sdk-python.md § Additional capabilities](https://github.com/agentidentitytrustprotocol/aitp-rs/blob/main/docs/sdk-python.md#additional-capabilities-on-by-default);
below is only **what the playground wires up** and **which scenario shows
it**.

| Surface | Playground wiring (the part that's ours) | Scenario | Spec |
| --- | --- | --- | --- |
| **OIDC identity** | The engine mints a per-run **mock OIDC issuer** (`trust/oidc_issuer.py`) and threads its key material through every bootstrap; OIDC agents sign via an `oidc_mint_jwt` callback, pinned-key agents still get a `JwksProvider` to verify OIDC peers. Real deployments swap in an external IdP. | `intra-org/oidc-identity` (+ `p256-suite` template) | [RFC-AITP-0002](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/blob/main/rfcs/RFC-AITP-0002-identity.md) · [sdk-python.md](https://github.com/agentidentitytrustprotocol/aitp-rs/blob/main/docs/sdk-python.md#oidc-identity-rfc-aitp-0002) |
| **Key rotation** | `/admin/rotate-keys` regenerates the key + republishes the manifest; `verify_capability_tct`'s **issuer-AID guard** then rejects TCTs minted under the old AID before the SDK is consulted. | `intra-org/key-rotation` | [RFC-AITP-0007](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/blob/main/rfcs/RFC-AITP-0007-key-resolution.md) |
| **TCT renewal** | Holder's `/admin/renew-tct` → issuer's `/admin/process-renewal`; the holder swaps its held TCT in place. | `intra-org/tct-renewal` | [RFC-AITP-0013](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/blob/main/rfcs/RFC-AITP-0013-tct-renewal-extension.md) |
| **TCT verification cache** | When `aitp.TctStore` exists, `verify_capability_tct` routes through `verify_tct_cached`; `tct_cache_stats` exposes hit/miss counters. | `intra-org/tct-cache-perf` | [RFC-AITP-0005](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/blob/main/rfcs/RFC-AITP-0005-tct.md) |
| **Session bundles** | A coordinator's `/admin/export-session-bundle` packages the TCTs it issued; a verifier's `/admin/verify-session-bundle` returns the `BundleOutcome`. | `intra-org/session-bundle` | [RFC-AITP-0010](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/blob/main/rfcs/RFC-AITP-0010-session-trust-bundle.md) |
| **Multi-hop delegation** | The redeem endpoint swaps `verify_delegation` for `verify_delegation_multihop` when available. | `intra-org/delegation-multihop` | [RFC-AITP-0011](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/blob/main/rfcs/RFC-AITP-0011-multihop-delegation.md) |
| **SPKI pinning** | A pure-SDK `spki_pin_check` step — no agent involved. | `intra-org/spki-pinning` | [sdk-python.md § SPKI cert pinning](https://github.com/agentidentitytrustprotocol/aitp-rs/blob/main/docs/sdk-python.md#spki-cert-pinning-hpkp-style-feature-spki-pinning) |

## What you can ignore (boundary check)

If you find yourself wanting to do any of these in this repo, the SDK
should be doing it instead:

- Parse a TCT to inspect grants (workers read the *unverified* claims
  via the shared `decode_claims` helper only for the revocation
  short-circuit, the issuer-AID guard, and the declared audience;
  everything security-relevant routes through `verify_tct`).
- Canonicalize JSON for signing.
- Verify a signature — **in `src/` or `agents/`**. Test code is the one
  carve-out, and it is deliberate: when the SDK itself is the thing under
  test, verifying with the SDK is circular. It would pass under *any*
  self-consistent convention, including a wrong one — which is exactly how a
  wrapped-form revocation signing input survived a full release across this
  family before 0.5.0. `tests/unit/test_revocation_signing_convention.py`
  therefore verifies with `cryptography` plus an independent RFC 8785
  canonicalizer vendored in `tests/unit/_jcs_reference.py`. The oracle has to
  be independent of the artifact under test. Do not "fix" that into
  circularity.
- Build any AITP message by hand.
- Track handshake state across multiple requests (the responder map
  in `AitpServer._sessions` is keyed by `session_id` from the SDK,
  not state we own).

The playground exists to drive scenarios; the SDK exists to enforce
the protocol. Keep the wall solid.
