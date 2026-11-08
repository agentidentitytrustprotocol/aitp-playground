# CI cost

What GitHub Actions costs this repo, what was cut and why, and how to
re-measure it. Decision record: `DECISIONS.md` D-29.

## Bottom line

The repo is public, so Actions minutes on GitHub-hosted runners are free:
**$0 before, $0 after.** GitHub's `/timing` endpoint reports 0 billable
milliseconds for every run. The cost that matters is runner time and queue
time: it delays PR feedback and holds runners other repos in the org share.

## Measurement (2026-10-08 → 2026-11-07)

241 workflow runs, every job fetched. "Billed minutes" here means each job's
wall time rounded up to the minute, which is how GitHub would bill it on a
private repo.

| | Billed min | Wall min |
| --- | --- | --- |
| Total | 1,235 | 850 |

By event: `pull_request` 674, `push` 536, `repository_dispatch` 16, `dynamic` 9 (Dependabot Updates).

By workflow and event:

| Workflow | Event | Billed min |
| --- | --- | --- |
| Docker | pull_request | 337 |
| CI | pull_request | 335 |
| Docker | push | 298 |
| CI | push | 218 |
| notify-website | push | 20 |
| bump-aitp | repository_dispatch | 16 |
| auto-merge | pull_request | 2 |

By job: `docker-compose e2e` 346, `Build & push playground image` 211,
`Tests (Python 3.11)` 185, `Tests (Python 3.13)` 162,
`Integration (agent subprocess e2e)` 93, `Detect SDK pin changes` 78
(a ~10 s job that bills a full minute each time), `Lint (ruff)` 78,
`Tests (experimental SDK)` 35.

Longest observed job run (minutes): e2e 7.2, Build & push 5.3, Tests 3.11
2.8, Tests experimental 2.5, Tests 3.13 2.4, Integration 1.4, Lint 0.2,
Detect SDK pin changes 0.2.

Branch protection on `main`: classic, `strict: true`, required contexts
`Lint (ruff)`, `Tests (Python 3.11)`, `Tests (Python 3.13)`,
`Integration (agent subprocess e2e)` and `docker-compose e2e`. There is no
merge queue and there are no rulesets.

## What changed

| Change | Saves (billed min / 30 d) | Why it is safe |
| --- | --- | --- |
| `ci.yml` no longer runs on push to `main`; a weekly `schedule` and `workflow_dispatch` replace it | ~178 (~218 push minutes cut, ~40 added back by ~4.3 weekly runs of ~10 min) | `strict: true` means the tree a PR run tested is the tree that merges, so a push run tested it again. Nothing reads CI status on `main`: there is no badge and no `workflow_run` consumer, and the Railway service deploys the published image. The weekly run keeps the two signals push runs gave beyond PRs: a direct admin push (`enforce_admins` is off) and drift of `aitp-verifier-py` HEAD, which the JCS drift guard clones. |
| `docker.yml` `changes` runs only on `pull_request` | ~30 | Its outputs are read only by the `pull_request` branch of `e2e`'s `if:`. `e2e` already starts with `always()`, so on push it still runs with `changes` skipped. |
| Concurrency cancels superseded runs on PRs only | (reliability, not minutes) | Before, `cancel-in-progress: true` also applied on `main`, so a newer merge could cancel an in-flight image publish or the main e2e (one Docker push run was cancelled in the window; the 8 cancelled jobs were all PR runs, which is intended). Now a group keeps one running and one pending run, and a newer run replaces the pending one. Runs on `main` serialize, a running publish is never killed, and the newest image is tagged last. In a burst of merges a middle commit's pending run can be replaced, so that commit gets no image of its own. |
| `timeout-minutes` on every CI and Docker job except `build-and-push` | (bounds a hang at minutes, not 6 h) | Lint 5, Tests 15, Integration 15, Tests (experimental SDK) 30 (a cold sdist compile), changes 5, e2e 30. Each is at least 4× the longest observed run. |

**Estimate: ~1,235 → ~1,030 billed min per 30 days (about −17%), $0 → $0.**

No job `name:` and no matrix value changed, so every required context still
reports (see the warning at the top of `ci.yml`, and D-13). The
`build-and-push` job body is unchanged. Its scheduling did change: on `main`
it is no longer cancelled by a newer merge.

A scheduled workflow on a public repo is disabled by GitHub after 60 days
with no repository activity. A merge re-enables nothing on its own; re-enable
it from the Actions tab if that happens.

## Considered and not done

- **Smaller runners.** Every job runs on `ubuntu-latest`, which is free on a
  public repo. A smaller label saves nothing measurable.
- **A docs-only PR skip.** Unit tests read the docs (`test_config_env_table.py`,
  `test_docs_do_not_link_internal_docs.py`), so a docs-only PR still needs the
  Tests matrix. Skipping only lint and integration would save under 10
  minutes a month, at the cost of the skipped-required-context hazard. The
  `dorny/paths-filter` `changes` job already path-filters the one expensive
  job, e2e.
- **Grouping concurrency by commit SHA.** Parallel publishes of different
  commits could race on the `latest` tag.

## If a merge queue is enabled

There is no merge queue today, so the queue path filter does not apply yet.
When one is enabled:

1. Add `merge_group:` to every workflow that produces a required context
   (`ci.yml` and `docker.yml`) **before** turning the queue on, or the queue
   waits forever for contexts that never report.
2. Path-filter the queue run against `merge_group.base_sha`, failing closed.
   Copy the `changes` job of seam-runtime's `integration.yml`: when the diff
   cannot be computed, it reports "changed" and runs everything.
3. Extend `e2e`'s `if:` with a `merge_group` branch that reads the same
   outputs, and drop the PR-only `if:` from `changes`.

## Re-measuring

```bash
R=agentidentitytrustprotocol/aitp-playground
mkdir -p jobs
gh run list -R "$R" --limit 1000 --created ">=$(date -u -v-30d +%F)" \
  --json databaseId,workflowName,event > runs30.json
jq -r '.[].databaseId' runs30.json | xargs -P 8 -I{} sh -c \
  "gh api 'repos/$R/actions/runs/{}/jobs?per_page=100&filter=all' > jobs/{}.json"
# Per job: wall seconds = completed_at - started_at; billed = ceil(seconds / 60).
jq -rn --slurpfile runs runs30.json '
  ($runs[0] | map({key: (.databaseId|tostring), value: .}) | from_entries) as $R
  | [inputs | .jobs[]? | select(.started_at and .completed_at)
     | ($R[.run_id|tostring]) as $r
     | {wf: $r.workflowName, ev: $r.event, job: .name,
        bill: ((((.completed_at|fromdateiso8601) - (.started_at|fromdateiso8601)) / 60) | ceil)}]
  | group_by(.wf + "|" + .ev)[] | "\(.[0].wf) \(.[0].ev) \(map(.bill) | add)"' jobs/*.json
```

(`date -v-30d` is BSD/macOS; on Linux use `date -u -d '30 days ago' +%F`.)
