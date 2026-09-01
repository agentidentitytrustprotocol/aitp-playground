# ASSUMPTIONS — revocation-snapshot verification

Entries are reconciled via `/reconcile` before ship. `Plan:` scopes each entry.

(none yet)

## Interlock skip keeps CI green
- **Plan:** `../aitp-control-plane/plans/cross-repo/aitp-playground-revocation-verification.md` (Phase 2)
- **Assumed:** A wheel lacking `AitpAgent.sign_revocation_list` is a wheel nobody ships, so a
  skip is an acceptable failure mode for that case.
- **Chose:** Module-level `pytest.mark.skipif` with a reason that names it a coverage hole —
  the pattern the plan prescribed (`test_sdk_blocked_features.py:33` idiom). CI runs
  `pytest -q` with no `-ra`, so a skip shows as a count and the reason is never printed: the
  job stays green.
- **Alternatives:** Fail the suite outright when the surface is missing (turns a
  `--no-default-features` wheel into a hard CI failure); add `-ra` to addopts (widens output
  for every skip in the repo, not just this one).
- **Blast radius if wrong:** The interlock silently stops interlocking and nobody notices
  until a convention change ships. Low likelihood, high cost — exactly the shape of the
  original bug.
- **Status:** **CHANGED (2026-08-26)** — the three skip guards are now hard assertions. The
  skip condition became unreachable in CI once the floor moved to `>=0.6.0` (both symbols are
  unconditional in the binding), but it is still reachable via `maturin develop` from an old
  sibling checkout — the one place a silent skip does most damage. And the "skip LOUDLY"
  comment asserted a property the configuration never provided: `pytest -q` with no `addopts`
  prints a bare `s` and never the reason. Verified: on a 0.5.0 wheel the suite is now red with
  8 named failures. See `DECISIONS.md` D-11.

## Manifest verification failure returns 502, not 4xx
- **Plan:** `../aitp-control-plane/plans/cross-repo/aitp-playground-revocation-verification.md` (Phase 2B)
- **Assumed:** The failure being reported is the *upstream peer's*, not the caller's.
- **Chose:** `HTTPException(502)`. The caller asked us to handshake with a URL; the URL
  answered with something that does not verify. That is a bad gateway, not a bad request —
  a 4xx would blame the scenario author for a peer's malformed response.
- **Alternatives:** The plan says "4xx naming the cause". 502 risks reading as a transport
  blip, which is exactly the fetch-vs-verify confusion this effort exists to prevent — that
  is mitigated by the `manifest.verify_failed` telemetry event carrying an explicit `cause`,
  so the distinction lives in the event stream rather than the status code.
- **Blast radius if wrong:** A status-code change; any caller matching on it is in this repo.
- **Status:** **CONFIRMED (2026-08-26)** — and confirmed to be *unchangeable in effect*:
  `api/hosted.py` flattens any status from this route back to 502 at the federation
  boundary, so a 4xx would survive only inside a detail string. Nothing in this repo or any
  sibling branches on the code. See `DECISIONS.md` D-10.

## Serving-side manifest freshness folded into Phase 2B
- **Plan:** `../aitp-control-plane/plans/cross-repo/aitp-playground-revocation-verification.md` (Phase 2B)
- **Assumed:** Shipping ingest verification while our own served manifest can expire would
  introduce a worse bug than the one being fixed — every agent alive past its `ttl_secs`
  (default 3600) would serve a manifest that all verifying peers reject.
- **Chose:** Widen Phase 2B to re-mint the served manifest past its half-life
  (`aitp_server.py:_fresh_manifest_json`), rather than split it into a later phase. The two
  are one property — "manifests in this system are verifiable" — and splitting them would
  have shipped a known break in between.
- **Alternatives:** A separate phase (leaves a window where the repo is knowingly broken for
  long-lived agents); raising `ttl_secs` (moves the cliff, does not remove it).
