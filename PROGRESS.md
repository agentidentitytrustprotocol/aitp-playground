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
| 4 | Run e2e pre-merge on SDK-bump PRs | playground | Sonnet | **DONE** (criterion 4 deferred — PENDING P7) |
| 6 | Verify the snapshot in the production revocation path | playground | Fable | **BLOCKED** — needs aitp-sdk 0.6.0 (PENDING P8) |
| 7 | Correct the docs | playground | Opus | **DONE** |

Phase 6 is blocked on Phase 5 (cross-repo). Phase 7's revocation half tracks Phase 6;
its manifest half tracks Phase 2B.

## Repo map

The plan carries the authoritative map ("## Repo map — aitp-playground"). Condensed:

**Revocation path** — `src/aitp_playground/cp_client/client.py:206` (`fetch_revocation_list`,
signature-blind) · `agents/base/agent_admin.py:604-665` (`/admin/refresh-revocations`, the
path the scenario exercises; `:657` `revoked_jtis.add` — monotonic union) · `:499` local
`/admin/revoke-tct` · `agents/base/aitp_server.py:334` deny-set enforcement.

**Manifest path (Phase 2B)** — `agents/base/agent_admin.py:415-424` (delegatee AID, unverified)
· `:85-93` (handshake, raw manifest to `build_hello`) · `src/aitp_playground/trust/resolver.py:33-51`.

**Config plumbing** — `src/aitp_playground/config.py:32` (`cp_base_url`; no `cp_aid`) →
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
- **Criterion 4 is NOT met and was not silently patched.** `docker-compose e2e` is absent from
  `main`'s required contexts (verified via the protection API), so it now *runs* pre-merge but
  blocks nothing. Closing it means changing repo-wide branch protection — the user's call, not
  this feature's diff. `PENDING.md` P7 has the reproduction and the skipped-vs-required trap.
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
