# PROGRESS — doppler-and-ci-cost

Plan: `plans/doppler-and-ci-cost.md`. Branch `chore/doppler-secrets`. Lock `.drive.lock` (gitignored).

## Repo map
- `src/aitp_playground/config.py` — pydantic `Settings`; every env var the service reads (`RAILWAY_PLATFORM_VARS` :12).
- `agents/base/llm.py` — agents read `LLM_PROVIDER`, `OPENAI_*`, `ANTHROPIC_*` from the process env.
- `Dockerfile:178-196` — `INSTALL_EXTRAS` build arg (set on Railway).
- `.env.example` — local env template; `.env` gitignored (`.gitignore:37`).
- `docs/getting-started.md:77-130` — Configure section + env table (checked by `tests/unit/test_config_env_table.py`).
- `docs/README.md` — published docs map; `internal_docs/README.md` — contributor docs map.
- `internal_docs/testing.md` — CI gating doc.
- `.github/workflows/ci.yml` — lint / test matrix / test-experimental / integration; required-name warning :14-20.
- `.github/workflows/docker.yml` — build-and-push (publish, do not touch) :51, changes :120, e2e :169.
- `DECISIONS.md` — last entry D-27; `DEFERRED.md`, `ASSUMPTIONS.md` exist.
- Reference: `/Users/Shared/agentcontextdistributionprotocol/acdp-playground/deploy/doppler-sync.sh` + `deploy/doppler-keys/playground.txt`.
- Railway ids: project `70754f0b-c930-4433-a268-7b408e8230e2`, env production `72787d79-e3d4-4ec8-81fa-ffdc476ad376`, service `f321666f-c5c3-45cf-9669-5f837bfcff5c`.

## Checkpoints
- 2026-11-08 plan written.

PR strategy: one PR (waves §5: one PR per unit; phases are small and share one purpose).
Risk: P1 complex (prod Doppler write), P2 complex (network script), P3+P4 simple (batched gate).
- 2026-11-07 P1 DONE — verdict PASS, round 1, Opus verifier (prod Doppler write = complex, gated solo). Gap round 1: PROGRESS checkpoint missing (bookkeeping, closed here).
  - prd: 7 keys set by pipe (CP_API_KEY CP_BASE_URL INSTALL_EXTRAS LLM_PROVIDER OPENAI_API_KEY OPENAI_MODEL PLAYGROUND_BASE_URL); PLAYGROUND_API_KEY was already equal. 8/8 EQUAL to Railway; managed hash `690a601691b49038` (sha256 of `sort` output incl. trailing newline, first 16 hex) on both sides.
  - dev: 16 keys from .env (count recorded), all equal. Hash `cc8d98fd3f690b11` uses the no-trailing-newline join; with the prd formula it is `30ce6d078f857c04`.
  - stg: 0 keys. dev_personal: branch config of dev, inherits its 16 keys, no overrides.
  - Railway: no write, no redeploy (latest deployment `4ba5e131-…` unchanged).
  - Files: plans/doppler-and-ci-cost.md, PROGRESS.md. Next: P2.
