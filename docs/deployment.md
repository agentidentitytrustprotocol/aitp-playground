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
| Source | the prebuilt image `ghcr.io/agentidentitytrustprotocol/aitp-playground:latest`, published by `docker.yml` on push to `main` ([docker.md](docker.md)) |

Because the service deploys the published image, a merge to `main` is what
ships code. Changing a variable ships config, and that is what this page is
about.

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
