# PENDING — deferred items from the revocation-verification work

Items deliberately **not** done during the `/drive` run of
`../aitp-control-plane/plans/cross-repo/aitp-playground-revocation-verification.md`.
Each names why it was deferred and what closing it would take, so none of them
depends on remembering a conversation. Decisions live in `DECISIONS.md`;
still-open judgement calls live in `ASSUMPTIONS.md` as `UNCONFIRMED`.

## ~~P1 — Pin identity at the CP trust-anchor site~~ — **CLOSED 2026-08-26**
**From:** Phase 2B · **Closed by:** reconcile `DECISIONS.md` D-8 — the pin is implemented, with a substitution test proven non-vacuous by mutation

`src/aitp_playground/runner/engine.py` verifies the agent manifest before pinning
`identity_hint.public_key` into the control plane's trust store, but does **not** assert
`manifest["aid"] == ra.aid`. Whatever answers at a launched agent's port can therefore still
get its key pinned *under that agent's AID*.

Closing it needs `FakeSupervisor` (`tests/unit/test_engine_run.py`) to issue real AITP AIDs
instead of synthetic `aid-<agent_id>` strings, which three other tests assert on. That rework
belongs with Phase 6's expected-issuer pinning, where real AIDs are the subject anyway.

## ~~P2 — A skipped interlock keeps CI green~~ — **CLOSED 2026-08-26**
**From:** Phase 2 · **Closed by:** reconcile `DECISIONS.md` D-11 — all three guards are now assertions; on a 0.5.0 wheel the suite is red with 8 named failures

`tests/unit/test_revocation_signing_convention.py` skips wholesale if the installed wheel
lacks `AitpAgent.sign_revocation_list`. CI runs `pytest -q` with no `-ra`, so the skip shows
only as a count and the reason is never printed — the job stays green while the interlock is
not interlocking.

Options: fail the suite outright when the surface is missing (turns a `--no-default-features`
wheel into a hard CI failure), or add `-ra` to addopts (widens output for every skip in the
repo). Neither is obviously right; that is why it is here rather than decided.

## ~~P3 — `aitp-verifier` is vendored, not depended on~~ — **CLOSED 2026-08-27**
**From:** Phase 2 · **Resolved by:** a decision not to publish, plus a real CI gate

**Decision: keep the vendored copy; do not publish `aitp-verifier`.** Publishing means
building a release pipeline in a repo that has none (`aitp-verifier-py/.github/workflows/`
holds only `auto-merge.yml` and `ci.yml`) for a package whose sole consumer is one test
file — and a released dev-dependency then pins a *version*, reintroducing the staleness the
vendoring was meant to avoid.

What actually needed fixing was different, and worse than the original entry said. The drift
guard compared the copy against its source but **skipped when the source was absent**, and
CI does a plain self-checkout — so it was a permanent no-op there, catching drift only on a
developer machine that happened to have both checkouts. The unit job now clones the sibling
before pytest, making it a real cross-repo gate against verifier **HEAD**, which is stricter
than any published pin would be.

Rejected alternatives: a committed checksum verifies the copy against a recorded hash, so
hash and source rot together and it signals nothing; a submodule pins a SHA that goes stale
silently. Both are worse than comparing against a fresh clone of HEAD.

The copy must stay independent of `aitp-sdk` either way — that independence is the whole
point, and a JCS check that runs against the SDK it is checking is exactly the
self-consistency that let the 0.5.0 signing-input divergence survive a release.

`tests/unit/_jcs_reference.py` is a 201-line verbatim copy of
`aitp-verifier-py/aitp_verifier/jcs.py`, because that package is not on PyPI (404) and a
path dependency on a sibling checkout would make this repo's unit suite depend on a repo CI
does not check out. If `aitp-verifier` is ever published, delete the copy and take a
dev-group dependency instead. The copy must stay independent of `aitp-sdk` either way — that
independence is the whole point.

## ~~P4 — `expired` is classified by substring-matching an SDK error message~~ — **CLOSED 2026-08-27**
**From:** Phase 2B · **Resolved by:** aitp-rs#92, shipped in aitp-sdk 0.7.0

