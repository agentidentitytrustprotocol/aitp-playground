# DECISIONS — revocation-verification work

Settled calls, with the reasoning that settled them. Open items are in
`ASSUMPTIONS.md` (`UNCONFIRMED`) or `PENDING.md`.

## D-1 — The interlock's oracle must not be the SDK
**Phase 2.** Verification uses `cryptography` plus a vendored RFC 8785 canonicalizer, never
`aitp-sdk` itself. A test where the SDK both signs and verifies passes under *any*
self-consistent convention, including a wrong one — which is exactly how the pre-0.5.0
wrapped signing input survived a full release across this family. The oracle has to be
independent of the artifact under test. Accepted cost: a 201-line vendored copy (`PENDING.md`
P3).

## D-2 — A negative assertion ships only once it has been shown to fire
**Phase 2.** Every negative case is demonstrated by construction or by mutation, never by
reasoning: the wrapped-form predicate is proven against a hand-signed legacy envelope; the
0.4.1-vs-0.5.0 behaviour was probed under both wheels; the integration suite's coverage of
manifest verification was proven by forcing the check to fail and watching the scenario go
red. A test that cannot fail is the thing being fixed.

## D-3 — `self_inclusive` is guarded, not proven
**Phase 2.** A signature over a body containing that signature is a fixed point no signer can
construct, so its negative assertion cannot be shown non-vacuous the way the wrapped form
can. Kept anyway — it guards a *convention* change (someone generalizing the session bundle's
member placement onto revocation) — with the limit documented in the test rather than papered
over, plus a distinctness check so it cannot silently alias another input.

## D-4 — Manifest verification and manifest freshness ship together
**Phase 2B.** `verify_manifest_json` enforces `expires_at`, and this repo minted its served
manifest once at startup with a 3600s TTL. Shipping ingest verification alone would have made
every agent alive past an hour serve a manifest all verifying peers reject — a worse bug than
the one being fixed. They are one property ("manifests in this system are verifiable"), so
they landed in one phase rather than leaving a knowingly-broken window between two.

## D-5 — Verification failure is telemetry-distinct from fetch failure
**Phase 2B.** `manifest.verify_failed` carries an explicit `cause`
(`signature_invalid | expired | malformed`) and never aliases a transport error. Collapsing
the two is how a signing-convention break gets triaged as a network blip — the precise
confusion this whole effort exists to prevent. Same requirement carries into Phase 6 for
revocation.

## D-6 — Fixtures mint; the check is not relaxed
**Phase 2B.** Turning on verification broke a test that hand-built an unsigned manifest dict.
The fixture was changed to mint a real signed envelope, and a hard-coded `PK-b64` literal was
replaced with a value derived from what the fixture actually serves. Relaxing the check to
keep a fake passing would have removed the property being added.

## D-7 — Plan defects are corrected in the plan, not worked around
Where implementation showed a plan criterion was wrong, the plan was edited inline and the
correction recorded: Phase 1's "no `uv.lock` modification" (unsatisfiable — uv mirrors the
declared specifier), and Phase 2's implied by-construction proof for `self_inclusive` (a fixed
point). A plan that survives contact with the code unamended is a plan nobody checked.
