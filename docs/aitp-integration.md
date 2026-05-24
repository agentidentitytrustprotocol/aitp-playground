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

That's the full surface area. Everything else is HTTP plumbing or
telemetry.

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

The wire schema is owned by the SDK. The playground only fishes
`offered_capabilities`, `handshake_endpoint`, `aid`, and
`identity_hint.public_key` out of it when constructing requests.

## Peer discovery (`TrustOrchestrator.resolve_peers`)

Resolves `{agent_id: {manifest_url, did, source?}}` based on the
scenario's `spec.trust.discovery`:

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

The 4-message AITP mutual handshake, in this codebase:

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

1. **Local revocation short-circuit.** RFC-AITP-0008 places the
   revocation check after signature verification (so a forged jti
   can't bypass the deny set). For demo purposes we check first —
   the only jtis in our deny set were observed via prior handshakes,
   so the early-out is safe and cheaper.
2. **SDK `verify_tct` in presented-TCT mode.** We pass the TCT's own
   declared `audience` as `expected_audience`. In v0.1
   (RFC-AITP-0005) `audience == subject`, so this asserts "the
   TCT identifies this holder" — the holder's identity claim. The
   signature check itself (against the issuer's pubkey derived from
   `tct.issuer`) is the actual security gate: it proves *we* (this
   resource server) issued the TCT.

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

Notes:
- `build_delegation` takes the *held* TCT as input. The SDK enforces
  `scope ⊆ TCT.grants` at build time — you can narrow but not widen.
- The verifier (`writer`) ensures it is the token's `delegator` before
  issuing — untrusted parties cannot redeem against a writer with a
  chain it never authored.
- The redeemed TCT is bound to the delegatee's `cnf` key, so a stolen
  token can't be replayed by a third party — the cnf binding requires
  the holder to actually sign with the matching key.
- The v0.1 demo skips proof-of-possession on redemption; the cnf
  binding still bounds replay.

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

No revocation list is published over the wire. Full conformance would
sign and distribute one; the playground intentionally stops short of
that since the demo only needs to show the fail-closed behavior.

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