`_verify_peer_manifest` now branches on `exc.code`. The SDK floor is `>=0.7.0`, so the code
is always present on a verification failure; input that is not JSON at all still raises a
plain `ValueError` with no code, which is reported as `malformed` (there is no envelope to
classify), and anything else uncoded is `unknown` rather than being guessed at
`signature_invalid` — reporting a parse bug as an attack would page someone for nothing.

Two guards keep it from regressing: one raises an error whose *text* says nothing about
expiry but whose `code` is `expired` and asserts the cause is still `expired` (under the old
substring match it would have read `signature_invalid`), the other pins the `unknown`
fallback. Both fail if the substring logic is restored.

The original entry below is kept for the history of why this mattered.

`_verify_peer_manifest` distinguishes `cause=expired` from `cause=signature_invalid` by
looking for "expired" in the SDK's exception text, because the Python binding registers no
exception classes — every failure is a `RuntimeError`/`ValueError` carrying a string. A
wording change upstream silently reclassifies the cause. Pinned by the expired-manifest test,
so it fails loudly rather than silently.

**Checked against 0.6.0 — NOT fixed, and the reason is worth recording.** PR #90 gave the
*revocation* path a typed error, and left the manifest path untyped:

| Path | Error type | `.code` |
|---|---|---|
| `aitp.verify_revocation_list` | `RevocationVerificationError` | yes |
| `aitp.verify_manifest_json` | `RuntimeError` | no |

So this is the same asymmetry the whole effort is about — one binding has the surface, its
sibling does not — reproduced one artifact over, by the change that was fixing it. The fix is
a small upstream follow-up in `aitp-rs`: give `verify_manifest_json` the same treatment
(`bindings/aitp-py/src/manifest.rs`, plus Node parity in `bindings/aitp-node`), then branch
on `.code` here instead of the message text.

## ~~P5 — Criterion 4 of Phase 3 is unverified locally~~ — **CLOSED 2026-08-25**
**From:** Phase 3 · **Resolved by:** CI run 32929085653 on PR #47

The plan's Phase 3 criterion 4 ("the full `docker-compose.test.yml` stack passes on the
`pypi` path, `revocation-via-cp` included") could **not** be run on this machine. The
`aitp-cp` image fails to build, before the playground image is even reached:

```
> next build
⚠ Mismatching @next/swc version, detected: 15.5.23 while Next.js is on 16.3.2
Error: An IO error occurred while attempting to create and acquire the lockfile
  [cause]: TypeError: bindings.lockfileTryAcquireSync is not a function
```

Reproduced with `--no-cache`, so it is not a stale layer. It is a Next.js native-binding
fault in the **control plane's** build, unrelated to anything in this repo — and
`Dockerfile.cp-e2e`'s own header notes the CP's napi binding ships only `darwin-arm64`,
while this host is arm64 and CI is `linux/amd64`.

What *was* verified locally, directly against built images: the pinned wheel is installed
(`aitp_sdk-0.5.0`, matching `uv.lock`), `/capabilities` is byte-identical between the `pypi`
and `path` builds, `AITP_SDK_SOURCE=path` still compiles, and no Rust toolchain exists on the
default path. Only the full multi-service run is outstanding, and `docker.yml`'s `e2e` job
exercises exactly that on `linux/amd64` against a fresh `aitp-control-plane` checkout.

**A correction to an earlier version of this entry.** It originally said the only open
question was the control plane's own build. That was wrong, and the Phase 3 verifier caught
it: the first draft of `docker.yml` also removed the `aitp-rs` checkout from the `e2e` job,
which would have broken the `aitp-cp` image in CI for a *different, self-inflicted* reason —
`Dockerfile.cp-e2e:46-48` compiles the **control plane's** napi binding from `aitp-rs`
source, a dependency entirely separate from the playground's SDK. The local repro hid it
because `aitp-rs` exists on disk here no matter what CI checks out. The checkout has been
restored to the `e2e` job (and correctly stays removed from `build-and-push`, which never
builds the CP image).

