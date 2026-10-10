# PROGRESS — docs-sync-2026-10

**Plan:** `plans/docs-sync-2026-10.md`
**Branch:** (create `docs/sync-2026-10` off `main` @ `035c75b` at implement time)
**Driver:** `/drive`, session `01XYchHeQgtcEtwj4BzfTyXw`. Single repo; no cross-repo writes.
**Previous feature's log:** `plans/archive/revocation-snapshot-verification-PROGRESS.md`.

## Phase status

| # | Phase | Verifier tier | Status |
|---|-------|---------------|--------|
| 1 | Versions, build, CI, Docker facts | Opus | DONE (2 rounds) |
| 2 | aitp-integration.md accuracy + de-dup | Opus | DONE (3 rounds) |
| 3 | control-plane.md vs live CP | Opus | DONE (2 rounds) |
| 4 | architecture/scenarios/observability/README | Opus | DONE (2 rounds) |
| 5 | federated README + link sweep | Opus | DONE (1 round) |

## Repo map

- `README.md`, `docs/*.md` — published docs (synced to website by `aitp-website/scripts/sync-content.sh`; `docs/README.md` is skipped by sync).
- `CLAUDE.md` — project instructions (sibling list, invariants, layout).
- `federated/` — cross-domain demo stack (README, compose, Caddyfiles, `gen-ca.sh`, certs git-ignored).
- `internal_docs/` — unpublished; published docs must not link into it (guard test).
- `pyproject.toml` — SDK floor + comment history (guard: `test_sdk_floor_comment_matches_specifier.py`).
- `Dockerfile`, `Dockerfile.cp-e2e`, `docker-compose*.yml`, `Dockerfile.dockerignore` — build facts.
- `.github/workflows/{ci,docker,bump-aitp,auto-merge}.yml` — CI facts.
- `src/aitp_playground/{api,registry,runner,hosting,trust,cp_client}` — behavior ground truth; `errors.py`, `config.py`, `main.py`, `api/hosted.py`, `trust/orchestrator.py`, `cp_client/client.py`.
- `agents/base/{aitp_server,agent_admin,llm,oidc}.py` — worker behavior ground truth.
- `scenarios/` — 21 scenario versions + templates.
- `tests/unit/test_{config_env_table,docs_do_not_link_internal_docs,sdk_floor_comment_matches_specifier}.py` — doc-drift guards.
- Siblings (read-only), under `/Users/Shared/agentIdenitytrustprotocol/`: `agentidentitytrustprotocol` (RFCs/spec, `docs/`), `aitp-rs` (`docs/sdk-python.md`, `bindings/aitp-py/{README.md,aitp.pyi}`, `CHANGELOG.md`), `aitp-control-plane` / `aitp-cp` (`docs/api.md`, `integration-playground.md`, `src/app/api`), `aitp-verifier-py`, `aitp-website`.

## Checkpoint log
- plan written; review round 1 REVISE applied; plan SOUND.
- PR strategy: one PR (docs-only, single repo, no natural seams). Risk tiers: P1–P5 all `simple`; gates batched (P1+P2), (P3+P4), (P5 + finalization).
- Branch `docs/sync-2026-10` created off main @ 035c75b.
- P1+P2 batch gate: round 1 GAPS (env_file claim, CI ordering wording, stale 'first build slow' lines; core-RFC mislabel, OIDC signer contradiction, revocation_refresh importer, rotation link); round 2 P1 PASS / P2 GAPS (OIDC signer file); fixed. Link checker (scratchpad) 0 unresolved; guard tests + 627 unit/scenario tests pass. Verifier: Opus. Files: README.md, docs/{README,aitp-integration,architecture,capabilities,docker,getting-started,scenarios}.md, pyproject.toml (comments), scenarios/intra-org/key-rotation scenario.yaml, CLAUDE.md (local only).
- P3+P4 batch gate: round 1 GAPS (POST/GET registry wording, leftover revocation prose, CP api.md section links, fault wording, env vars undocumented); round 2 (with P5 + cumulative) all PASS. Divergence: revocation narrative canonical in aitp-integration.md. Final gate: cumulative PASS (no cross-file contradictions, no leftover post-v0.1, link checker 0 unresolved, ruff clean, 627 unit+scenario tests pass). Nits fixed: fault wording, architecture link parenthesis. Files: docs/{control-plane,aitp-integration,architecture,scenarios,observability,getting-started,README}.md, README.md, federated/{README.md,gen-ca.sh,Caddyfile.org-b,docker-compose.federated.yml} (comments only).
- ASSUMPTIONS.md: nothing new logged (no one-way doors; nothing UNCONFIRMED for this plan). /reconcile: nothing to reconcile.
