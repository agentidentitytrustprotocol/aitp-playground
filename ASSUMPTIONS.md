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
- **Status:** UNCONFIRMED

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
- **Status:** UNCONFIRMED

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
  *bytes* would see them change every half-TTL.
- **Status:** UNCONFIRMED

## CP trust-anchor pin verifies authenticity but not identity
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
- **Status:** UNCONFIRMED