**Closed by the PR's own `Docker` run**, which did not need to wait for main:

```
Installing aitp-sdk==0.5.0 (pinned by uv.lock)
tests/integration/test_protocol_e2e.py::test_sdk_version_matches_lock PASSED
tests/integration/test_protocol_e2e.py::test_protocol_scenario[intra-org/revocation-via-cp@1.0.0] PASSED
docker-compose e2e  pass  5m21s
```

Phase 3 criteria 2 and 4 are therefore **met**, on `linux/amd64`, on the `pypi` path, with
`revocation-via-cp` included. The arm64 control-plane build fault described above is real and
local-only; it did not reproduce in CI. Nothing further to do.

## ~~P6 — `aitp-cp` is a symlink to `aitp-control-plane`~~ — **CLOSED 2026-08-26**
**From:** Phase 3 · **Closed by:** documented in `internal_docs/docker.md`, where someone debugging a local-vs-CI divergence in the CP image will actually look

`aitp-cp` → `aitp-control-plane` (a symlink, not a second checkout). Anything that changes
the branch of one changes what the other builds — `docker-compose.test.yml` builds the CP
from `../aitp-cp`, so switching branches in `aitp-control-plane` silently changes the control
plane the e2e stack tests. Worth knowing before debugging a "works in CI, fails locally"
divergence: CI checks the CP out fresh into its own path and has no such coupling.

## ~~P7 — `docker-compose e2e` runs pre-merge but does not *block* merge~~ — **CLOSED 2026-08-28**
**From:** Phase 4 · **Closed by:** user decision to make it required (see `DECISIONS.md` D-12's
reversal and D-13) — added via the `required_status_checks` sub-resource `PATCH`, both
directions demonstrated live on PR #53 (`.md`-only, skipped cleanly, stayed `CLEAN`) and PR #54
(throwaway `uv.lock` bump, `BLOCKED` while the job ran, `CLEAN` once it passed, closed unmerged)

Phase 4 widened the `e2e` job so it fires on any PR touching `uv.lock`.
It now runs before a bump PR merges. It does **not** stop that merge.

`main`'s required status checks are, verified via
`gh api repos/.../branches/main/protection`:

```
Lint (ruff)
Tests (Python 3.11)
Tests (Python 3.13)
Integration (agent subprocess e2e)
```

`docker-compose e2e` is absent. `auto-merge.yml` runs on `pull_request` and delegates to
`aitp-ci`'s shared auto-merge, so a bump PR that satisfies those four merges **unattended**,
whether or not the e2e stack has finished or gone red. A green-but-not-required job blocks
nothing — which is the plan's own criterion 4, and it is currently unmet.

**Not changed here on purpose.** Branch protection is repo-wide configuration affecting every
contributor's PR, not part of this feature's diff. It also has a trap worth thinking about
before flipping: `e2e` is conditional, so on PRs that do *not* touch the pin it is skipped.
GitHub treats a job skipped by a job-level `if:` as satisfying a required check, but that
behaviour is easy to get wrong — if the job ends up "expected, waiting" instead of "skipped",
every unrelated PR hangs forever. The `changes` job always runs precisely so `e2e` has a
definite input and skips cleanly rather than never reporting.

**Decided 2026-08-26: leave advisory.** The job runs pre-merge and is visible on the PR; it
just cannot block. The trade accepted is that a green bump PR can auto-merge while e2e is
still running or red — the detection is there, the gate is not.

**To close later:** add `docker-compose e2e` to `main`'s required contexts, then open one PR
that touches `uv.lock` and one that does not, and confirm the first blocks on e2e while the
second merges normally. Both halves need demonstrating — the second is the one that breaks if
the skip semantics are wrong.

## ~~P8 — Phase 6 is blocked on an aitp-sdk release~~ — **CLOSED 2026-08-26**
**From:** Phase 6 · **Resolved by:** `aitp-sdk` 0.6.0 published; floor raised; both axes shipped

Phase 6 needs `aitp.verify_revocation_list`. That surface was implemented in this run —
`aitp-rs` PR #90, branch `feat/bind-verify-revocation-list` — but:

```
installed aitp-sdk: 0.5.0     has verify_revocation_list: False
PyPI latest:        0.5.0     releases: 0.3.0, 0.4.0, 0.4.1, 0.5.0
```

So the blocker is a **release**, not a merge. Implementing Phase 6 now would mean either
writing against a surface the pinned wheel does not have (it would raise `AttributeError` at
runtime the first time an agent refreshed revocations), or raising `pyproject.toml`'s floor to
a version that does not exist. Neither is shippable, and a capability-probe fallback is
exactly the unchecked posture the phase exists to remove.

**All four steps done.** `aitp-rs#90` merged; `v0.6.0` released (release-plz's computed
v0.5.1 was overridden — the issuer-mismatch reclassification is a behavioural change and in
0.x the minor is the breaking position); `aitp-sdk` 0.6.0 is on PyPI; the floor at the time
was `>=0.6.0` and the suite ran **490 passed, 0 skipped** against the pinned wheel. (The floor
has since moved to `>=0.7.0` — see `pyproject.toml`'s rationale comment; this paragraph is a
historical record of the 0.6.0 release, not a claim about the current floor.)

Two operational notes worth keeping. The binding cascade was wedged by a GitHub Actions
outage and had to be re-dispatched by hand (`gh workflow run release-bindings.yml -f
version=0.6.0`) — the original push-triggered run stayed `queued` forever and could not even
be cancelled. And `uv lock` initially insisted only `<=0.5.0` existed, for the Python 3.14
resolution split only; that was a stale index cache, cleared with
`uv lock --refresh-package aitp-sdk`.

**Do not skip the prerequisite.** The plan's `[REV]` note is load-bearing: the deny-set is
today a **monotonic union** (`agent_admin.py` `revoked_jtis.add(...)`) shared with local
`/admin/revoke-tct`, with no snapshot metadata. Three phrases in Phase 6 presuppose structure
that does not exist — "the previously verified snapshot stays current", "entries never merge",
and "locally-revoked jtis are enforced in every snapshot state". Closing those needs a
snapshot object (`published_at`/`expires_at`/`entries`, replaced wholesale) and a **split**
between local and CP-derived sets, unioned only at enforcement. That restructure touches
shared state in `aitp_server.py`, not just the two ingest sites the Approach names. It was
deliberately not started here: landing a semantic change to the deny-set without the
verification it exists to enable would leave a half-built refactor on `main` with no caller.

**Also carry into Phase 6:** `PENDING.md` P1 (the CP trust-anchor site should pin identity,
not just authenticity — it needs the same real-AID fixture rework) and P4 (`expired` is
currently classified by substring-matching an SDK message; PR #90's typed `.code` removes the
need).

## ~~P9 — the CP-derived deny-set cannot change an outcome, only a diagnosis~~ — **CLOSED 2026-08-27**
**From:** Phase 7 · **Resolved by:** aitp-rs#93 (bindings) + the redeem-path wiring here

**The design question had a factual answer, and it was candidate 2 — but it was never really
a design choice, because the playground was violating two spec MUSTs.**

