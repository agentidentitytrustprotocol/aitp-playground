# PENDING — deferred items from the revocation-verification work

Items deliberately **not** done during the `/drive` run of
`../aitp-control-plane/plans/cross-repo/aitp-playground-revocation-verification.md`.
Each names why it was deferred and what closing it would take, so none of them
depends on remembering a conversation. Decisions live in `DECISIONS.md`;
still-open judgement calls live in `ASSUMPTIONS.md` as `UNCONFIRMED`.

## P1 — Pin identity, not just authenticity, at the CP trust-anchor site
**From:** Phase 2B · **Blocks:** nothing · **Cost:** small code, medium fixture rework

`src/aitp_playground/runner/engine.py` verifies the agent manifest before pinning
`identity_hint.public_key` into the control plane's trust store, but does **not** assert
`manifest["aid"] == ra.aid`. Whatever answers at a launched agent's port can therefore still
get its key pinned *under that agent's AID*.

Closing it needs `FakeSupervisor` (`tests/unit/test_engine_run.py`) to issue real AITP AIDs
instead of synthetic `aid-<agent_id>` strings, which three other tests assert on. That rework
belongs with Phase 6's expected-issuer pinning, where real AIDs are the subject anyway.

## P2 — A skipped interlock keeps CI green
**From:** Phase 2 · **Blocks:** nothing · **Cost:** one line, one judgement call

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

The real fix is Phase 5's ask: typed exceptions or a stable `.code` attribute on the binding.
Revisit here once that lands.

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

**To close:** add `docker-compose e2e` to `main`'s required contexts, then open one PR that
touches `uv.lock` and one that does not, and confirm the first blocks on e2e while the second
merges normally. Both halves need demonstrating — the second is the one that breaks if the
skip semantics are wrong.

## P8 — Phase 6 (verify the snapshot in the production path) is blocked on an aitp-sdk **release**
**From:** Phase 6 · **Blocks:** the plan's headline security deliverable · **Cost:** the phase itself, once unblocked

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

**Sequence to unblock:**
1. Merge `aitp-rs#90`.
2. Release `aitp-sdk` 0.6.0 to PyPI (the existing `aitp-py-release.yml` cascade).
3. In this repo: raise the floor to `>=0.6.0` and let `uv lock` regenerate.
4. Then run Phase 6 — its plan section is fully written, including the prerequisite the
   original draft missed (see below).

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