- **Blast radius if wrong:** Re-minting changes `published_at`/`expires_at`/`signature` on a
  cadence. The AID is unchanged, so AID-pinning peers are unaffected; a peer caching manifest
  *bytes* would see them change every half-TTL. **Correction: that byte-cache case is not
  hypothetical** — the control plane stores the manifest sent at enrollment and drops the
  agent from `listAgents` once `manifest_expires_at` passes, and nothing re-enrolls after a
  re-mint. The push path is knowingly out of scope; see `PENDING.md` P10.
- **Status:** **CONFIRMED (2026-08-26)** — and for a stronger reason than logged:
  `verify_manifest_json` is the *same* function on both sides, so enabling it for ingest
  enabled it for every peer verifying us in the same commit. A split phase would not have
  left a theoretical window but an undialable agent, reported as `cause=expired` on the
  *peer's* telemetry — the fetch-vs-verify misattribution D-5 exists to prevent. Half-life
  is the standard floor-guaranteeing trigger (SPIFFE/SPIRE rotate at exactly 50%), and it
  clears both the CP's 300s enrollment guard and `max_staleness_secs` by 6x.
  See `DECISIONS.md` D-9.

## CP trust-anchor pin verifies authenticity but not identity — **RESOLVED**
- **Plan:** `../aitp-control-plane/plans/cross-repo/aitp-playground-revocation-verification.md` (Phase 2B)
- **Assumed:** Closing the authenticity hole at `runner/engine.py` now, and deferring the
  identity comparison, is better than doing neither in this phase.
- **Chose:** `aitp.verify_manifest_json` before reading `identity_hint.public_key`, but NOT
  `manifest.aid == ra.aid`. The identity pin needs the fake supervisor to issue real AIDs
  instead of synthetic `aid-<agent_id>` strings, which three other tests assert on — a
  fixture rework that belongs with Phase 6, where expected-issuer pinning is the subject.
- **Alternatives:** Do the fixture rework inside 2B (sprawls the phase); leave the site fully
  unverified (was the pre-existing hole, and this site pins a key into the CP trust store).
- **Blast radius if wrong:** Whatever answers at a launched agent's port can still get its
  key pinned in the CP *under that agent's AID*. Local runner, agent it launched itself, so
  exposure is narrow — but it is the substitution shape, not yet closed.
- **Status:** **CHANGED (2026-08-26)** — the identity pin was added. Both premises of the
  deferral had expired: Phase 6 shipped without touching this site, and Phase 2B had already
  done the expensive half of the fixture rework (`_fake_agent_manifest` mints real signed
  envelopes), leaving a ~4-line change. The "narrow exposure" reasoning was also wrong in
  kind: it is narrow by *wiring*, not by verification — launch and manifest-fetch are
  separate channels, so anything that takes the port between ready and provisioning serves
  its own genuinely-signed manifest and passes. See `DECISIONS.md` D-8.