`/aitp/delegation/redeem` consulted **no** revocation source at all — not the CP snapshot,
not even the agent's own local deny list. Both SDK delegation verifiers built their context
with `VerifyDelegationContext::new`, which hardcodes `revocation_check` and
`hop_revocation_check` to `None`, and neither binding exposed a parameter, so the hooks were
unreachable from Python and Node. That skipped **RFC-AITP-0006 §4 step 7** ("the delegation
token MUST be rejected") and **RFC-AITP-0011 §6** ("the verifier MUST check every hop"). A
revoked source TCT still bought a freshly minted TCT for the delegatee.

The Rust core was already correct — both hooks, tests for each (`round_trip.rs:307`,
`multihop.rs:326`), and the `del-mh-004-revoked-hop` conformance fixture. The gap was
entirely the binding layer.

**This is also where a CP-derived entry finally changes a decision rather than a diagnosis.**
The analysis below stands for the *capability* path: `verify_capability_tct` rejects any TCT
whose `iss` is not this agent, so there a foreign jti is refused either way. But hop jtis are
issued by peers — foreign to the verifier — so the snapshot is the only source it has for
them. `test_a_cp_snapshot_entry_is_enough_to_refuse` pins exactly that.

**Candidate 1 (holder-side pre-flight) is rejected**: it changes no security outcome, since
the issuer's own deny list already rejects, and revocation is specified as a verifier-side
post-signature check (RFC-AITP-0008 §3.3). **Candidate 3 (informational only) is rejected**
because it leaves the two MUSTs violated.

**One deviation is deliberate and documented in the binding.** §6 wants each hop `jti`
checked against the deny list of *that hop's issuer*; a flat set cannot express that, so it
applies to every hop. It can only reject more, never accept a revoked hop — but a set
aggregated across issuers lets any contributor revoke any hop. Note the CP list is not a
spec-faithful RFC-AITP-0008 artifact for agent-issued TCTs: §1.5 wants the snapshot's issuer
to equal the `iss` of every covered TCT, while the CP signs under its own AID and its schema
carries no issuer attribution. Acceptable intra-org and narrated as such; the spec-faithful
source is each hop issuer's own `ListRevoked`.

**Revisit when** a cross-org multi-hop scenario exists (only `intra-org/delegation-multihop`
does today). At that point the CP-as-oracle shortcut stops being narratable, and either the
CP schema grows issuer attribution plus issuer-proof on POST, or agents fetch each hop
issuer's `ListRevoked` directly.

The original analysis follows, since its empirical findings about the capability path remain
correct and are why this was investigated at all.

The entry originally said `revocation-via-cp` was "one step away" from demonstrating
enforcement-from-propagation: add a call *into* the writer after it refreshes from the CP, so
its CP-derived deny-set produces the 403. **That step cannot work**, and the reason is
structural.

`verify_capability_tct` rejects any TCT whose `iss` is not this agent
(`agents/base/aitp_server.py`, the issuer-AID guard). So:

- an agent only ever honours TCTs **it issued** — and for those it already knows the
  revocation locally, because it is the one that revoked them;
- a **foreign** TCT is rejected whatever the deny-set says.

Verified empirically rather than argued. Presenting the same foreign TCT to an agent, with and
without the jti in its CP snapshot:

```
CP entry present -> 403  tct revoked (cp-snapshot): jti=da4f386b-…
CP entry absent  -> 403  tct issuer mismatch: aid:pubkey:qxb5YQNB…
```

Same outcome, different explanation. The CP snapshot moves the *diagnosis*, never the
*decision*. The delegation redeem path does not consult the deny-set at all, so it is not an
alternative site either.

**What this means.** Snapshot verification (Phase 6) is still worth having — it stops a
forged or suppressed list from corrupting an agent's view, and the view is real: it is
reported in `revocation.list_fetched` and observable as `audience_revoked_count`. But the
"federation story" the scenario narrates — *a peer that consults the CP list refuses a token
without asking the issuer* — is not reachable while only the issuer honours its own TCTs.

**The real design question, for a human:** where should a CP-published revocation actually
bite? Three candidates, none of them a one-line change:

1. **Holder-side pre-flight** — a holder checks the CP list before presenting a TCT, so a
   revoked token is never sent. Changes what `/admin/invoke` does.
2. **Delegation-chain validation** — a redeemer validating a multi-hop chain checks every
   intermediate jti against the CP list. This is the case where a third party genuinely needs
   an issuer-independent source, and where RFC-AITP-0008's distribution model earns its keep.
3. **Accept it as informational** — the snapshot exists for observability and for the
   holder's own bookkeeping, and the scenario text says so. Cheapest, and closest to what the
   code does today.

The scenario text and the docs already describe the current behaviour accurately (Phase 7),
so nothing is *claiming* more than it does. This entry is now a design decision awaiting an
owner, not a task.

## ~~P10 — Manifest re-mint: three refinements to the same trigger~~ — **CLOSED 2026-08-27**
**From:** reconcile of the Phase 2B freshness decision (`DECISIONS.md` D-9)

All three are done. Items 1 and 2 landed in #49 — `_manifest_deadline()` reads
`published_at`/`expires_at` off the manifest itself (`_manifest_minted_at` is gone),
`_MANIFEST_REMINT_COOLDOWN_SECS` backs a failed re-mint off by 30s instead of retaking the
lock on every request, and `/admin/rotate-keys` now calls the shared `get_manifest_json`
rather than re-implementing the mint (the two copies had already drifted). #49 simply never
marked this entry closed.

Item 3 is closed here: `/admin/enroll-with-cp` now documents that the push path is one-shot
and the CP's stored copy goes stale — an agent enrolled at start-up drops out of the CP's
`listAgents` at `ttl_secs` while its own endpoint serves a fresh manifest. Pre-existing and
not worsened by the re-mint, but real, and re-enrolling after a re-mint is a
control-plane-side decision rather than a change to that route.

The original text follows.

The decision to fold serving-side freshness into Phase 2B, and the half-life trigger, are both
CONFIRMED. Three refinements came out of that review. None changes the decision, so they are
follow-ups rather than a new assumption.

1. **Derive the re-mint deadline from the manifest itself.** `_fresh_manifest_json` currently
   computes it from `bootstrap.ttl_secs` plus a `_manifest_minted_at` stamped in the
   constructor — for a manifest the constructor did not mint. Correct today only because the
   agent mints milliseconds earlier with the same config. Parsing `published_at`/`expires_at`
   out of `manifest_json` removes both couplings and lets `_manifest_minted_at` go entirely.
   It would also expose that `/admin/rotate-keys` re-implements the mint by hand instead of
   calling `bootstrap.get_manifest_json`, and that the two copies have already drifted
   (`display_name` handling, and rotate drops the `identity_type` default).
2. **Add a cooldown on repeated re-mint failure.** The failure path does not advance
   `_manifest_minted_at`, so every subsequent request retakes the lock, retries the signature,
   and logs a full traceback. Harmless at Ed25519 speed; a self-inflicted request-serialization
   stall the moment signing moves behind a KMS or HSM.
3. **The push path is out of scope, and should say so.** `/admin/enroll-with-cp` sends fresh
   bytes, but the control plane *stores* them and nothing re-enrolls after a re-mint — and its
   `listAgents` drops rows whose `manifest_expires_at` has passed. So an agent enrolled at
   startup silently vanishes from the CP registry at `ttl_secs` while its own endpoint serves
   a perfectly fresh manifest. Pre-existing and not made worse by the re-mint (before it, the
   stored and served copies expired together), but the "peer caching manifest bytes" case the
   assumption called hypothetical is real, one repo over.

## P11 — `external-enrollment` flaked once in the e2e stack
**From:** Phase 6 CI · **Blocks:** nothing · **Cost:** a watch item, not a fix

On one `docker-compose e2e` run, `intra-org/external-enrollment` failed at its `self_enroll`
step with `All connection attempts failed`; 9 of 10 scenarios passed, and a re-run of the same
job went green. Not reproducible so far.

Recorded rather than dismissed because **Phase 6 may have narrowed the window**. Agents now
make a blocking control-plane fetch during lifespan start-up (the refresh that must complete
before `AITP_AGENT_READY`), and `self_enroll` fires within ~15ms of ready. The revocation
fetch to the *same* CP succeeded 15ms earlier in the failing run, so the CP was reachable —
which points at a CP-side transient (a cold Next.js route, a connection pool) rather than our
change. But "the agent now talks to the CP twice in quick succession at start-up, where it
used to talk once" is a real difference, and this is the first flake seen there.

**If it recurs:** capture the agent's stderr around `cp.enroll_started` — the distinction that
matters is whether the failure is agent→CP (CP-side) or engine→agent (our start-up path). If
the latter, the start-up refresh is holding the event loop longer than the supervisor's
readiness signal implies.

**Updated 2026-08-28 — the reason this was undiagnosable is now fixed, the flake itself is
still open.** `plans/audit-2026-08-28-cleanup.md` Phase 5 found why "capture the agent's
stderr" above had nothing to capture: `/admin/enroll-with-cp`'s `cp.enroll_failed` event fired
only for a non-success HTTP *status*; a transport exception (`All connection attempts failed`
is exactly that shape) out of either `client.post` was uncaught, surfaced as a bare 500, and
emitted nothing. Both posts now go through `_post_to_cp_or_502`, which emits
`cp.enroll_failed(stage=..., transport=...)` on exactly this failure and returns 502. A
recurrence now yields the agent→CP vs engine→agent discriminator this entry originally asked
for, in the events stream rather than by inference from stderr. Not closing P11 itself — it
remains a non-reproducible flake with no confirmed root cause; what changed is that it is now
diagnosable if it recurs.

## ~~P12 — `internal_docs/agents.md` still describes the pre-Phase-6 deny-set~~ — **CLOSED 2026-08-28**
**From:** Phase 8 verification (round 2, Opus) · **Closed by:** the worker-scaffolding snippet
and route list in `internal_docs/agents.md` rewritten to match `agents/researcher/main.py`
(`RevocationState`, construction order, `manifest_provider`), and `docs/aitp-integration.md`'s
condensed `verify_capability_tct` snippet + Revocation section brought current — it had drifted
back out of sync with itself after the Phase-8 sweep only fixed the prose sections, not this
snippet 100 lines below them.

`internal_docs/agents.md:131-138` showed agent wiring as `_revoked_jtis: set[str] = set()`
/ `revoked_jtis=_revoked_jtis`. The real code is `RevocationState()` / `revocation=_revocation`
(`agents/researcher/main.py:24-37`, identical in `writer/` and `analyzer/`). This was leftover
Phase 6 drift — the last place in the repo that still described the deny-set as a bare
monotonic set, which is precisely the structure Phase 6 decomposed (CP-derived and local sets
held separately, unioned at enforcement).

## P13 — Two more doc gaps found while closing P12, out of its scope
**From:** P12 close-out · **Status:** OPEN (P13.1) — **P13.2 corrected 2026-08-28, see below**

Found while checking `internal_docs/agents.md` and `docs/aitp-integration.md` for the same
staleness class as P12; neither is the same defect (no claim citing a closed ticket), so
neither was folded into that fix:

1. `internal_docs/agents.md`'s `build_admin_router` route list is missing six live routes —
   `/admin/held-tct`, `/admin/renew-tct`, `/admin/export-session-bundle`,
   `/admin/verify-session-bundle`, `/admin/process-renewal`, `/admin/enroll-with-cp`
   (`agents/base/agent_admin.py:287,304,357,428,446,605`) — and the repo-layout tree omits
   `oidc.py` and `tct_claims.py`. **Still open** — Phase 11 of
   `plans/audit-2026-08-28-cleanup.md` closes this.
2. ~~**Axis B (`revocation_fail_mode` / `revocation_max_staleness_secs`) is undocumented in
   every public doc.** `grep -rln "fail_mode" docs/ internal_docs/ README.md` returns nothing;
   both settings exist only in `src/aitp_playground/config.py` and `hosting/bootstrap.py`. A
   real section, not a one-line mention.~~

   **Corrected and closed 2026-08-28.** That grep is now stale — `docs/aitp-integration.md:255`
   and a substantial explanatory paragraph at `:277-288` already cover `fail_closed` vs
   `soft_fail` and the Axis A/B separation correctly. The conceptual gap this item named does
   not exist. What IS still missing, verified fresh: none of `CP_AID`, `REVOCATION_FAIL_MODE`,
   `REVOCATION_MAX_STALENESS_SECS`, `REVOCATION_POLL_SECS` appear in
   `docs/getting-started.md`'s env table or `.env.example` — an environment-variable gap, not a
   conceptual one. `CP_AID` appears in prose in five docs but is never listed as a variable a
   deployment sets — the one that matters most, since an empty value silently discards every
   revocation snapshot. Closed by `plans/audit-2026-08-28-cleanup.md` Phase 10, which added all
   four to both the env table and `.env.example`.
