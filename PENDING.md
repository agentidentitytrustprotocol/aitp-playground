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

## P3 — `aitp-verifier` is vendored, not depended on
**From:** Phase 2 · **Blocks:** nothing · **Cost:** delete a file, add a dev dependency

`tests/unit/_jcs_reference.py` is a 201-line verbatim copy of
`aitp-verifier-py/aitp_verifier/jcs.py`, because that package is not on PyPI (404) and a
path dependency on a sibling checkout would make this repo's unit suite depend on a repo CI
does not check out. If `aitp-verifier` is ever published, delete the copy and take a
dev-group dependency instead. The copy must stay independent of `aitp-sdk` either way — that
independence is the whole point.

## P4 — `expired` is classified by substring-matching an SDK error message
**From:** Phase 2B · **Blocks:** nothing · **Cost:** depends on Phase 5

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

## P6 — `aitp-cp` is a symlink to `aitp-control-plane`
**From:** Phase 3 · **Blocks:** nothing · **Cost:** awareness only

`aitp-cp` → `aitp-control-plane` (a symlink, not a second checkout). Anything that changes
the branch of one changes what the other builds — `docker-compose.test.yml` builds the CP
from `../aitp-cp`, so switching branches in `aitp-control-plane` silently changes the control
plane the e2e stack tests. Worth knowing before debugging a "works in CI, fails locally"
divergence: CI checks the CP out fresh into its own path and has no such coupling.

## P7 — `docker-compose e2e` runs pre-merge but does not *block* merge
**From:** Phase 4 · **Blocks:** the point of Phase 4 · **Cost:** one branch-protection change — **your call, not mine**

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
0.x the minor is the breaking position); `aitp-sdk` 0.6.0 is on PyPI; the floor here is
`>=0.6.0` and the suite runs **490 passed, 0 skipped** against the pinned wheel.

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

## P9 — `revocation-via-cp` never enforces from the CP-derived deny-set
**From:** Phase 7 (found while correcting the docs) · **Blocks:** nothing · **Cost:** one scenario step

The scenario's summary claimed the writer's follow-up probe is rejected *because* the CP
advertised the revocation. It is not. `blocked_call` is **writer → researcher**, so its 403
comes from the **researcher's local** deny-set, written directly by `/admin/revoke-tct`. It
would fire identically with the entire CP path broken.

The writer's CP-derived deny-set is never consulted on that call: `/admin/invoke` attaches the
held TCT unconditionally, and a deny-set is only read by the peer *serving* a capability
(`verify_capability_tct`). So the CP half is observable only as `audience_revoked_count` and
the `revocation.list_fetched` event — the data arrives, and nothing in the run depends on it.

The summary and the docs that echoed it now say this plainly. What is still missing is the
step that would make the claim true: **a call INTO the writer, presenting the revoked jti,
after the writer has refreshed from the CP** — so the writer's CP-derived deny-set is the
thing that produces the 403. That is the federation story the scenario is named for, and it is
one step away.

Worth doing together with Phase 6: once snapshots are verified, this step is what proves
verification is load-bearing rather than decorative.

## P10 — Manifest re-mint: three refinements to the same trigger
**From:** reconcile of the Phase 2B freshness decision (`DECISIONS.md` D-9) · **Blocks:** nothing

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
