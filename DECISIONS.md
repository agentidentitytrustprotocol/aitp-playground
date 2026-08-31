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
unconditional in the binding (no `#[cfg(feature)]`), and the floor is now `aitp-sdk>=0.7.0`,
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

**A fourth guard, found later, took a different shape (2026-08-28).**
`test_revocation_signing_convention.py`'s vendored-canonicalizer drift guard had the same false
"CI does not check this out" justification for its `pytest.skip` — false since `ci.yml` clones
`aitp-verifier-py` specifically to make it a real gate (`PENDING.md` P3). It was **not**
converted to an unconditional assertion like the three above: those three guard a property of
the *installed wheel*, which every developer has; this one guards the presence of a *second git
checkout*, which most developers legitimately do not have. Making it unconditional would turn a
fresh clone of this repo into a red suite for a reason that is not a defect. Gated on
`os.environ.get("CI")` instead — skip on a developer machine, hard-fail in CI — which keeps
D-11's property (no silent green where the gate is supposed to run) without that cost.
Demonstrated all three paths live: `CI=1` + sibling absent → red; `CI` unset + absent → skip;
sibling present → compares normally. See `plans/audit-2026-08-28-cleanup.md` Phase 2.

## D-12 — `docker-compose e2e` stays advisory, not required
**2026-08-26 · user decision · REVERSED 2026-08-28, see below**

Phase 4 widened the job to run pre-merge on any PR touching `uv.lock`. It is deliberately
**not** added to `main`'s required status checks.

Accepted trade: a green bump PR can auto-merge while e2e is still running or red. Detection
exists; the gate does not. Weighed against changing repo-wide configuration that affects every
contributor's PR, plus the skipped-vs-required trap — a conditional job wired wrong leaves
unrelated PRs waiting on a check that never reports.

Reversible in one settings change; `PENDING.md` P7 keeps the close-out steps, including the
requirement to demonstrate **both** directions (a pin PR blocks, an unrelated PR merges).

**Reversed 2026-08-28.** The skipped-vs-required trap this decision was hedging against does
not apply to this job: `docker.yml`'s `pull_request` trigger has no path/branch filter that
would exclude a normal PR, so `e2e` always schedules and reports either `skipped` or a real
conclusion — never "no status at all." Demonstrated both directions on live PRs, not reasoned
about: `docker-compose e2e` skipped cleanly on a `.md`-only PR (#53, `mergeStateStatus: CLEAN`
throughout) and blocked a `uv.lock`-touching PR (#54, `mergeStateStatus: BLOCKED` while the job
ran ~4-5 min, `CLEAN` only once it passed). Added to `main`'s required status checks via the
`required_status_checks` sub-resource `PATCH` (not a full `PUT`, which would have reset
`enforce_admins`/`allow_force_pushes`/etc. to their defaults) — verified byte-identical on
every other protection field before and after. `PENDING.md` P7 closed. See D-13 for the trap
this reversal introduces in its place.

## D-13 — Renaming the `docker-compose e2e` job (or its workflow file) will self-block
**2026-08-28 · recorded, not decided**