## Phase 8 — the revocation-via-cp scenario's *second* caveat
- **Plan:** `../aitp-control-plane/plans/cross-repo/aitp-playground-revocation-verification.md` (Phase 8)
- **Assumed:** The scenario text carried two caveats. Phase 8 owns only the first
  (the snapshot's signature is unverified, citing the now-closed `#46` / `PENDING.md` P8).
  The second — "no step drives a call whose outcome depends on the CP-derived deny-set,
  so the final 403 comes from the issuer's local set" — cites neither closed ticket and
  is outside Phase 8's acceptance criteria.
- **Chose:** Corrected the signature claim everywhere it appeared and left the second
  caveat standing, rather than rewriting text I had not re-verified against the code.
- **Alternatives:** Rewrite both while in the file. Rejected — `PENDING.md` P9 (closed
  2026-08-27) says the redeem path now consults revocation, so a CP-derived entry *can*
  change a decision; whether *this scenario* exercises that is a separate question, and
  asserting it without checking would repeat the exact "claim outlived the condition"
  defect Phase 8 exists to remove.
- **Blast radius if wrong:** Docs/scenario prose understates what the scenario shows.
  No code impact. One-line fix once someone checks the step list against the redeem path.
- **Status:** **CONFIRMED (2026-08-28)** — checked, not assumed.
  `scenarios/intra-org/revocation-via-cp/1.0.0/scenario.yaml` has exactly four workflow steps:
  `handshake`, `first_call`, `revoke` (type `revoke_tct`), `blocked_call` (type
  `capability_probe`). No `delegate` or `redeem_delegation` step exists, so P9's redeem-path
  deny-set enforcement (`aitp_server.py`'s `/aitp/delegation/redeem`) is never reached by this
  scenario — the caveat's text is accurate as written and needs no change. See
  `plans/audit-2026-08-28-cleanup.md` Phase 12.

## Phase 4 — PENDING.md gets two entries (P15, P16), not one
- **Plan:** `plans/aitp-rs-breaking-changes-adoption.md` (Phase 4)
- **Assumed:** The plan specified one `PENDING.md` entry linking the already-filed
  `aitp-rs`#152. Phase 3's pre-flight (same run) surfaced a second, distinct finding —
  `UNKNOWN_FIELD` reachable via `TCT_CLAIMS_MEMBERS` on TCT/voucher/delegation
  compact-JWS paths, a cross-implementation interop hazard that blocks Phase 5
  specifically, not a general upstream-SDK-gap report like #152.
- **Chose:** Two entries — P15 for exactly what the plan specified, P16 for the new
  Phase 3 finding — rather than folding P16's content into P15 or a Phase 3 entry.
  Reasoning: P15's `**Blocks:**` field is honestly "nothing in this repo today"; P16's is
  honestly "Phase 5 of this plan." Merging them would make one of those two blocks fields
  wrong, and PENDING.md's own convention (one problem, one entry) argues for the split.
- **Alternatives:** Fold P16 into Phase 3's PROGRESS.md note only, not PENDING.md.
  Rejected — PROGRESS.md is a phase-log, not a tracked-item registry; a residual risk
  that specifically blocks a *future* phase belongs in PENDING.md's forward-looking
  P-number sequence, matching how P8 tracked "Phase 6 is blocked on an aitp-sdk release."
- **Blast radius if wrong:** Cosmetic — worst case, two PENDING.md entries where one
  would have done, or the P16 content is over-scoped as a blocker rather than a
  watch-item. Phase 5 is already BLOCKED for an unrelated reason (no successor release
  exists), so this doesn't change any near-term behavior.
- **Status:** **CONFIRMED (2026-08-31)** — the split stands, on independently-verified
  grounds different from the ones this entry originally gave. `/reconcile`'s Opus review
  found the real justification: P15 and P16 are opposite-signed findings on the same
  upstream commit (P15 is a *missing* check on JSON-envelope paths; P16 is an
  *over-strict* check on compact-JWS paths), with different close conditions (P15 closes
  when `aitp-rs`#152 lands upstream; P16 closes when someone here verifies a real
  `aitp-control-plane`-minted token) and different owners. That's a materially stronger
  argument than "PENDING.md's own convention (one problem, one entry)" — which the
  same review found does **not** actually exist as a repo convention (`P10` bundles three
  distinct refinements under one number; `P13` explicitly covers two independently-closed
  sub-items). The real convention is closer to "one origin/trigger, one entry, sub-numbered
  if needed" — recorded correctly this time so a future reconciliation doesn't cite a
  convention that isn't real. One correction was made as part of confirming this: P16's
  `**Blocks:** Phase 5` field was inaccurate (Phase 5 is independently blocked on no
  successor release existing; P16 is a pre-merge check *inside* that phase, not why it
  hasn't shipped) and is now `**Blocks:** nothing today` / `**Gates:** Phase 5's eventual
  adoption`. See `DECISIONS.md` D-21.
