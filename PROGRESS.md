# PROGRESS — revocation-snapshot verification

**Plan:** `../aitp-control-plane/plans/cross-repo/aitp-playground-revocation-verification.md`
(tracked as of aitp-control-plane PR #55; GitHub blob URLs resolve from this repo's issues)
**Tracking issue:** `aitp-playground#46`
**Branch:** `feat/revocation-snapshot-verification` (off `main` @ `5789098`)
**Driver:** `/drive` session `67845297`, cross-repo writes authorized by the user for this run.

## Phase order and status

Plan's own recommended order, not numeric order. Phase 5 lands in `aitp-rs`, not here.

| # | Phase | Repo | Verifier tier | Status |
|---|-------|------|---------------|--------|
| 1 | Pin the SDK floor to 0.5.0 | playground | Sonnet (version/config) | **DONE** |
| 2 | Signing-convention interlock in the unit suite | playground | Fable | **DONE** |
| 2B | Verify peer manifests at both ingest sites | playground | Fable | **DONE** |
| 3 | e2e stack + published image use the pinned wheel | playground | Sonnet | **DONE** |
| 5 | Bind `verify_revocation_list` in Python + Node SDKs | **aitp-rs** | Fable | **DONE** — [aitp-rs#90](https://github.com/agentidentitytrustprotocol/aitp-rs/pull/90) |
| 4 | Run e2e pre-merge on SDK-bump PRs | playground | Sonnet | **DONE** (criterion 4 closed 2026-08-28) |
| 6 | Verify the snapshot in the production revocation path | playground | Fable | **DONE** — both axes |
| 7 | Correct the docs | playground | Opus | **DONE** |
| 8 | Close the second revocation ingest, in `src/` | playground | Opus | **DONE** |

Phase 6 is blocked on Phase 5 (cross-repo). Phase 7's revocation half tracks Phase 6;
its manifest half tracks Phase 2B.

## Repo map

The plan carries the authoritative map ("## Repo map — aitp-playground"). Condensed:

**Revocation path** — `agents/base/revocation_refresh.py` `refresh_revocations()` is the
single ingest and the only verify decision point (`aitp.verify_revocation_list` against the
pinned `CP_AID`; discard on failure) · `agents/base/revocation_state.py` `RevocationState`
holds the result and verifies nothing by design, keeping CP-derived and local jtis in
separate sets unioned at enforcement · reached from `/admin/refresh-revocations`, the
start-up refresh, and the background poll · `agents/base/aitp_server.py` deny-set
enforcement. **`CpClient` has no revocation-fetch method** — the signature-blind
`fetch_revocation_list` was deleted in Phase 8.

**Manifest path** — `agents/base/agent_admin.py:172` (handshake) and `:516` (delegatee), both
verified via `_verify_peer_manifest` · `src/aitp_playground/runner/engine.py:587` (the
CP-trust-anchor site — verified as of the 2026-08-28 audit cleanup, Phase 4; previously the one
unverified ingest site) · `src/aitp_playground/trust/resolver.py:33-51`.

**Config plumbing** — `src/aitp_playground/config.py:32` (`cp_base_url`), `:46` (`cp_aid`, the pin) →
`src/aitp_playground/hosting/bootstrap.py:39-43` (`cp_block`) → agent subprocess `bootstrap["cp"]`.

**Build & CI** — `pyproject.toml:23` (floor) · `Dockerfile:33-51` (maturin from `../aitp-rs`)
· `.github/workflows/ci.yml:50-53` (installs the PyPI wheel — the only tier that sees the pin)
· `.github/workflows/docker.yml:46-50,:93` (build-and-push, unpinned aitp-rs@main) · `:107` e2e
job, main-push only · `.github/workflows/auto-merge.yml` (a green bump PR auto-merges).

## Checkpoints

### Phase 1 — Pin the SDK floor — 2026-08-25 — PASS (1 round)
- **Verifier:** Sonnet. Tier rationale: a specifier bump plus a comment — mechanical, with a
  well-defined right answer. It did more than asked: reproduced the `uv sync --locked` failure
  from first principles and checked every factual claim in the new comment against
  `aitp-rs/CHANGELOG.md`.
- **Files:** `pyproject.toml` (specifier `>=0.4.0` → `>=0.5.0`, floor comment rewritten),
  `uv.lock` (1 line: recorded specifier).
- **Gaps:** none.
- **Plan defect found and recorded inline:** acceptance criterion 3 ("no modification to
  `uv.lock`") is unsatisfiable alongside criterion 1 — uv mirrors the declared specifier string
  in lock metadata, so any specifier edit forces a regen. Superseded in the plan by the
  criterion that carries the actual intent: the *resolved* version must not move. It didn't
  (0.5.0 before and after, 1-line total lock diff, no other package moved).
- **Unplanned fix:** the pre-existing comment said "Floor pinned to 0.3.0" while the dependency
  read `>=0.4.0`. Corrected rather than built upon.
- **Tests:** 440 passed (`tests/unit tests/scenarios`), ruff clean, `uv sync --locked` OK.
- **Learned, needed by Phase 3:** `aitp` exposes **no `__version__`**. Phase 3's acceptance
  criterion 2 must assert `importlib.metadata.version("aitp-sdk")` (→ `0.5.0`) instead.
- **Next:** Phase 2 — signing-convention interlock.

### Phase 2 — Signing-convention interlock — 2026-08-25 — PASS (1 round)
- **Verifier:** Fable. Tier rationale: the whole value of this phase is that its negative
  assertions can actually fire; a plausible-but-vacuous interlock is worse than none.
- **Files:** `tests/unit/test_revocation_signing_convention.py` (new, 5 tests),
  `tests/unit/_jcs_reference.py` (new, vendored RFC 8785 canonicalizer + provenance header).
  `tests/unit/test_cp_client.py` unchanged, as the plan requires.
- **Gaps:** none blocking. One plan edge case was **not** implemented in the first pass and
  the verifier caught it: "assert the absence of a [signature] prefix rather than assuming
  it." Closed in-phase — `_assert_untagged_signature` plus a test proving it fires on a
  `p256.`-tagged signature. Without it a tagged signature would decode to garbage and report
  "SDK does not sign the inner body": a true failure with a misleading cause.
- **Mutation check (criterion 6), run not reasoned:** 0.4.1 → RED on `inner_body` with the
  intended diagnostic; 0.5.0 → green. **The obvious way to run it silently no-ops**: `uv run`
  re-syncs to the lockfile first, so the naive invocation tests 0.5.0 and reports green.
  `uv run --no-sync` is required. Recorded in the plan.
- **Convention table, probed under both wheels:** 0.4.1 → `wrapped`; 0.5.0 → `inner_body`.
- **Plan correction recorded inline:** `self_inclusive` cannot be shown non-vacuous by
  construction (fixed point); implemented as a distinctness check + documented limit instead.
- **Tests:** 445 passed (440 baseline + 5), ruff clean, `uv.lock` still only Phase 1's line.
- **Next:** Phase 2B — verify peer manifests at both ingest sites.

### Phase 2B — Verify peer manifests — 2026-08-25 — PASS (2 rounds)
- **Verifier:** Fable. Round 1 → GAPS (6 items); round 2 → PASS, anchored on the gap list.
- **Files:** `agents/base/agent_admin.py` (async `_verify_peer_manifest`, both ingest sites),
  `agents/base/aitp_server.py` (`_fresh_manifest_json` + lock), `agents/{researcher,analyzer,
  writer}/main.py` (`manifest_provider`), `src/aitp_playground/runner/engine.py` (third
  ingest site + a shadowing local import removed), `tests/unit/test_manifest_verification.py`
  (new, 14 tests), `tests/unit/test_engine_run.py` (fixture now mints a real manifest).
- **Round 1 gaps, all closed:** (1) no named telemetry event — now `manifest.verify_failed`
  with a `cause`; (2) no expiry test; (3) deviations unlogged; (4) a **third** blind ingest at
  `engine.py` pinning a CP trust key; (5) a rotate-keys × re-mint race; (6) `enroll-with-cp`
  bypassing the freshness fix.
- **Scope widened deliberately:** the plan covers ingest only. Turning on verification while
  our own served manifest expires at `ttl_secs` would have shipped a worse bug than the one
  fixed — every agent alive past an hour serving a manifest all verifying peers reject. Both
  halves are one property; splitting would have left a knowingly-broken window. Logged.
- **Deferred, logged:** the `engine.py` site verifies authenticity but not identity
  (`manifest.aid == ra.aid`). The pin needs the fake supervisor to issue real AIDs, which
  three other tests assert on — a fixture rework belonging to Phase 6.
- **Mutation checks (run, not reasoned):** forcing `_verify_peer_manifest` to raise turns the
  end-to-end scenario RED, proving integration really exercises the path; replacing the
  rotation guard with `if False:` turns its test RED.
- **A verifier claim that was wrong:** round 1 reported "a ttl=1 manifest raises 502". It does
  not — `ttl_secs` of 0 and 1 both verify (`expires_at == now` is not in the past). The expiry
  test uses `ttl_secs=-1`, which is deterministically expired with no sleep. Checking beat
  trusting.
- **Incident:** the round-2 verifier ran `git checkout --` on `aitp_server.py`, destroying
  uncommitted work, then reconstructed it from memory. Audited line-by-line against intent
  before accepting — the full functional diff matches exactly, nothing added or lost — and
  all suites re-run independently.
- **Tests:** 459 unit/scenario, 4 integration, ruff clean.
- **Next:** Phase 3 — e2e and published image use the pinned wheel.

### Phase 3 — Pinned wheel in e2e and the published image — 2026-08-25 — PASS (2 rounds)
- **Verifier:** Sonnet (CI/CD + build config tier). Round 1 → GAPS (4 items), round 2 → PASS.
- **Files:** `Dockerfile` (AITP_SDK_SOURCE switch, two builder stages), `.github/workflows/
  docker.yml`, `tests/integration/test_protocol_e2e.py` (`test_sdk_version_matches_lock`),
  `internal_docs/docker.md`, `internal_docs/testing.md`.
- **The gap that mattered:** I removed the sibling `aitp-rs` checkout from *both* jobs. That
  is right for `build-and-push` and **wrong for `e2e`** — `Dockerfile.cp-e2e:46-48` compiles
  the *control plane's* napi binding from `aitp-rs`, a separate dependency I conflated with
  the playground's. Local runs could never catch it: `aitp-rs` is on disk here whatever CI
  does. Restored to `e2e` only.
- **Verified against real built images:** wheel is `aitp_sdk-0.5.0` matching `uv.lock`;
  `/capabilities` byte-identical across `pypi` and `path`; `path` still compiles; no Rust
  toolchain on the default path.
- **Unverified locally, disclosed:** criterion 4 (full compose stack). The `aitp-cp` image
  will not build on this arm64 host — `bindings.lockfileTryAcquireSync is not a function`,
  reproduced with `--no-cache`, unrelated to this repo. `PENDING.md` P5; CI proves it.
- **Also found:** `aitp-cp` is a **symlink** to `aitp-control-plane` (PENDING P6).
- **Tests:** 459 unit/scenario, ruff clean, docker.yml valid YAML.
- **Next:** Phase 5 — bind `verify_revocation_list` in aitp-rs.

### Phase 5 — Bind verify_revocation_list (aitp-rs) — 2026-08-25 — PASS (2 rounds)
- **Verifier:** Fable. Round 1 → GAPS, round 2 → GAPS (one stale CHANGELOG line), then closed.
- **Shipped as** `aitp-rs` PR #90 on `feat/bind-verify-revocation-list`.
- **The gap that mattered — I shipped the defect this phase exists to remove.** The first Node
  pass used `Error::new(Status::GenericFailure, "{code}: {message}")`, so `error.code` was
  **always** `"GenericFailure"` and the cause was recoverable only by parsing the message —
  with a comment above it claiming napi surfaced the status as `code`. True of napi, false of
  my code. Fable caught it by probing live rather than reading the comment. Fixed with
  `Env::throw_error(&msg, Some(code))`.
- **A plan claim that was wrong:** `TctError::IssuerMismatch` already existed; no new variant
  was needed. One-line fix.
- **Recorded honestly:** the conformance adapter's emitted wire code for a revocation issuer
  mismatch moved `INVALID_ENVELOPE` → `TCT_SIGNATURE_INVALID`. Unpinned by any fixture, but
  "matching callers are unaffected" was too strong.
- **Tests:** 88 Rust groups, 48 Python, 49 Node; fmt + clippy `-D warnings` clean.

### Phase 4 — Pre-merge e2e on SDK-bump PRs — 2026-08-25 — PASS (1 round + fixes)
- **Verifier:** Sonnet. Verdict GAPS; three of four closed in-phase, one deferred by design.
- **Files:** `.github/workflows/docker.yml` (new `changes` job, widened `e2e` trigger),
  `internal_docs/testing.md`.
- **Criterion 4 — met 2026-08-28.** Was NOT met at the time and was not silently patched:
  `docker-compose e2e` was absent from `main`'s required contexts, so it ran pre-merge but
  blocked nothing. Closed once the user made the call: added to `main`'s required status
  checks via the `required_status_checks` sub-resource `PATCH`, and demonstrated both
  directions on live PRs rather than assumed — a `.md`-only PR (#53) skipped the check cleanly
  and stayed mergeable; a throwaway `uv.lock`-touching PR (#54) blocked while the job ran and
  went mergeable only once it passed, then was closed unmerged. See `DECISIONS.md` D-12
  (reversed) and D-13, `PENDING.md` P7 (closed).
- **Filter tightened to `uv.lock` alone**, using what Phase 1 proved: uv mirrors the declared
  specifier into lock metadata, so any dependency edit moves `uv.lock`, while `[tool.ruff]`
  and the LLM extras — same file, unrelated churn — do not. Sufficient *and* cheaper.
- **`always()` added** so a failure in the cheap filter job cannot starve the push-to-main run.
- **Next:** Phase 6 — verify the snapshot in the production revocation path.

### Phase 6 — BLOCKED, not attempted — 2026-08-25
- Needs `aitp.verify_revocation_list`. Installed `aitp-sdk` is 0.5.0 and PyPI's latest is
  0.5.0; the surface exists only in `aitp-rs#90`, which is open. So the blocker is a
  **release**, not a merge.
- Not started deliberately. Writing against a surface the pinned wheel lacks would raise
  `AttributeError` at runtime; raising the floor to a version that does not exist is not
  shippable; and a capability-probe fallback is the unchecked posture the phase exists to
  remove. Landing only its deny-set restructure would leave a semantic change to shared state
  on `main` with no caller.
- Full unblock sequence + the prerequisite the original plan missed: `PENDING.md` P8.

### Phase 7 — Correct the docs — 2026-08-25 — PASS (2 rounds)
- **Verifier:** Opus. Round 1 → GAPS (7 items), round 2 → GAPS (3), then closed.
- **Files:** `docs/aitp-integration.md`, `docs/control-plane.md`, `docs/scenarios.md`,
  `docs/README.md`, `docs/architecture.md`, `README.md`,
  `scenarios/intra-org/revocation-via-cp/1.0.0/scenario.yaml`, plus two source docstrings.
- **The finding that mattered was not a wording problem.** The `revocation-via-cp` summary
  claimed the writer's probe is rejected *because* the CP advertised the revocation. It is
  not: `blocked_call` is writer → researcher, so the 403 is the **researcher's local**
  deny-set and would fire with the entire CP path broken. The CP-derived deny-set is never
  consulted. Corrected in the scenario and everywhere that echoed it; the missing step is
  `PENDING.md` P9.
- **The boundary sentence was rewritten rather than patched.** "No envelope is parsed outside
  the SDK" was false for manifests and TCTs. It now reads "nothing security-relevant is
  *decided* outside the SDK", with the three places JSON is read named explicitly — because
  the old sentence had already caused two other docs to call correct behaviour a bug.
- **Round 1 also caught a route that does not exist** (`/admin/build-delegation`; it is
  `/admin/delegate`) — in prose I had written, contradicting the same file 40 lines up.
- **Tests:** 459 passed, ruff clean, scenario YAML validates.

### CI outcome — 2026-08-25
Three PRs, all green:

| Repo | PR | Checks |
|---|---|---|
| `aitp-control-plane` | [#55](https://github.com/agentidentitytrustprotocol/aitp-control-plane/pull/55) | 3 pass |
| `aitp-rs` | [#90](https://github.com/agentidentitytrustprotocol/aitp-rs/pull/90) | 35 pass |
| `aitp-playground` | [#47](https://github.com/agentidentitytrustprotocol/aitp-playground/pull/47) | 7 pass |

**Two things CI settled that local runs could not.**

1. **Phase 3 criteria 2 and 4 are met.** `docker-compose e2e` passed in 5m21s on the `pypi`
   path with `revocation-via-cp` included, and `test_sdk_version_matches_lock` **actually
   executed** (`PASSED`) against a running container — it had never run before. PENDING P5
   closed. The arm64 CP build fault was genuinely local-only.
2. **Phase 4 criterion 1 is demonstrated on a real PR.** This PR touches `uv.lock`, so
   `Detect SDK pin changes` → `sdk_pin=true` → `docker-compose e2e` ran pre-merge. Criterion 2
   (an unrelated PR does *not* trigger it) is still only shown by the filter logic.

**One CI-only failure, now fixed:** `bindings fmt + clippy` failed on aitp-rs #90 while
`cargo fmt --all` was clean locally — the bindings are **separate workspaces** the root
Cargo.toml does not cover, which `ci.yml`'s own comment warns about. I ran the wrong gate.
Fixed in `e86835b`.

### Phase 6 — Verify the snapshot in the production path — 2026-08-26 — PASS
Shipped in three commits: Axis A (`43bfffe`), Axis B (`94a4826`), floor bump.

- **Axis A** — the deny-set restructure (the prerequisite the plan's first draft assumed
  away), verify-or-discard with distinct causes, the `CP_AID` pin threaded to agent
  subprocesses, and the envelope-tolerant parse removed.
- **Axis B** — `fail_closed` by default, a 300s staleness budget, a 60s poll, and posture
  (`unchecked | current | degraded`) evaluated as a pure function.
- **Verifier:** Fable on both axes. Axis B round 1 returned 7 gaps, 3 blocking.

**Three design errors the review and the tests caught, not the plan:**

1. Treating "CP configured but no `CP_AID`" as *degraded* meant `fail_closed` rejected every
   call on any deployment that had not set a brand-new variable — broken-by-default on the
   upgrade that introduces it. A federated e2e test caught it.
2. My fix then folded the SDK capability probe into the same check, silently downgrading a
   deployment that HAD pinned its AID but ran an old wheel. `PENDING.md` P8 forbids exactly
   that by name. A pinned deployment on an old SDK is now degraded, loudly.
3. My cold-start fix (refresh immediately rather than after a full interval) connected to a
   socket uvicorn had not bound yet, breaking the federated handshake tests. Now a short
   grace delay: no 60s window of 403s, no self-inflicted startup error.

Also: criterion 7's test was **vacuous** — it asserted a jti it never added was absent, and
would have passed even if `soft_fail` did rescue forgeries. It now presents a real
attacker-signed snapshot. An unrecognized `fail_mode` now fails closed. `CP_AID` is pinned in
the compose stack, without which the shipped demo ran unchecked.

- **Final:** **490 passed, 0 skipped** against the published `aitp-sdk` 0.6.0 — every test
  that had been skipping is now real coverage. 4 integration, ruff clean, `uv sync --locked`
  clean.


### Phase 8 — Close the second revocation ingest, in `src/` — 2026-08-27 — PASS (2 rounds)

Added by the plan's [R3] refresh after phases 1-7 shipped; tracked as `aitp-playground#51`.

- **Verifier:** Opus, both rounds (user constraint for this run: Opus/Sonnet only, no Fable).
  Tier rationale: the acceptance criteria are repo-wide trust-boundary greps — "no claim
  anywhere states a signature is unchecked" — not a mechanical line removal, so the review
  had to be independent enough to hunt claims the executor never thought to grep. It was:
  round 1 found 7 stale sites and 2 errors in the executor's own rewrites.
- **Decision:** option (1) DELETE, chosen by the user from the plan's two mutually exclusive
  options. `CpClient.fetch_revocation_list()` had no production callers — only its own tests.
- **Files:** `src/aitp_playground/cp_client/client.py` (method deleted, -39),
  `tests/unit/test_cp_client.py` (its 4 tests, -60), `agents/base/aitp_server.py` (comment
  reworded off a closed ticket), `docs/aitp-integration.md`, `docs/control-plane.md`,
  `docs/scenarios.md`, `README.md`,
  `scenarios/intra-org/revocation-via-cp/1.0.0/scenario.yaml`, `PENDING.md` (P12 logged),
  `ASSUMPTIONS.md` (1 UNCONFIRMED).

**Round 1 — GAPS (9 items).** The code deletion was clean, but the phase's real content was
the claim sweep, and the executor had grepped only `src/`, `agents/` and two docs:

1. **7 stale sites still asserting the signature is unverified**, all citing the closed
   `#46` / `PENDING.md P8`: the scenario YAML's summary *and* step description (the worst —
   runtime-visible through the `/scenarios` API), `README.md`, `docs/control-plane.md:54`
   and `docs/aitp-integration.md:361` (each making a file the executor had already edited
   contradict itself ~60 lines away), and `docs/scenarios.md` twice.
2. **Two claims the executor's own rewrites introduced, both too strong** — the exact defect
   class this plan exists to remove, committed while fixing it:
   - Credited `RevocationState` as the verifier, in the wrong file. The verifier is
     `refresh_revocations()`; `RevocationState`'s own docstring says it "deliberately
     performs no verification". A boundary doc naming who verifies had named the one type
     documenting that it doesn't.
   - Asserted the agent "verifies against the pinned `CP_AID`" without noting `cp_aid`
     defaults to `""`, where the snapshot is discarded and nothing propagates at all.

**Round 2 — PASS.** All nine confirmed closed with file:line evidence. One new minor item
(the "reached via `/admin/refresh-revocations`" clause named one of three callers) fixed
rather than carried.

**What this phase actually was.** Deleting the method was ~15 minutes; the value was the
sweep. The plan predicted this in criterion 2 — *"the check that would have caught the
drift: the claim outlived the condition"* — and the drift was real in 7 places, one of them
shown to users at runtime. It also demonstrated the failure mode twice over: two of the
sweep's own replacement claims were themselves too strong on the first pass.

**Out of scope, logged not absorbed:** `PENDING.md` P12 (`internal_docs/agents.md` still
describes the pre-Phase-6 monotonic deny-set) and one `ASSUMPTIONS.md` entry (the scenario's
*second* caveat, which cites no closed ticket and was verified still true).

- **Suite:** 497 passed, 21 skipped (all env-gated live-stack tests, pre-existing),
  ruff clean, `cli validate` green on the edited scenario.

## Plan 2 — 2026-08-28 audit cleanup

**Plan:** `plans/audit-2026-08-28-cleanup.md` (local, gitignored). Follows an adversarial
file:line audit run after the revocation-verification effort above closed. 12 phases, executor
Sonnet with a per-phase verifier tier (Fable/Opus where a negative assertion must be shown to
fire, Sonnet where mechanical). Branch `chore/audit-2026-08-28-cleanup`.

### Phase 1 — SDK-floor citation rot — 2026-08-28 — PASS
- **Verifier tier:** Sonnet (four version strings, one right answer).
- **Files:** `pyproject.toml` (new 0.7.0 bullet in the floor-rationale comment, citing
  `agent_admin.py:93-95` and `aitp_server.py:576-593`), `docs/getting-started.md:41`
  (`>=0.4.0` → `>=0.7.0`), `DECISIONS.md:126` (D-11's version citation), `PENDING.md:217-218`
  (P8's closing sentence — reworded to mark it historical rather than a claim about the
  current floor).
- **Acceptance:** `grep -rn "0\.6\.0|0\.4\.0" pyproject.toml docs/getting-started.md` returns
  only the historical-rationale bullets (0.6.0's own bullet, by design). `uv.lock` unchanged
  (`git diff --stat uv.lock` empty). `uv sync --locked` clean.
- **Tests:** 497 passed, unchanged from baseline (no test touches version strings).
- **Next:** Phase 2 — test-suite integrity.

### Phase 2 — Test-suite integrity: real ingest under test, fourth sibling guard — 2026-08-28 — PASS
- **Verifier tier:** Fable/Opus (D-2: the phase's value is entirely whether the new
  assertions can fire).
- **Files:** `tests/unit/test_revocation_verify_or_discard.py` (rewritten — `_apply` is now a
  thin wrapper over the real `revocation_refresh.refresh_revocations`, transport stubbed via a
  module-local `httpx` rebind so `agent_admin`'s shared `httpx` import is untouched; 3 new
  negative-case tests), `tests/unit/test_revocation_signing_convention.py` (drift guard's
  `pytest.skip` → `CI`-conditional hard assertion), `tests/unit/_jcs_reference.py` (header no
  longer claims CI doesn't clone the sibling).
- **Part A — real ingest under test.** `grep -rn "refresh_revocations" tests/` previously
  matched nothing; the nine existing tests now drive the production function through a
  `MockTransport`-backed `httpx.AsyncClient`, with `revocation_refresh`'s own module-global
  `httpx` name rebound via `monkeypatch.setattr(revocation_refresh, "httpx", ...)` — this
  rebinds only that module's local name, not the shared `httpx` module object other importers
  see.
- **Mutation results, all run not reasoned:**
  | Guard deleted / aliased | Result |
  |---|---|
  | `no_expected_issuer` (`revocation_refresh.py:103-108`) | RED — 1 failed |
  | `sdk_cannot_verify` (`:109-114`) | RED — 1 failed |
  | Transport-failure emit aliased to `revocation.verify_failed` | RED — 1 failed |
- **Post-verification parse guard (Phase 2's item 4) deliberately deferred to Phase 5**, per
  the plan: asserting today's `KeyError`-raising behaviour here would need rewriting the moment
  Phase 5 lands the fix. Not forgotten — tracked in the plan.
- **Part B — fourth sibling guard.** `test_the_vendored_canonicalizer_has_not_drifted_from_its_source`
  skipped silently when the sibling checkout was absent, with a docstring claiming CI doesn't
  clone it — false, `ci.yml` clones `aitp-verifier-py` specifically as this guard's real gate
  (`PENDING.md` P3's close-out). Unlike D-11's three wheel-surface guards, made `CI`-conditional
  rather than unconditional (see `DECISIONS.md` D-11 addendum for why). Demonstrated live with
  the sibling checkout temporarily renamed: `CI=1` + absent → 1 failed; `CI` unset + absent → 1
  skipped; sibling restored → 1 passed (real comparison).
- **Tests:** 500 passed (497 + 3 new negative cases), ruff clean.
- **Next:** Phase 3 — `revocation.verify_failed` survives the poll.

### Phase 3 — `revocation.verify_failed` survives the poll — 2026-08-28 — PASS
- **Verifier tier:** Opus (a telemetry-volume judgement call).
- **Decision:** Candidate 1 (`_discard` emits unconditionally; `quiet` still governs
  `list_fetched`/`refresh_failed`) — see `DECISIONS.md` D-14 for the full reasoning and the
  two rejected alternatives.
- **Files:** `agents/base/revocation_refresh.py` (`_discard`'s `if not quiet:` removed; both
  docstrings corrected to state what `quiet` actually suppresses), `agents/base/aitp_server.py`
  (poll-loop docstring corrected — it no longer implies `revocation.poll` is the only
  steady-state signal), `agents/base/agent_admin.py` (the admin route's `quiet` param doc
  states the exemption), `tests/unit/test_revocation_verify_or_discard.py` (3 new tests).
- **Mutation result, run not reasoned:** restoring `if not quiet:` inside `_discard` turns
  `tests/unit/test_revocation_verify_or_discard.py` red (1 failed, `IndexError` on the now-empty
  captured-events list) — demonstrated, then the file was restored from a pre-mutation copy
  rather than `git checkout --`, which would have discarded this phase's own uncommitted edits
  along with the mutation (it did, once, mid-phase; redone from a fresh read of the reverted
  file).
- **Tests:** 503 passed (500 + 3), ruff clean.
- **Next:** Phase 4 — one verify-failure taxonomy across all three manifest ingest sites.

### Phase 4 — One verify-failure taxonomy across all three manifest ingest sites — 2026-08-28 — PASS
- **Verifier tier:** Opus (a module-placement decision a mechanical executor gets wrong in a
  way only local `uvicorn` catches, not the test suite).
- **Decision:** Candidate C — mirror the classifier, pin with a parity test — over a shared
  import (breaks local dev; see `DECISIONS.md` D-15 for the two path facts that rule it out).
- **Files:** `agents/base/agent_admin.py` (classifier extracted to module-level
  `classify_manifest_verify_failure`, no behaviour change), `src/aitp_playground/runner/engine.py`
  (new `_classify_manifest_verify_failure` mirror; `cp_provision_trust_anchor`'s
  `aitp.verify_manifest_json(mr.text)` call now wrapped, emitting `manifest.verify_failed` and
  raising `PlaygroundError` with a named cause instead of letting the raw SDK error propagate),
  `src/aitp_playground/runner/context.py` (`RunEvent` gained `cause`/`source_url` fields —
  pydantic's default `extra="ignore"` was silently dropping them, confirmed empirically before
  adding), `tests/unit/test_engine_run.py` (new: tampered-manifest-with-matching-AID case),
  `tests/unit/test_manifest_verification.py` (new: 4-case classifier parity test).
- **Mutation results, all run not reasoned:**
  | Check | Result |
  |---|---|
  | Delete the `try`/classify/emit wrapper at `engine.py:587` | RED — 1 failed |
  | Swap one classifier's branch order (parity test) | RED — 3 failed |
- **`uv run uvicorn aitp_playground.main:app` starts clean with no `PYTHONPATH` set** —
  confirmed live (`GET /capabilities` → 200), ruling out the shared-import approach empirically
  rather than by argument alone.
- **`PROGRESS.md`'s own repo map corrected** (audit B12) — the manifest-path line cited stale
  pre-Phase-2B line numbers and labelled the delegatee site "unverified"; both were wrong.
- **Tests:** 508 passed (503 + 5 — 1 engine test + 4 parametrized parity cases), ruff clean.
- **Next:** Phase 5 — failure observability at the enroll and post-verify-parse boundaries.

### Phase 5 — Failure observability at the enroll and post-verify-parse boundaries — 2026-08-28 — PASS
- **Verifier tier:** Opus (Part A explains a standing `PENDING.md` watch item; deciding what to
  record there is a judgement call).
- **Files:** `agents/base/agent_admin.py` (new `_post_to_cp_or_502` and `_decode_cp_json_or_none`
  helpers; `/admin/enroll-with-cp`'s two `client.post` calls and two `.json()` decodes now
  route through them), `agents/base/revocation_refresh.py` (the post-verification snapshot
  parse wrapped in `try`/`except (KeyError, TypeError, ValueError)`, routed through `_discard`
  as cause `malformed_body`), `tests/unit/test_agent_admin_enroll.py` (new file, 3 tests),
  `tests/unit/test_revocation_verify_or_discard.py` (1 new test — this is Phase 2's deferred
  item 4, landing here in its fixed form).
- **Part A finding worth recording:** the real SDK's own deserialization is strict enough that
  every malformed-body shape tried (missing `expires_at`, non-numeric timestamps, non-UUID
  `jti`) is rejected by `aitp.verify_revocation_list` itself, before the signature check runs —
  so the post-verify parse guard is genuinely unreachable through the real SDK today. The new
  test stubs `verify_revocation_list` to a no-op to isolate this module's own defensive parse
  from the SDK's, which is the only way to demonstrate it fires (D-2).
- **Mutation results, all run not reasoned:**
  | Check | Result |
  |---|---|
  | Remove `_post_to_cp_or_502`'s try/except | RED — 2 failed |
  | Remove the post-verify-parse try/except | RED — 1 failed |
- **`rg "status_code=500" agent_admin.py`** — one match, the pre-existing (and correct)
  wiring-bug guard for a missing `manifest_provider`; no new 500s introduced.
- **Docs:** `PENDING.md` P11 updated, not closed — the flake itself is still unreproduced, but
  the reason its own "capture the agent's stderr" instruction had nothing to capture is now
  fixed (`cp.enroll_failed` fires on transport failure, not just a non-success status). No new
  `DECISIONS.md` entry — Part A applies D-10's existing taxonomy rather than deciding anew.
- **Tests:** 512 passed (508 + 4), ruff clean.
- **Next:** Phase 6 — a cancelled run stays cancelled.

### Phase 6 — A cancelled run stays cancelled — 2026-08-28 — PASS (with a live-found correction)
- **Verifier tier:** Opus (a metrics-double-count trap a mechanical reading would miss — and it
  did initially, see below).
- **Decision:** Candidate 2 (guard the store AND the `run.failed` emit) — but the cancel
  route's kill/mark ordering also had to change, which the plan did not call for. See
  `DECISIONS.md` D-16 for the race found live and the reasoning.
- **Files:** `src/aitp_playground/runner/engine.py` (`_finalize_failure` preserves an
  already-`cancelled` record instead of upserting `failed`; the mid-run exception handler
  skips the `run.failed` emit under the same condition), `src/aitp_playground/api/runs.py`
  (`/cancel` now upserts `cancelled` + emits `run.cancelled` **before** killing subprocesses,
  not after; docstrings corrected), `internal_docs/runner.md` (same ordering fix), new tests in
  `tests/unit/test_engine_run.py` (2) and `tests/unit/test_metrics.py` (1),
  `tests/integration/test_runner.py` (assertion tightened from `in {"cancelled","failed"}` to
  `== "cancelled"`, plus a check that exactly one terminal event lands in the run's log).
- **What the plan got right vs. what live testing corrected:** the plan called for guarding the
  store write and the emit (Candidate 2), which was necessary but not sufficient. Running the
  real `AITP_E2E=1` integration test against the first implementation (guards in place, but the
  route's original kill-then-mark order) reproduced the race live: `supervisor.kill_run` can
  fail the background run's in-flight call fast enough that its guard check reads the store
  *before* the route's own upsert lands, so it still emitted `run.failed` — which then landed
  in the event log ahead of `run.cancelled`. Reordering the route (mark-then-kill) closes the
  race at its source; re-ran 5/5 clean where it failed reliably before.
- **A mutation-testing lesson, recorded in D-16 rather than glossed over:** after the reorder,
  mutating `_finalize_failure` to remove its guard did **not** turn the live E2E test red — the
  reorder narrowed the timing window enough that the same mutation no longer reliably
  reproduces through real subprocess timing. The unit-tier `_finalize_failure` test (store
  pre-seeded directly, no subprocess involved) does turn red on the identical mutation,
  deterministically. The E2E test remains valuable end-to-end corroboration; the unit test is
  the actual mutation gate for this guard.
- **Mutation results:**
  | Check | Vehicle | Result |
  |---|---|---|
  | Remove `_finalize_failure`'s guard | `tests/unit/test_engine_run.py` (deterministic) | RED — 1 failed |
  | Remove `_finalize_failure`'s guard | `AITP_E2E=1` integration test (post-reorder) | did NOT turn red — see above |
- **Live E2E confirmation, 5 consecutive runs:** `AITP_E2E=1 uv run pytest tests/integration/test_runner.py -k cancel_inflight` — 5/5 passed after the reorder.
- **Docs:** `docs/observability.md`'s metric table needed no change — it lists the label set,
  not a reachability claim, so it was already accurate.
- **Tests:** 515 passed (512 + 3 unit-tier), ruff clean.
- **Next:** Phase 7 — negative-case tests for untested rejection branches; federated test into CI.

### Phase 7 — Negative-case tests for untested rejection branches; federated test into CI — 2026-08-28 — PASS
- **Verifier tier:** Fable/Opus (D-2: a test that cannot fail is the thing being fixed — every
  item below was demonstrated by deleting the branch it covers).
- **Files:** `tests/unit/test_delegation_revocation.py` (+4: old-SDK 503, Axis B freshness on
  mint, multihop deny-set, capability-TCT issuer mismatch), `tests/unit/test_agent_admin_routes.py`
  (new file, 7 tests: five 412s, one 500, one 404, plus the session-bundle forgery case),
  `tests/unit/test_federation.py` (+4: non-did:web 400, loopback 409, the opt-out's other
  direction, origin-mismatch 409), `.github/workflows/ci.yml` (integration job now also runs
  `test_federated_handshake.py`).
- **Two audit corrections, both narrower-than-stated, neither needing a new test:** "redeem
  single-hop rejection when `allow_multihop_delegation` unset" was already covered by the five
  existing tests in `test_delegation_revocation.py` (their harness never sets the flag).
  `engine.py:587` manifest authenticity (item 5) was delivered by Phase 4 — not duplicated here.
- **Mutation results, all run not reasoned:**
  | # | Branch | Result |
  |---|---|---|
  | 1 | Old-SDK `TypeError` → 503 (`aitp_server.py`) | RED — 1 failed |
  | 2 | Axis B freshness on the redeem/mint path | RED — 1 failed |
  | 3 | Multihop branch's deny-set argument | RED — 1 failed |
  | 4 | `verify_capability_tct` issuer-AID mismatch | RED — 1 failed |
  | 9 | `AITP_FEDERATION_ALLOW_LOOPBACK` opt-out clause | RED — 1 failed (`tests/unit`, 381 passed) |
  Items 6, 7, 8, 10 are precondition/shape checks with a single obvious failure mode each (the
  guard IS the branch); their tests pin current behaviour directly rather than needing a
  separate delete-and-rerun step.
- **One item's real behaviour was honestly weaker than the rest of the module's taxonomy, and
  the test says so rather than asserting an improvement that wasn't made:**
  `/admin/verify-session-bundle` has no `try`/`except` around `aitp.verify_session_bundle` — a
  tampered bundle raises an uncaught `RuntimeError`, reaching the client as a bare 500 rather
  than the 403/502 shape every other verify site in this module uses. The new test pins that a
  forged bundle IS rejected (never a 200), not that it is rejected *cleanly* — fixing the shape
  is a follow-up, not something to claim was already done.
- **`test_federated_handshake.py` confirmed live, not just wired:**
  `AITP_E2E=1 uv run pytest tests/integration/test_runner.py tests/integration/test_federated_handshake.py -v`
  — 4 passed in 3.47s locally, well inside the 10-minute job timeout. `ci.yml:86`'s job `name:`
  is byte-identical before and after, so this does not trip the D-13 required-check trap.
- **Tests:** 530 passed (515 + 15 new), ruff clean.
- **Next:** Phase 8 — bring `agents/base` inside the coverage gate.

### Phase 8 — Bring `agents/base` inside the coverage gate — 2026-08-28 — PASS
- **Verifier tier:** Sonnet (mechanical once the decision is taken; the verifier re-ran the
  numbers rather than trusting them, per the plan).
- **Decision:** Candidate 2 — two separate `coverage report --include` gates over one
  `coverage run` data file, `agents/base`'s floor set from a fresh measurement (55.2%, minus
  headroom → 54) rather than folding into one aggregate. See `DECISIONS.md` D-17.
- **Files:** `pyproject.toml` (`[tool.coverage.run]` gains `source = ["agents/base"]` — no
  `agents/__init__.py` exists, so it cannot be named via `source_pkgs`), `.github/workflows/ci.yml`
  (one `coverage report --fail-under=88` → two, `--include="src/*"` and
  `--include="agents/base/*" --fail-under=54`).
- **Measured, not estimated, on the post-Phase-7 tree (530 tests):**
  | Scope | Stmts | Miss | Cover |
  |---|---|---|---|
  | `src/aitp_playground` | 3052 | 324 | **89.4%** |
  | `agents/base` | 819 | 367 | **55.2%** |

  Per-module `agents/base`: `agent_admin.py` 46.3%, `aitp_server.py` 55.3%, `bootstrap.py` 52.0%,
  `llm.py` 0.0% (not omitted — see D-17), `oidc.py` 41.9%, `revocation_refresh.py` **97.9%**
  (was 18.2% pre-Phase-2 — A1's own number, closed), `revocation_state.py` 100.0%,
  `tct_claims.py` 91.7%, `telemetry.py` 70.6%.
- **Both gates verified against the real `ci.yml` invocation** (not the scratch rcfile used to
  take the measurement): `coverage run -m pytest tests/unit tests/scenarios -q`, then both
  `coverage report` calls — both exit 0.
- **Criterion 4, demonstrated:** hid three of the test files Phases 2/5/7 added
  (`test_agent_admin_routes.py`, `test_delegation_revocation.py`,
  `test_revocation_verify_or_discard.py`) and re-ran both gates. `agents/base` dropped to 43.1%
  and the gate failed with **exit code 2**; `src/*` stayed at 89.4% and passed — the two gates
  are independently meaningful, not one number wearing two labels.
- **Tests:** no new tests (this phase adds a gate over tests earlier phases wrote); suite stays
  at 530 passed, ruff clean.
- **Next:** Phase 9 — required-check traps and floor-comment drift.

### Phase 9 — Required-check traps and floor-comment drift — 2026-08-28 — PASS
- **Verifier tier:** Sonnet, re-read live branch protection rather than trusting the plan's
  snapshot — confirmed unchanged (`gh api .../branches/main/protection/required_status_checks`
  → the same five contexts before and after).
- **Part A — the matrix-derived required-check trap, widened beyond D-13's original scope.**
  `DECISIONS.md` D-13 documented the self-block trap for `docker-compose e2e` only. Widened to
  cover `Tests (Python 3.11)`/`Tests (Python 3.13)` (matrix-derived, `ci.yml`'s `test` job) —
  a *higher*-probability trap than the one already documented, since it has no `skipped`
  fallback and a Python-version bump is the single most predictable future edit to that file.
  Warning comments landed at `ci.yml`'s jobs block, the `test` job's matrix, and `docker.yml`'s
  `e2e` job name — all five required contexts now have a comment at their source naming the
  trap. No config change; branch protection stays a repo-admin decision per D-12's history.
- **Part B — floor-comment drift, mechanically caught.** New
  `tests/unit/test_sdk_floor_comment_matches_specifier.py` parses `pyproject.toml`'s declared
  `aitp-sdk>=X.Y.Z` specifier and the floor-rationale comment's highest bullet, failing when
  they disagree. Chose a test over a PR-template checklist (`DECISIONS.md` D-18) — and moot
  either way, since `.github/` has no PR template and one was not created for this.
- **Mutation result, run not reasoned:** bumping the specifier to `0.8.0` without adding a
  rationale bullet turns the new test red; reverted, and `uv sync --locked` confirms the lock
  is untouched by the revert.
- **Tests:** 532 passed (530 + 2), ruff clean.
- **Next:** Phase 10 — config surface: env table, `.env.example`, one dead setting.

### Phase 10 — Config surface: env table, `.env.example`, one dead setting — 2026-08-28 — PASS
- **Verifier tier:** Sonnet (doc edits mechanical; the dead-field decision stated in advance).
- **Part A — `PENDING.md` P13.2 corrected before closing.** Its original grep (`fail_mode`
  undocumented everywhere) is stale — `docs/aitp-integration.md:255,277-288` already covers it.
  Rewrote P13.2 to the real gap (`CP_AID` + three `REVOCATION_*` vars missing from the env
  table/`.env.example`) and struck it through as closed; P13.1 (route list) stays open for
  Phase 11.
- **Part B — files:** `docs/getting-started.md` (7 new env-table rows: `CP_AID`,
  `REVOCATION_FAIL_MODE`, `REVOCATION_MAX_STALENESS_SECS`, `REVOCATION_POLL_SECS`,
  `PUBLIC_HOST`/`PUBLIC_SCHEME`, `AITP_DIDWEB_INSECURE_HOSTS`), `.env.example` (same 7,
  commented, after `CP_TIMEOUT_MS` — noting `AITP_DIDWEB_INSECURE_HOSTS` is read via raw
  `os.environ`, so a `.env`-only value needs the process to also export it).
- **Part C — decision:** deleted the dead `didweb_insecure_hosts` `Settings` field rather than
  wiring it to the resolver — see `DECISIONS.md` D-19. `rg "didweb_insecure_hosts" src/`
  confirmed only the declaration existed before deletion.
- **NEW test** `tests/unit/test_config_env_table.py` — asserts every `Settings` field name
  appears in the env table; not noisy (all 19 current fields map cleanly to `NAME.upper()`).
- **Mutation result, run not reasoned:** adding an undocumented `Settings` field turns the new
  test red.
- **Tests:** 533 passed (532 + 1), ruff clean.
- **Next:** Phase 11 — `internal_docs/agents.md`: routes, layout, telemetry catalog.

### Phase 11 — `internal_docs/agents.md`: routes, layout, telemetry catalog — 2026-08-28 — PASS
- **Verifier tier:** Opus (the previous effort's Phase 8 showed a doc sweep's own replacement
  claims are what goes wrong — grepped the claims against source rather than reading the diff).
- **Files:** `internal_docs/agents.md` only. `PENDING.md` P13 struck through as CLOSED.
- **Route list:** confirmed 13 `build_admin_router` routes (not the 6 P13.1 named — the six
  plus the seven already-listed ones), added all six missing. Also found, not in the original
  audit: `AitpServer` itself mounts two more `/admin/*` routes directly
  (`/admin/rotate-keys`, `/admin/tct-cache-stats`) that the "From `AitpServer`" section never
  listed — added, with a note that they live outside `build_admin_router` on purpose (key
  material stays with the server, not the admin router).
- **Layout tree:** added `oidc.py` and `tct_claims.py` to `base/`, matching P13.1.
- **Telemetry catalog:** enumerated every event type actually emitted under `agents/`
  (`rg -o '"[a-z]+\.[a-z_.]+"' agents/`), filtered capability-name false positives
  (`analyze.data`, `research.deep`, `research.query`, `write.content` — payload strings, not
  telemetry types) from the remaining 23. Documented the 12 missing ones with their real field
  sets, checked per-site — including **`identity.key.rotated`** (`aitp_server.py:475-478`,
  `/admin/rotate-keys`), which the original audit's 11-item list missed entirely. Kept the
  `verify_failed`/`refresh_failed` split explicit (D-5/D-14's non-aliasing requirement) and did
  not assert a field set `delegation.redeemed`'s three emit sites don't all provide.
  `docs/observability.md` needed no edit — it already lists taxonomy families and points here as
  the catalog, confirmed accurate as written.
- **Tests:** 533 passed, unchanged (docs-only phase; `agents.md` is not loaded at runtime).
- **Next:** Phase 12 — prose accuracy: SDK call shapes, CP URL prefix, scenario summaries.

### Phase 12 — Prose accuracy: SDK call shapes, CP URL prefix, scenario summaries — 2026-08-28 — PASS
- **Verifier tier:** Opus (every item is "text asserting an API shape the code does not have" —
  checked by grep against source, not by reading the diff).
- **Files:** `docs/aitp-integration.md` (3 call-shape fixes: `verify_delegation` and
  `verify_delegation_multihop`'s missing deny-set arguments in the table and the ASCII sequence
  diagram; the `/api/registry/agents` URL prefix), `docs/architecture.md` (`aitp.verify_tct` →
  `agent.verify_tct`, matching the one correct form already in `aitp-integration.md`),
  `scenarios/intra-org/delegation-multihop/1.0.0/scenario.yaml` and
  `.../cp-trust-anchor-provisioning/1.0.0/scenario.yaml` (both summaries updated to state what
  they now enforce post-P9/D-8, without claiming more precision than the implementation has),
  `ASSUMPTIONS.md` (last entry flipped to CONFIRMED).
- **`docs/scenarios.md` checked, not edited** — its per-scenario table entries for both
  touched scenarios are brief and don't overclaim; no site to fix.
- **Both edited scenario files validate:** `cli validate` → `ok` for both.
- **Acceptance checks, all clean:** `rg "verify_delegation\("` / `"verify_delegation_multihop\("`
  in `docs/` show no bare 2-arg forms (the one apparent grep hit is the ASCII diagram's call
  split across two lines — the `deny_set)` continuation is on the next line); `rg
  "aitp\.verify_tct"` returns nothing; `rg "<CP_BASE_URL>/"` shows only the now `/api/`-prefixed
  form; `ASSUMPTIONS.md` has zero `UNCONFIRMED` entries.
- **Tests:** 533 passed, unchanged (docs/scenario-only phase), ruff clean repo-wide.

## Plan 2 close-out — all 12 phases PASS, 2026-08-28

`plans/audit-2026-08-28-cleanup.md` complete. Summary: 3 code-behavior fixes (Phases 3, 4, 6),
2 observability fixes (Phase 5), 1 CI gate change (Phase 8) plus 1 CI/process-doc phase
(Phase 9), 3 doc-correction phases (10, 11, 12), 1 test-integrity phase (2) plus one dedicated
negative-case-test phase (7), and 1 citation-rot cleanup (1). 6 new `DECISIONS.md` entries
(D-14 through D-19). `PENDING.md` P13 closed; `ASSUMPTIONS.md` has no open entries.
Every negative assertion added was demonstrated by mutation before being trusted (D-2).
Suite grew from 497 to 533 tests across the run; `agents/base` coverage moved from an
unmeasured 0% (outside the gate entirely) to a measured, gated 55.2%.

## Plan 3 — post-PR#57 docs/test drift sweep (2026-08-29) — repo map

(See `plans/docs-tests-audit-2026-08-29.md` for the full plan. This is the discovery
record from the three parallel survey agents, so `/implement` doesn't re-scan.)

- `scenarios/intra-org/tct-renewal/1.0.0/scenario.yaml`, `agents/base/agent_admin.py:354`,
  `src/aitp_playground/registry/models.py:58`, `src/aitp_playground/runner/engine.py:748`
  — cite `RFC-AITP-0005 §10` for TCT renewal; should be `RFC-AITP-0013` (confirmed against
  `docs/capabilities.md:29`, `docs/aitp-integration.md:403`, which already say 0013).
  **Do not blanket-grep for `rfc-aitp-0005`** — `tct-cache-perf/scenario.yaml:9,61`,
  `aitp_server.py:162,671,701`, `engine.py:780`, `models.py:62` correctly cite 0005 for the
  unrelated TCT verify-cache feature.
- `runner/engine.py:87-90` — `run()`'s first action upserts `{"status": "running"}`,
  unconditionally, before dispatch. `runner/engine.py:228-230` — the dispatch-level
  `run.failed`-emit guard (D-16 sibling site to the already-unit-tested
  `_finalize_failure` store guard at `:296-297`); has no deterministic unit coverage.
  `runner/engine.py:641-642` — `manifest.verify_failed` emit site (D-15), missing from
  `internal_docs/runner.md`'s event catalog. `runner/engine.py:667-674` — D-8 AID
  comparison (`declared_aid != ra.aid`), implemented but `docs/aitp-integration.md:125-128`
  still describes it as missing, citing closed `PENDING.md` P1.
- `tests/unit/test_engine_run.py:1245-1281` — existing `_finalize_failure` guard tests
  (store-level, already covers the sibling site, not the dispatch-emit guard).
- `aitp_server.py:440,669` — `/admin/rotate-keys` and `/admin/tct-cache-stats` are mounted
  directly by `AitpServer`, not `build_admin_router`. `agent_admin.py:335,786` —
  `/admin/held-tct` and `/admin/refresh-revocations` are in `build_admin_router` but
  missing from `docs/architecture.md:66-74`'s topology diagram, which also misgroups
  rotate-keys under the router list. `internal_docs/agents.md:215-219` already has the
  correct router-vs-direct-mount split — Phase 3 mirrors that precedent.
- `api/hosted.py` (6 routes) + `hosting/hosted.py` (business logic, no routes of its own)
  — the `/hosted-agents` cross-domain surface, undocumented beyond a passing env-var
  mention in `docs/architecture.md`, `docs/getting-started.md`, `README.md`.
- `docs/getting-started.md:~236` — states a single "floor: 88%", omitting the
  `agents/base/*` ≥54% gate (`ci.yml:101-102`, D-17). `docs/getting-started.md:~164` —
  describes `POST /runs/{id}/cancel` as "kill subprocesses, mark cancelled" (pre-D-16
  order); actual order in `api/runs.py:336,343` is cancelled-then-kill.
- `internal_docs/testing.md:80-81` — "no enforced threshold today" (false, contradicts
  `ci.yml:101-102`). `:184-185` — stale `PENDING.md` P7 citation (closed 2026-08-28,
  commit 92ebb33, e2e job now required). `:9-42` — test layout tree lists ~21 files;
  actual `tests/unit/` has 41.
- `internal_docs/runner.md` — event-type table missing `manifest.verify_failed` (the only
  gap found by re-running the `rg -o '"[a-z]+\.[a-z_.]+"'` extraction over `runner/`).
- `README.md:170-193`, `CLAUDE.md` Layout section — repo-map trees omit `scripts/`
  (`scripts/demo-e2e-run.sh`) and `federated/` (full cross-domain demo stack, own
  README); `agents/base/` bullet in both omits the revocation subsystem
  (`revocation_state.py`, `revocation_refresh.py` — first-class per P8/P9/P12, D-8–D-14).

### Phase 1 — Fix RFC-AITP-0005→0013 citation drift for TCT renewal — 2026-08-29 — PASS
- **Verifier tier:** Sonnet (explicit user instruction for this plan run: Sonnet only,
  no Opus/Fable — no phase in this plan is a one-way door).
- **Rounds:** 1.
- **Files:** `scenarios/intra-org/tct-renewal/1.0.0/scenario.yaml`,
  `agents/base/agent_admin.py:354`, `src/aitp_playground/registry/models.py:58`,
  `src/aitp_playground/runner/engine.py:748`.
- **Checked spec repo first:** RFC-AITP-0013 has no numbered subsections (just Abstract/
  Status/Sketch/References), so used the bare `RFC-AITP-0013` form, matching
  `docs/aitp-integration.md:403`'s existing correct citation.
- **Confirmed unrelated RFC-AITP-0005 citations left untouched:** `tct-cache-perf/
  scenario.yaml:9,61`, `aitp_server.py:162,671,701`, `models.py:62`, `engine.py:780`
  (the TCT verify-cache feature, a different subject).
- **Tests:** `uv run pytest tests/unit/test_capabilities.py
  tests/unit/test_sdk_blocked_features.py -q` → 16 passed. `cli validate
  scenarios/intra-org/tct-renewal` → `ok`.
- **Shipped now or accumulating:** Accumulating — verifier confirmed independently
  shippable, but per precedent (`plans/audit-2026-08-28-cleanup.md`'s 12 phases all
  bundled into one PR, #57) this plan's phases accumulate to one closing PR rather than
  8 separate small-doc PRs, to keep review noise down for a set of related, same-day
  drift fixes.
- **Next:** Phase 2 — deterministic unit test for the D-16 dispatch-level guard.

### Phase 2 — Deterministic unit test for the D-16 dispatch-level guard — 2026-08-29 — PASS
- **Verifier tier:** Sonnet (plan-wide instruction: Sonnet only, no one-way doors here).
- **Rounds:** 1.
- **Files:** `tests/unit/test_engine_run.py` only — no source change.
- **New test:** `test_run_failed_not_emitted_when_dispatch_races_a_cancel` — overrides
  `agent_http`'s `/admin/self-execute` to upsert the store to `"cancelled"` then raise,
  mirroring the real cancel/kill race, and asserts `run.failed` is absent from the
  emitted events and the store stays `"cancelled"`.
- **Mutation check (required per D-2's convention), run twice independently** (once by
  the executor, once by the verifier): removing the `!= "cancelled"` condition at
  `engine.py:228-230` turns the new test red with the exact expected assertion failure;
  reverting leaves `engine.py` byte-identical (`git diff` empty) and the test green again.
- **Non-vacuous, confirmed:** the captured event list under mutation was
  `['run.started', 'agent.spawning', 'agent.ready', 'trust.peers_resolved',
  'step.started', 'run.failed']` — proves the run genuinely executes through spawn/trust/
  dispatch before hitting the guard, not an early crash.
- **Tests:** `uv run pytest tests/unit/test_engine_run.py -q` → 52 passed. `ruff check` clean.
- **Shipped now or accumulating:** Accumulating (same reasoning as Phase 1 — one closing
  PR for the whole sweep, precedent from PR #57).
- **Next:** Phase 3 — `docs/architecture.md` route topology + `/hosted-agents` docs.

### Phase 3 — Fix architecture.md route topology, document /hosted-agents — 2026-08-29 — PASS
- **Verifier tier:** Sonnet. **Rounds:** 1.
- **Files:** `docs/architecture.md` only.
- **Fix:** split admin routes into "build_admin_router" (13 routes, now including
  `/admin/held-tct` and `/admin/refresh-revocations` which were missing from the diagram)
  vs. "mounted directly by `AitpServer`" (`/admin/rotate-keys`, `/admin/tct-cache-stats`),
  mirroring the precedent at `internal_docs/agents.md:202`. Added a new "Hosted agents"
  section documenting all 6 `/hosted-agents` routes (spawn/list/get/stop/
  resolve-and-handshake/invoke), previously undocumented beyond a passing env-var mention.
- **Verified against source independently** (both executor and verifier ran the same
  `grep -n '@router\.'` extraction over `agent_admin.py`, `aitp_server.py`, `api/hosted.py`
  and confirmed the doc now matches exactly).
- **Tests:** docs-only, N/A.
- **Shipped now or accumulating:** Accumulating (per Phase 1/2 precedent).
- **Next:** Phase 4 — getting-started.md/README.md hosted-agents mention, coverage gate,
  cancel-route ordering.

### Phase 4 — getting-started.md/README.md: hosted-agents, coverage gate, cancel order — 2026-08-29 — PASS
- **Verifier tier:** Sonnet. **Rounds:** 1.
- **Files:** `docs/getting-started.md`, `README.md`.
- **Fixes:** (1) added a substantive `/hosted-agents` pointer to both files, linking to
  Phase 3's `docs/architecture.md#hosted-agents-...` section — anchor slug verified to
  resolve correctly against GitHub's slug rules. (2) coverage-gate description now states
  both gates (`src/*` >=88%, `agents/base/*` >=54%, confirmed against `ci.yml:101-102`)
  instead of a single "floor: 88%". (3) cancel-route description corrected to
  "mark cancelled, then kill" (confirmed against `api/runs.py:303-344`'s actual order),
  with a one-clause note on why the order matters (D-16).
- **Tests:** `tests/unit/test_config_env_table.py -q` -> 1 passed (only doc-consistency
  test touching this file).
- **Shipped now or accumulating:** Accumulating (per precedent).
- **Next:** Phase 5 — docs/aitp-integration.md AID-check claim fix.

### Phase 5 — Fix aitp-integration.md's stale AID-check/PENDING-P1 claims — 2026-08-29 — PASS
- **Verifier tier:** Sonnet. **Rounds:** 1.
- **Files:** `docs/aitp-integration.md` only.
- **Fix:** replaced the "not compared yet, tracked as PENDING.md P1" framing with a
  positive description of the implemented check (`engine.py:667-674`'s
  `declared_aid != ra.aid` comparison, raising `PlaygroundError` before pinning), plus
  D-8's rationale.
- **Provenance claim independently verified twice** (executor + verifier): `ra.aid` is
  read from the spawned subprocess's `AITP_AGENT_READY` line over a `subprocess.PIPE`
  (`hosting/supervisor.py:73-89`), never off the network — confirmed by reading the
  actual supervisor code, not just engine.py's comment.
- **Tests:** docs-only, no doc-consistency test references this file.
- **Shipped now or accumulating:** Accumulating (per precedent).
- **Next:** Phase 6 — internal_docs/testing.md coverage gate, P7 citation, layout tree.

### Phase 6 — Fix testing.md: coverage gate, P7 citation, layout tree — 2026-08-29 — PASS
- **Verifier tier:** Sonnet. **Rounds:** 1.
- **Files:** `internal_docs/testing.md` only.
- **Fixes:** (1) "no enforced threshold today" -> both real gates stated (`src/*` >=88%,
  `agents/base/*` >=54%, `ci.yml:101-102`, D-17). (2) stale P7 "not required" claim ->
  closed 2026-08-28, e2e job now a required status check (PENDING.md P7, D-12/D-13).
  (3) test-layout tree fully regenerated from live `ls` (21 -> 46 files across unit/
  integration/scenarios) rather than hand-patched.
- **Bonus fix found during regeneration:** scenarios section previously listed three
  nonexistent placeholder files; corrected to the one real file, `test_scenario_packs.py`.
  Verified independently by both executor and verifier (`ls` confirms the three don't
  exist; `test_scenario_packs.py` is the only real file there).
- **Verified with a full independent file-set diff** (verifier rebuilt the live inventory
  from scratch and diffed against the doc's tree): 46 files, zero mismatch either
  direction.
- **Tests:** docs-only, no test references this doc.
- **Shipped now or accumulating:** Accumulating (per precedent).
- **Next:** Phase 7 — internal_docs/runner.md event catalog (manifest.verify_failed).

### Phase 7 — Add manifest.verify_failed to internal_docs/runner.md's event catalog — 2026-08-29 — PASS
- **Verifier tier:** Sonnet. **Rounds:** 1.
- **Files:** `internal_docs/runner.md` only.
- **Fix:** added `manifest.verify_failed` (fields `step_id`, `agent_id`, `cause`,
  `source_url`, confirmed exact against `engine.py:641-645`'s emit call) to the
  Trust/delegation/revocation/identity table.
- **Full re-check, not just the one known gap:** both executor and verifier independently
  ran `rg -o '"[a-z]+\.[a-z_.]+"' src/aitp_playground/runner/` (32 unique events) and
  cross-checked every one against the doc's tables — zero other gaps found.
  `run.cancelled`/`cp.webhook.delivered` correctly excluded (emitted from `api/runs.py`/
  `api/webhooks.py`, not `runner/engine.py`) and confirmed already documented elsewhere
  in the same file.
- **Tests:** docs-only, no test references this doc.
- **Shipped now or accumulating:** Accumulating (per precedent).
- **Next:** Phase 8 — README.md/CLAUDE.md repo maps + revocation subsystem mention.