D-12's reversal makes `main`'s required status checks match a job by its display **name**
(`docker.yml`'s `e2e` job has `name: docker-compose e2e`), not by job id or workflow path.
GitHub has no other handle for a required check.

If a future change renames that job, renames `docker.yml`, or restructures the workflow so a
job with that exact name no longer reports, the PR making the change blocks **itself**: the
required context never reports, and `mergeStateStatus` sits `BLOCKED`/`Expected` rather than
failing loudly. `enforce_admins: false` is the escape hatch (`gh pr merge --admin`), but that
is a repo admin working around branch protection, not a fix.

Not closing this by pinning the job to a workflow path instead — GitHub's required-checks
model doesn't support that — so it is recorded as a standing trap rather than solved. Anyone
touching `docker.yml`'s `e2e` job name should land the protection-settings update in the same
PR as the rename.

**Widened 2026-08-28 — the trap applies to three more required contexts, and one of them is a
*higher*-probability trigger than the one this entry originally named.** Live branch protection
(`gh api .../branches/main/protection/required_status_checks`) requires all five of:

```
Lint (ruff)                         — ci.yml's `lint` job, name: "Lint (ruff)"
Tests (Python 3.11)                 — ci.yml's `test` job, matrix-derived
Tests (Python 3.13)                 — ci.yml's `test` job, matrix-derived
Integration (agent subprocess e2e)  — ci.yml's `integration` job
docker-compose e2e                  — docker.yml's `e2e` job
```

`Tests (Python 3.11)` / `Tests (Python 3.13)` are **matrix-derived** — `name: Tests (Python
${{ matrix.python-version }})` combined with `python-version: ["3.11", "3.13"]`. A routine
Python-version bump (adding 3.14, or moving the floor off 3.11 once it's out of support) changes
the reported context names. Unlike `docker-compose e2e`, this job is unconditional — there is no
`skipped` conclusion to fall back on, so the required context simply never reports again.
Version bumps are also the single most predictable future edit to this file, which makes this a
*higher*-probability trigger than the `docker-compose e2e` rename this entry originally covered.
`Lint (ruff)` and the two per-job base names carry the same trap in principle but are far less
likely to be touched incidentally.

Warning comments landed at `ci.yml`'s jobs block, the `test` job's matrix, and `docker.yml`'s
`e2e` job name (Phase 9 close-out). No config change — branch protection stays repo admin's
call, per this decision's own history. See `plans/audit-2026-08-28-cleanup.md` Phase 9.

## D-14 — `revocation.verify_failed` is exempt from `quiet`; `refresh_failed` and
`list_fetched` are not
**2026-08-28 · recorded**

The background poll calls `refresh_revocations_now(quiet=True)`
(`aitp_server.py:223`), and `revocation_refresh.py`'s `_discard` gated its own emit on
`if not quiet:`. So a discarded snapshot in steady state — forged, wrong-issuer, expired,
or unverifiable for lack of a pinned issuer or SDK surface — emitted **nothing**. The only
steady-state signal was `revocation.poll`'s `healthy` flag, which is `False` identically for
a CP that is down and for an attacker-signed snapshot. Two docstrings
(`aitp_server.py`'s poll loop, `revocation_refresh.py`'s module docstring) already claimed
`verify_failed` was the one thing `quiet` must not suppress; the code just didn't do it.

Two candidates considered. **Chosen: `_discard` emits unconditionally; `quiet` still governs
`revocation.list_fetched` and `revocation.refresh_failed`.** One line
(`revocation_refresh.py`'s `_discard`, dropping the `if not quiet:` guard). The volume
argument that motivated `quiet` in the first place is weak for discard causes specifically: a
discard means the CP is reachable and answering with something that does not verify, which is
not the routine state `quiet` was introduced for (an ordinary CP outage, which still fires
`refresh_failed` once per poll tick and stays suppressed).

**Rejected: also suppress the two static-configuration causes** (`no_expected_issuer`,
`sdk_cannot_verify`) **under `quiet`.** These are true on every tick forever once true at all,
so under the chosen candidate they emit once per `revocation_poll_secs` (default 60)
indefinitely on a misconfigured deployment. Rejected anyway: that deployment — `CP_BASE_URL`
set with `CP_AID` empty, or an old SDK — already has every capability call refused under the
default `fail_closed`, so being loud about it is correct, not noisy. It also keeps
`revocation_refresh.py` a single decision site with one rule rather than two.

**Rejected outright: have the poll loop emit `verify_failed` itself from the returned
`discarded` cause**, rather than fixing `_discard`. `refresh_revocations_now` collapses the
result to a bool, so this would need a wider return type *and* recreate a second decision site
in a module whose own docstring already forbids exactly that duplication.

Demonstrated by mutation: restoring `if not quiet:` inside `_discard` turns
`tests/unit/test_revocation_verify_or_discard.py` red. See
`plans/audit-2026-08-28-cleanup.md` Phase 3.

## D-15 — The manifest verify-failure classifier is duplicated on purpose, not shared
**2026-08-28 · recorded**

Two of three manifest ingest sites (`agent_admin.py`'s handshake and delegatee fetches) had a
`signature_invalid | expired | malformed | unknown` cause taxonomy and `manifest.verify_failed`
telemetry; the third (`runner/engine.py`'s CP trust-anchor provisioning — the site that pins a
key into the control plane's trust store) had neither: a bare `aitp.verify_manifest_json(mr.text)`
call whose raw SDK error propagated with no cause and no event.

The obvious fix — a shared classifier function both sides import — does not work. Verified: an
agent subprocess's `PYTHONPATH` carries `agents/base`+`agents` but never `src`
(`hosting/adapters/base.py`'s env builder), and this service gets `agents/base` on `sys.path`
only in Docker and under pytest, **not** under the documented local dev command (`uv run
uvicorn aitp_playground.main:app`). A module placed in either package and imported by the other
passes CI and Docker and breaks local `uvicorn` at start-up, and pytest's own `pythonpath`
setting (both dirs) means the test suite cannot catch it.

**Chosen: mirror the classifier in both places, pinned in sync by a parity test.**
`agent_admin.classify_manifest_verify_failure` (extracted, no behaviour change) and
`runner.engine._classify_manifest_verify_failure` (new) must return identical results for the
same exception; `tests/unit/test_manifest_verification.py`'s parity test asserts that and is
demonstrated non-vacuous by mutation — swapping one copy's branch order turns it red.

**Rejected:** a shared module in either package (breaks local dev, above); pushing the fallback
logic into the SDK so `.code` is always present and no playground-side classification exists at
all (correct long-term, cross-repo, out of scope here — revisit-when the SDK adds it).

See `plans/audit-2026-08-28-cleanup.md` Phase 4.

## D-16 — A cancelled run's record stays cancelled; mark before kill, not after
**2026-08-28 · found live, not just reasoned**

`POST /runs/{id}/cancel` upserted `status="cancelled"` and returned, but killing the agent
subprocesses turns the background run's next inter-agent HTTP call into an exception, and
`_finalize_failure` (`runner/engine.py`) unconditionally upserted `status="failed"` — clobbering
the cancellation one HTTP round-trip later. `run.cancelled` was also emitted by nothing in the
repo, so the documented `aitp_playground_runs_total{status=cancelled}` metric label was
unreachable.

**Chosen: guard both the store write and the `run.failed` emit in `_finalize_failure`/the
mid-run exception handler, AND reorder the cancel route to mark-then-kill rather than
kill-then-mark.** Guarding only the store (not the emit) was rejected outright:
`observability/metrics.py` treats `run.complete`/`run.failed`/`run.cancelled` identically —
one `runs_active` decrement and one `runs_total` label increment each — so an unguarded
`run.failed` emit for an already-cancelled run double-counts one run into two labels even with
the store itself correctly preserved.

**The ordering fix was not in the original plan and was found by running the real
`AITP_E2E=1` integration test, not by reasoning about the code.** The first implementation
guarded the store and the emit but left the cancel route's original order (kill subprocesses,
*then* upsert `cancelled`). Live, this raced: `supervisor.kill_run` runs on the request thread
while the background run's task lives on its own asyncio loop, and killing a subprocess can
fail the in-flight inter-agent call fast enough that the background task's guard check reads
the store *before* the cancel route's own upsert has landed — so it saw a non-cancelled status
and correctly (by its own logic) proceeded to emit `run.failed`, which then landed in the event
log ahead of `run.cancelled`. Reordering the route (upsert + emit `run.cancelled` first, kill
subprocesses last) closes the race at its source: by the time anything can observe a failure
from the kill, the store already says `cancelled`. Re-ran the live integration test 5/5 clean
after the reorder, where it failed reliably before.

**Deferred: having the engine itself own cancellation** (a cancel flag `_dispatch_step` checks,
so the engine is the one place that can emit the terminal event, removing the race by
construction rather than closing it with ordering). Correct long-term — revisit if this area
gets touched again — but it means threading a cancellation token through every step type, which
is a runner refactor, not a fix to the two failure sites this defect lives in.

Also pinned at the unit tier, not only behind `AITP_E2E`: a `_finalize_failure` test that
pre-seeds the store as `cancelled` and asserts the record survives. The live integration test
alone would not have been a reliable mutation gate — a first mutation attempt (removing
`_finalize_failure`'s guard alone, post-reorder) did NOT turn the E2E test red, because the
reorder made the specific race window it used to hit close enough that it no longer reproduces
reliably within that test's timing; the unit-tier test, driving `_finalize_failure` directly
with no subprocess or timing involved, does turn red on the same mutation, deterministically.

See `plans/audit-2026-08-28-cleanup.md` Phase 6.

## D-17 — `agents/base` gets its own coverage gate, not folded into one aggregate
**2026-08-28 · measured, not estimated**

`agents/base/` holds every revocation, manifest, TCT, and delegation security decision in the
repo and was entirely outside `pyproject.toml`'s `source_pkgs = ["aitp_playground"]` — D-11
noted this in passing (*"these modules exercise `agents/base`, outside `source_pkgs`, so
skipping them costs zero coverage and clears `--fail-under=88`"*) without closing it.

Measured on the tree after Phase 7's new tests, 530 tests passing: `src/aitp_playground` 89.4%,
`agents/base` 55.2%. `revocation_refresh.py` alone moved from 18.2% (measured before Phase 2)
to 97.9% — A1's own numbers, closed.

**Chosen: two separate `coverage report --include` gates over one `coverage run` data file** —
`src/*` stays at `--fail-under=88`, `agents/base/*` gets its own `--fail-under=54` (the measured
55.2%, rounded down and given one point of headroom so an unrelated PR adding one uncovered
defensive line does not block on it). Verified both gates pass on the real `ci.yml` invocation,
and that dropping the `agents/base` gate is detectable: hiding three of Phase 2/5/7's test files
drops `agents/base` to 43.1% (gate fails, exit code 2) while `src/*` stays at 89.4% (gate still
passes) — proving the two gates are actually independent, not one number in two clothes.

**Rejected: fold `agents/base` into the same measured source and re-baseline `--fail-under` to
the combined ~78% aggregate.** This *lowers* the effective floor on `src/` by ten points — a
regression that drops `src/` from 89% to 79% would still pass, hidden behind `agents/base`'s
much lower number. A single aggregate gate that gets weaker the moment a second, worse-covered
package joins it is not a gate anyone should trust.

**Rejected: add `agents/base` and keep `--fail-under=88` on the aggregate.** The combined number
is 78% today — a job that goes red on day one for coverage that was never actually claimed gets
disabled by the next person who touches CI, which defeats the point of adding it at all.

**Not omitted:** `agents/base/llm.py` (0.0%, the LLM provider selector, largely untestable
without live keys) drags the `agents/base` number down materially. Left in the measured source
rather than `[tool.coverage.report] omit`-ed — the ratchet threshold already accounts for it,
and omitting anything needs its own justification recorded, which this repo's convention
(nothing security-relevant hidden from the gate) argues against doing casually.

See `plans/audit-2026-08-28-cleanup.md` Phase 8.

## D-18 — Floor-comment drift is caught by a test, not a checklist
**2026-08-28 · recorded**

`.github/workflows/bump-aitp.yml` delegates to `aitp-ci`'s shared `bump-consume.yml` with
`ecosystem: uv`, which runs `uv lock --upgrade-package` — confirmed empirically, `f18447f`
("bump aitp to 0.8.0") touched only `uv.lock`. `pyproject.toml`'s floor specifier and its
rationale comment move only by hand, and Phase 1 of this cleanup (the comment stopping at
0.6.0 while the specifier read `>=0.7.0`) is the second time this exact defect has landed —
the original effort's own Phase 1 fixed the same class once already, at 0.3.0-vs-0.4.0.

**Chosen: a unit test** (`tests/unit/test_sdk_floor_comment_matches_specifier.py`) that parses
the declared specifier and the comment's highest rationale bullet and fails when they disagree.
Runs on both Python versions for free, in the coverage job, and a failing assertion names
itself better than a grep step buried in a workflow.

**Rejected: a checklist line in a bump PR template.** Relies on a human reading a template on a
PR that `auto-merge.yml` is designed to merge unattended — the exact failure mode this guards
against is nobody looking. Moot here anyway: `.github/` has no PR template, and one was not
created for this.

**Rejected, recorded as revisit-when: teach `bump-consume.yml` to edit `pyproject.toml`
directly.** Out of scope — it lives in the shared `aitp-ci` repo across every consumer, and the
rationale comment is prose no generic bump workflow can write. Revisit if `aitp-ci` ever grows
a hook for repo-specific post-bump edits.

See `plans/audit-2026-08-28-cleanup.md` Phase 9.

## D-19 — The did:web-insecure-hosts allowlist stays out of `Settings`
**2026-08-28 · recorded**

`config.py` declared `didweb_insecure_hosts: str = ""` (binding `DIDWEB_INSECURE_HOSTS`), and
nothing read it — `rg "didweb_insecure_hosts"` matched only the declaration and its own
comment. The live allowlist is read directly from the environment at
`trust/resolver.py:22`, as `AITP_DIDWEB_INSECURE_HOSTS` — a **different** variable name. The
`config.py` comment claimed the resolver reads that var "too", implying the `Settings` field
was also consulted; it was not.

**Chosen: delete the unread field.** `resolve_did_web` (`trust/resolver.py`) is a module-level
coroutine with no `Settings` access, called from both `api/hosted.py` and
`trust/orchestrator.py`. Threading `Settings` into it for a test-only escape hatch used only
by the Level 1 federated stack is more machinery than the feature deserves. The env var name
is already load-bearing in four places outside this repo's Python
(`federated/docker-compose.federated.yml`, `federated/README.md`, `docs/aitp-integration.md`,
`tests/unit/test_federation.py`) — deleting the unread field and leaving the resolver's direct
read is the change that makes the code agree with every one of those.

**Rejected: keep the field with `validation_alias="AITP_DIDWEB_INSECURE_HOSTS"` and thread it
into the resolver.** Two call sites, a signature change, and a fixture rework across
`tests/unit/test_trust_resolver.py` and `test_federation.py`, all to move a test-only value
from one lookup style to another. Recorded as the tidier long-term shape if the resolver ever
needs other configuration — revisit then, not now.

See `plans/audit-2026-08-28-cleanup.md` Phase 10.

## D-20 — The floor moved for a wire reason, not for currency
**2026-08-31 · recorded**

`aitp-sdk` 0.11.0 changed the wire encoding of the pinned-key identity proof: the envelope
`timestamp` inside `proof_input` moved from an 8-byte big-endian signed integer to its base-10
ASCII-decimal string (RFC-AITP-0002 §3.1 erratum, spec issue #17). A proof minted by <=0.10.x
does not verify under >=0.11.0 and vice versa, and `aitp-rs` implements no dual-accept. This
repo's default `identity_type` is `"pinned_key"` (`bootstrap.py:34`,
`registry/models.py:242`), so the handshake path is affected by default, not as an edge case —
and `aitp-verifier-py` has always used the ASCII-decimal form, so pre-0.11.0 wheels never
actually interoperated with it. A mixed-version pair fails as `handshake.failed` with a
signature error, which reads like a bad key rather than a version mismatch.

**Chosen: bump the floor to `aitp-sdk>=0.11.0` and add a rationale bullet, at the same rigor as
the existing 0.3.0/0.5.0 wire-break bullets** (`pyproject.toml:17-52`). The already-open,
already-green dependency-bot PR (#62, `chore(deps): bump aitp to 0.11.0`) merged, but turned
out to be a no-op for the actual version resolution: its `uv lock --upgrade-package` run fired
22 seconds after 0.11.0 was uploaded to PyPI (2026-08-30T14:53:06Z upload vs. 14:53:28Z bot
run) and raced index propagation, so it re-resolved to 0.10.0 — its merged diff touches only
resolution-marker reordering (3 insertions/3 deletions in unrelated marker/numpy lines) and
contains no `aitp-sdk` version change at all. The real `uv.lock` bump to 0.11.0 comes from
this phase's own `uv lock` run, after raising the `pyproject.toml` specifier below; this
decision is the hand-written half D-18 says the bot cannot do regardless — the specifier and
the prose explaining why it moved. PR #58 (an earlier, stale `chore(deps): bump aitp to
0.10.0`) was closed as superseded, since the 0.10.0 lock value it wanted actually landed via
#60 — well before #62 merged as the no-op described above.

**Rejected: bump the floor silently, without a new bullet.** This is the exact defect D-18's
test guards against and the exact class of defect the 0.3.0/0.5.0 bullets already exist to
prevent — a silent, self-consistent wire divergence that reads as a network fault instead of a
resolver error. `tests/unit/test_sdk_floor_comment_matches_specifier.py` would also fail.

See `plans/aitp-rs-breaking-changes-adoption.md` Phase 1.
