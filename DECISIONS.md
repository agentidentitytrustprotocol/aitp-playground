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

## D-8 — Pin identity at the CP trust-anchor site, not just authenticity
**2026-08-26 · reconcile · recommender: Fable · verdict: CHANGE, implemented**

`cp_provision_trust_anchor` verified the agent manifest but did not assert
`manifest["aid"] == ra.aid`, so whatever answered at a launched agent's port could have its
key pinned into the control plane's trust store **under that agent's AID**.

Deferred originally as "belongs with Phase 6, needs a fixture rework". Both premises expired:
Phase 6 shipped without touching the site, and Phase 2B had already made the fixture mint
real signed manifests — leaving four lines plus one test.

The "narrow exposure" argument was also wrong in kind. It is narrow by *wiring*, not by
verification: launch and manifest-fetch are separate channels, so an agent that dies between
reporting ready and being provisioned leaves a port anything can take, serve its own
genuinely-signed manifest from, and pass. Extending provisioning to hosted agents would have
made it remotely exploitable with no test failing.

`ra.aid` is the right comparand: the supervisor reads it from the spawned process's own
`AITP_AGENT_READY` line over a parent-child pipe, not off the network.

Implemented in `runner/engine.py` with a mismatch test proven non-vacuous by mutation.
Closes `PENDING.md` P1.

## D-9 — Serving-side manifest freshness belongs with ingest verification
**2026-08-26 · reconcile · recommender: Opus · verdict: CONFIRM**

Folding the re-mint into Phase 2B was right, and the logged reason ("one property") understated
it. `verify_manifest_json` is the **same function** on both sides — enabling it for ingest
enabled it for every peer verifying us, in the same commit, across the family. A split phase
would not have left a theoretical window; it would have left every agent alive past `ttl_secs`
undialable, surfacing as `cause=expired` on the *peer's* telemetry, i.e. blamed on the wrong
agent. The control plane's enrollment guard (rejects manifests expiring within 5 minutes) and
its `listAgents` expiry filter are a third consumer a split would have broken.

Half-life is the correct trigger, not a compromise: it guarantees a remaining-life *floor* of
TTL/2, which clears both the CP's 300s enrollment guard and `max_staleness_secs` by ~6x. A
later trigger shrinks that floor below both. It is also the standard idiom (SPIFFE/SPIRE
rotate X.509-SVIDs at exactly 50% of lifetime). Cost is one Ed25519 signature per agent per
half-TTL, taken lazily.

Three refinements taken as follow-ups rather than re-decisions (`PENDING.md` P10): derive the
deadline from the manifest's own `published_at`/`expires_at` instead of config plus a
constructor timestamp; add a failure cooldown; record that the push path (re-enrolling after a
re-mint) is out of scope.

## D-10 — Manifest verification failure stays 502
**2026-08-26 · reconcile · recommender: Opus · verdict: CONFIRM**

502 matches the taxonomy `agent_admin.py` already uses — 412 caller-state, 404 unknown
capability, 500 wiring bug here, 502 downstream peer — and a 4xx would be the one site in the
repo that blames the caller for a third party's bytes.

The plan asked for "a 4xx naming the cause". The naming half is what was load-bearing, and the
status code cannot carry it regardless: `api/hosted.py` catches `HTTPStatusError` from this
route and re-raises 502 unconditionally at the federation boundary, so any 4xx would survive
only inside a detail string — the same place the current detail already lives. The
`manifest.verify_failed` event's `cause` is the only channel that crosses intact, which is
where the distinction was put. Nothing in this repo or any sibling branches on the code;
every caller uses `raise_for_status()`, which is code-blind.

Docstring tightened to record the taxonomy, so this is not re-litigated from the plan text.

## D-11 — The SDK-surface guards become assertions, not skips
**2026-08-26 · reconcile · recommender: Opus · verdict: CHANGE, implemented**

Three `skipif` guards (signing-convention interlock, verify-or-discard, and the soft-fail
forgery case) are now hard assertions.

Two reasons the skip stopped being defensible. First, the condition is unreachable where it
was supposed to protect: both `sign_revocation_list` and `verify_revocation_list` are
unconditional in the binding (no `#[cfg(feature)]`), and the floor is now `aitp-sdk>=0.6.0`,
so CI's `uv sync --locked` cannot produce a wheel without them. Second, the one path that
*does* still reach it — `maturin develop` from an old sibling `aitp-rs` checkout, which
bypasses the resolver entirely — is exactly where a silent skip does the most damage.

And the skip was never loud. The comment claimed "skip LOUDLY, with the reason"; CI runs
`pytest -q` with no `addopts`, so a skip renders as a bare `s` and the reason is never
printed. Coverage would not have caught it either: these modules exercise `agents/base`,
outside `source_pkgs`, so skipping them costs zero coverage and clears `--fail-under=88`.

Rejected the `addopts = "-rs"` alternative: it widens output for every skip in the repo and
still leaves a green job in a state the reason text calls "a coverage hole, not a pass".

Verified by installing 0.5.0: the suite is red with 8 named failures where it previously
reported a green count.

## D-12 — `docker-compose e2e` stays advisory, not required
**2026-08-26 · user decision**

Phase 4 widened the job to run pre-merge on any PR touching `uv.lock`. It is deliberately
**not** added to `main`'s required status checks.

Accepted trade: a green bump PR can auto-merge while e2e is still running or red. Detection
exists; the gate does not. Weighed against changing repo-wide configuration that affects every
contributor's PR, plus the skipped-vs-required trap — a conditional job wired wrong leaves
unrelated PRs waiting on a check that never reports.

Reversible in one settings change; `PENDING.md` P7 keeps the close-out steps, including the
requirement to demonstrate **both** directions (a pin PR blocks, an unrelated PR merges).
