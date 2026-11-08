# Deployment and secrets

The hosted playground runs as one Railway service. Its variables come from
Doppler, which is the source of truth; Railway holds a copy that a script
keeps in step. This page covers where each value lives, how to change one,
and how to check the two sides agree. It names keys only; no value belongs
in this repo.

## Where it runs

| | |
| --- | --- |
| Railway project | `aitp-runner` |
| Environment | `production` |
| Service | `aitp-playground` |
| Source | the prebuilt image `ghcr.io/agentidentitytrustprotocol/aitp-playground:latest`, where `latest` means **the latest release** ([Releasing](#releasing), [docker.md](docker.md#image-tags)) |

A merge to `main` builds and tests an image (`sha-<short>`) but does not ship
it: `:latest` moves only when a release promotes that image, and the release
redeploys the service (DECISIONS D-33). So an unrelated redeploy, such as a
Doppler sync or a restart, always runs the latest release. Changing a
variable ships config; most of this page is about that.

## Releasing

A release is a `vX.Y.Z` tag whose image is the **same digest** as the
`sha-<short>` image `docker.yml` built on `main`. Nothing is rebuilt. (The
e2e job builds its own stack from the same commit and Dockerfile; the gate
requires it to have succeeded.) [`.github/workflows/release.yml`](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/.github/workflows/release.yml)
does it in one job:

1. **Gate** ([`scripts/release/preflight.sh`](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/scripts/release/preflight.sh)).
   It refuses unless the Railway credentials are present, the version is
   `X.Y.Z` and greater than every other tag, the commit is on `main`, GHCR
   holds `sha-<short>` with that commit as its revision label, and the
   `docker-compose e2e` and `Build & push playground image` checks on the
   commit concluded `success`. It waits up to 35 minutes for a main build
   still in progress. Last, it checks that the Railway token can read the
   service's deployments, so a revoked or mis-scoped token refuses here.
   Nothing is retagged before it passes.
2. **Promote.** `docker buildx imagetools create` tags that digest `X.Y.Z`,
   `X.Y` and `latest`, then checks that all four tags resolve to it.
3. **Tag and release.** It creates the tag (when dispatched) and a GitHub
   release with generated notes, marked Latest.
4. **Redeploy.** `railway redeploy --from-source` on `aitp-runner` /
   `production` / `aitp-playground`, with the CLI pinned to 5.70.0.
5. **Verify.** It waits up to 15 minutes for the new deployment and fails
   with its id if it ends `FAILED` or `CRASHED`. Then
   [`scripts/release/check-latest.sh`](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/scripts/release/check-latest.sh)
   must print `MATCH`. The job summary lists the digests and names which
   check passed.

Cut a release from `main` HEAD:

```bash
gh workflow run release.yml -f version=0.3.0
gh run watch "$(gh run list --workflow release.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
```

Pushing a tag yourself does the same for the tagged commit, which must be on
`main`: `git tag v0.3.0 <sha> && git push origin v0.3.0`. The workflow
creates the GitHub release if the tag has none. An older commit may have no
`sha-<short>` image: docker.yml's concurrency group replaces a queued main
build with a newer one. The gate then refuses; release a later commit.

A run that fails after the retag but before the deploy is verified leaves
`:latest` ahead of what runs, and `check-latest.sh` says `MISMATCH`.
Re-running the same run is safe: the gate accepts a tag that already points
at the commit, the retag is a no-op, and the redeploy and the check run
again. Releases are serialised (concurrency group `release`) and a running
one is never cancelled.

### Rollback

Every release keeps its `X.Y.Z` tag, so a rollback re-points `latest`:

```bash
gh workflow run release.yml -f rollback_to=0.2.0
```

It checks that `v0.2.0` exists on `main`, has a GitHub release, that GHCR's
`0.2.0` carries that commit, and that the Railway token works. Then it retags `latest` to that digest, redeploys, verifies, and then marks
the `v0.2.0` GitHub release Latest. The invariant stays true during a
rollback because it compares against the release GitHub marks Latest, not
the highest tag. The next forward release needs a version greater than the
highest tag.

### Is prod on the latest release?

```bash
scripts/release/check-latest.sh             # MATCH exits 0, MISMATCH exits 1
DRY_RUN=1 scripts/release/check-latest.sh   # what it would query; no network
```

It prints three digests: the running one (from the latest Railway
deployment's `meta.imageDigest`), `:latest`, and the image of the release
GitHub marks Latest. When Railway does not report the digest, it compares
`GET /healthz` → `revision` (the commit the image was built from) with the
`:latest` image's revision label. If neither can be established it reports
`MISMATCH`. Locally it uses your `railway` CLI login, or `RAILWAY_API_TOKEN`.

### Release credentials

| Where | Name | What |
| --- | --- | --- |
| Railway | project token `gha-aitp-playground-release` | Scoped to `aitp-runner` / `production`, the narrowest scope Railway offers |
| Doppler `aitp-playground` / `prd` | `RAILWAY_PLAYGROUND_TOKEN` | Source of truth for that token. **Not** on the sync allowlist, so it never reaches Railway's variables |
| GitHub Environment `production` | secret `RAILWAY_PLAYGROUND_TOKEN` | What `release.yml` uses. The Environment allows branch `main` and tags `v*`, with no required reviewers |
| GitHub repo variables | `RAILWAY_PROJECT_ID`, `RAILWAY_ENVIRONMENT_ID`, `RAILWAY_SERVICE_ID` | The deploy target (not secrets) |

The name differs from the org-level `RAILWAY_TOKEN` (aitp-docs' token) on
purpose: a repo secret of the same name would shadow it, and each token can
be revoked alone.
[`scripts/release/setup-release-credentials.sh`](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/scripts/release/setup-release-credentials.sh)
provisions them, and is idempotent (`--apply`; rotate with
`--apply --rotate`). It needs a Railway account token in
`RAILWAY_API_TOKEN`; see the script's header.

## Doppler layout

Doppler project `aitp-playground`:

| Config | Holds | Used by |
| --- | --- | --- |
| `prd` | Every variable the Railway service has | `deploy/doppler-sync.sh`, which copies it into Railway |
| `dev` | The local development set: a subset of `.env.example`'s keys (no `PLAYGROUND_API_KEY`), for native runs | `doppler run -- …` on a developer machine ([getting-started.md](getting-started.md#configure)) |
| `dev_personal` | Nothing of its own; it inherits `dev` | Per-person overrides, if you need one |
| `stg` | Empty | Reserved; there is no Railway staging environment |

## Managed keys

[`deploy/doppler-keys/playground.txt`](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/deploy/doppler-keys/playground.txt)
is the allowlist: the script manages these keys and no others.

| Key | Kind | Notes |
| --- | --- | --- |
| `PLAYGROUND_API_KEY` | secret | Bearer key(s) for the authenticated routes. Comma-separated to overlap a rotation. |
| `CP_API_KEY` | secret | Control Plane bearer |
| `OPENAI_API_KEY` | secret | Used when `LLM_PROVIDER=openai` |
| `ANTHROPIC_API_KEY` | secret | Used when `LLM_PROVIDER=anthropic`. **Dev/local only:** never set in prd (owner, 2026-11-07; DECISIONS D-30) |
| `CP_BASE_URL` | config | Control Plane base URL |
| `PLAYGROUND_BASE_URL` | config | Where agents POST telemetry |
| `LLM_PROVIDER` | config | `openai` or `anthropic` |
| `OPENAI_MODEL` | config | |
| `ANTHROPIC_MODEL` | config | Dev/local only, like `ANTHROPIC_API_KEY` |
| `AITP_PLAYGROUND_REQUIRE_API_KEY` | config | Backend lockdown flag ([SECURITY.md](../SECURITY.md#backend-lockdown-aitp_playground_require_api_key)). `false` in prd until the console sends the key (rollout step 1); `true` with no `PLAYGROUND_API_KEY` refuses to start |
| `RUN_RATE_LIMIT_PER_IP` | config | Launches per trusted client IP per window (prd 5) |
| `RUN_RATE_LIMIT_WINDOW_SECONDS` | config | That window (prd 600) |
| `MAX_CONCURRENT_RUNS` | config | Runs in progress service-wide (prd 3) |
| `LLM_QUOTA_PER_IP_PER_DAY` | config | Real LLM call units per trusted client IP per UTC day (prd 30) |
| `LLM_QUOTA_PER_IP_PER_MONTH` | config | Per UTC month (prd 300) |
| `LLM_QUOTA_GLOBAL_PER_DAY` | config | All callers per UTC day (prd 1000) |
| `LLM_QUOTA_GLOBAL_PER_MONTH` | config | All callers per UTC month (prd 10000) |
| `INSTALL_EXTRAS` | build arg | Read by the Dockerfile. Inert while the service deploys the prebuilt image; kept so a source build gets the same extras. |

Every other variable the service reads keeps its default
([getting-started.md](getting-started.md#configure) lists them all).
Railway's own `RAILWAY_*` variables are Railway's, and the script refuses
to manage them.

## Quota database

The LLM quota counters live in a dedicated Railway Postgres, service
`playground-db` in `aitp-runner` / `production` (DECISIONS D-31). It holds
one table, `playground_llm_usage`, which the playground creates on first
use. Nothing else uses this database.

`aitp-playground` reads it through `DATABASE_URL`, set on the service as the
Railway reference `${{playground-db.DATABASE_URL}}`. That variable is **not**
on the Doppler allowlist: the sync refuses references, and it reports the
key as "left alone". The reference resolves to the private host
`postgres-0lu4.railway.internal` (the service was created under Railway's
generated name and renamed afterwards; the private domain kept the old name).

With `LLM_QUOTA_STORE=auto` (the default), a service on Railway with no
`DATABASE_URL` uses a store that refuses every real LLM call, so the
playground runs on its mocks rather than spending without a limit. Check
the reference exists (by name) before a deploy that depends on it:

```bash
railway variables --service aitp-playground --kv | cut -d= -f1 | grep -x DATABASE_URL
```

The `aitp_playground_llm_quota_store_up` gauge on `/metrics` is `1` while
the store answers ([observability.md](observability.md)).

## Syncing Doppler into Railway

[`deploy/doppler-sync.sh`](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/deploy/doppler-sync.sh)
needs `git`, `jq`, `curl`, `shasum`, the Doppler CLI logged in, and a Railway token: the `railway`
CLI login, or `RAILWAY_API_TOKEN` set to an account or workspace token.

```bash
deploy/doppler-sync.sh playground                         # dry run: names and statuses only
deploy/doppler-sync.sh playground --apply                 # write changed keys, then redeploy
deploy/doppler-sync.sh playground --apply --no-redeploy   # write, leave the deployment alone
```

The dry run is the default and changes nothing. It prints one status per
managed key, then a hash of each side:

| Status | Meaning | What happens |
| --- | --- | --- |
| `unchanged` | Same value on both sides | Nothing |
| `changed` | Different value | `--apply` writes the Doppler value |
| `new` | In Doppler, not on Railway | `--apply` writes it |
| `absent` | Set on neither side | Skipped. The allowlist can name a key before it has a value. |
| `missing-in-doppler` | On Railway, not in Doppler | **Refused.** Add it to Doppler first; the script never picks Railway's value. |
| `railway-reference` | Railway's value is a `${{…}}` reference | **Refused.** A reference stays Railway-managed; take the key off the allowlist. |
| `doppler-value-is-a-reference` | Doppler's value contains `${{` | **Refused** |

Guarantees:

- It never prints a value and never puts one in a command line. Values go
  from Doppler through `jq` into the Railway API request body on stdin,
  and the Railway token goes to `curl` as a header read from a file
  descriptor.
- It never deletes a variable and never touches one that is not on the
  allowlist. Unlisted keys are named as "left alone".
- `--apply` refuses to run on a dirty working tree, so the allowlist it
  applies is the committed one.
- It writes each key with `skipDeploys`, then re-reads Railway and checks
  the hash. Only if the hash matches does it redeploy, once. If the hash
  does not match, it stops without redeploying.
- With nothing to change, `--apply` does nothing and does not redeploy.

## Common tasks

**Change a value.** Set it in Doppler `prd` with the value on stdin, so it
never lands in your shell history (`pbpaste` is macOS; on Linux use `xclip -o` or similar):

```bash
pbpaste | doppler secrets set OPENAI_MODEL -p aitp-playground -c prd >/dev/null
deploy/doppler-sync.sh playground            # expect: OPENAI_MODEL changed
deploy/doppler-sync.sh playground --apply
```

**Add a key.** Add it to Doppler `prd`, add its name to
`deploy/doppler-keys/playground.txt` (and to the table above), merge, then
run the sync with `--apply` from a clean checkout of `main`.

**Stop managing a key.** Remove its name from the allowlist. The variable
stays on Railway; delete it in Railway by hand if it should go.

## Verifying

The dry run is the check. It exits non-zero on any refusal, and its last
lines read:

```text
hash: railway <16 hex>, doppler <16 hex> (match)
dry run: nothing to change
```

The hash is the first 16 hex characters of the sha256 of the managed keys'
sorted `KEY=VALUE` lines, so two people can compare it without seeing a
value.

After a deploy, run `uv run python scripts/console_e2e.py lockdown`. It is
keyless and read-only. It checks that the playground returns 401 for keyless
`/runs`, `/scenarios`, `/metrics` and `POST /runs`, that the control plane
returns 401 for `/api/events/history` and `/api/metrics`, and that the
console refuses its admin route anonymously. `uv run python
scripts/console_e2e.py list` confirms the console proxy serves the live
scenario list; it launches nothing. `matrix --auth anon|signed-in`,
`ratelimit` and `quota` launch real runs through the console, so they cost
LLM money and rate-limit budget. Run them only as part of a planned E2E pass
(see `internal_docs/testing.md`, "Railway E2E through the console").
Evidence is JSONL under `$TMPDIR`, and the exit status is non-zero on any
failed assertion.
