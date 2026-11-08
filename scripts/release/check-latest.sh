#!/usr/bin/env bash
#
# Read-only check of the release invariant (DECISIONS.md D-33):
#
#     running on Railway  ==  GHCR :latest  ==  the release GitHub marks Latest
#
# Prints the three digests and MATCH or MISMATCH. Exit 0 on MATCH, 1 on MISMATCH (or when the
# running image cannot be established, or a deployment being waited on failed), 2 on a usage or
# tool error. Used by .github/workflows/release.yml after the redeploy and by hand at any time.
#
#   scripts/release/check-latest.sh                        # compare now
#   scripts/release/check-latest.sh --release-tag v0.3.0   # compare against this tag, not GitHub's Latest
#   scripts/release/check-latest.sh --wait-new-deploy <id> [--timeout 900]
#                                   # first wait for a deployment other than <id> to finish
#   scripts/release/check-latest.sh --print-deployment-id  # print the latest deployment id and exit
#   DRY_RUN=1 scripts/release/check-latest.sh              # print what would be queried; no network
#
# How the running image is established, in order:
#   (a) digest: the latest Railway deployment's meta.imageDigest equals :latest's index digest or
#       its linux/amd64 manifest digest (Railway may record either);
#   (b) revision: if the digest is unreadable, GET /healthz -> revision equals :latest's
#       org.opencontainers.image.revision label (retagging keeps the digest, so it is the release
#       commit; after a rollback, the rollback target's commit).
# Neither -> exit 1. The output names which check passed.
#
# Railway auth, first match wins, and the token never goes in argv (curl reads the header from a
# process substitution):
#   RAILWAY_TOKEN       a project token (CI) -> header Project-Access-Token
#   RAILWAY_API_TOKEN   an account/workspace token -> Authorization: Bearer
#   ~/.railway/config.json   the railway CLI login -> Authorization: Bearer
#
# Overridable (non-secret): IMAGE, RAILWAY_PROJECT_ID, RAILWAY_ENVIRONMENT_ID, RAILWAY_SERVICE_ID,
# HEALTHZ_URL, GITHUB_REPOSITORY, POLL_SECONDS. Needs curl, jq, docker buildx, and gh (only when
# --release-tag is not given; without gh it falls back to the highest v* git tag).

# GraphQL and docker --format strings are single-quoted on purpose.
# shellcheck disable=SC2016
set -euo pipefail

IMAGE="${IMAGE:-ghcr.io/agentidentitytrustprotocol/aitp-playground}"
RAILWAY_PROJECT_ID="${RAILWAY_PROJECT_ID:-70754f0b-c930-4433-a268-7b408e8230e2}"         # aitp-runner
RAILWAY_ENVIRONMENT_ID="${RAILWAY_ENVIRONMENT_ID:-72787d79-e3d4-4ec8-81fa-ffdc476ad376}" # production
RAILWAY_SERVICE_ID="${RAILWAY_SERVICE_ID:-f321666f-c5c3-45cf-9669-5f837bfcff5c}"         # aitp-playground
HEALTHZ_URL="${HEALTHZ_URL:-https://aitp-playground-production.up.railway.app/healthz}"
REPO="${GITHUB_REPOSITORY:-agentidentitytrustprotocol/aitp-playground}"
POLL_SECONDS="${POLL_SECONDS:-15}"
RAILWAY_GQL=https://backboard.railway.com/graphql/v2

release_tag=""
wait_prev=""
wait_mode=0
timeout=900
print_id=0
while [ $# -gt 0 ]; do
  case "$1" in
    --release-tag) release_tag="${2:?--release-tag needs a value}"; shift 2 ;;
    --wait-new-deploy) wait_prev="${2:?--wait-new-deploy needs a deployment id}"; wait_mode=1; shift 2 ;;
    --timeout) timeout="${2:?--timeout needs seconds}"; shift 2 ;;
    --print-deployment-id) print_id=1; shift ;;
    -h|--help) sed -n '2,36p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*" >&2; }
die() { say "error: $*"; exit 2; }

# ── auth selection (names only) ──────────────────────────────────────────────
auth_header=""
auth_source=""
if [ -n "${RAILWAY_TOKEN:-}" ]; then
  auth_header="Project-Access-Token"; auth_source="RAILWAY_TOKEN (project token)"
elif [ -n "${RAILWAY_API_TOKEN:-}" ]; then
  auth_header="Authorization"; auth_source="RAILWAY_API_TOKEN"
else
  auth_header="Authorization"; auth_source="railway CLI login (~/.railway/config.json)"
fi

if [ "${DRY_RUN:-0}" = 1 ]; then
  cat <<EOF
check-latest dry run: nothing is queried.
  running   Railway GraphQL $RAILWAY_GQL  deployments(project $RAILWAY_PROJECT_ID, env $RAILWAY_ENVIRONMENT_ID, service $RAILWAY_SERVICE_ID, first 1) -> meta.imageDigest
            auth: $auth_header header from $auth_source
            fallback: GET $HEALTHZ_URL -> revision
  latest    docker buildx imagetools inspect $IMAGE:latest
  release   ${release_tag:-gh api repos/$REPO/releases/latest -> tag_name (fallback: highest v* git tag)} -> $IMAGE:<X.Y.Z>
  options   wait_new_deploy=${wait_prev:-none} timeout=${timeout}s print_deployment_id=$print_id
EOF
  exit 0
fi

for c in curl jq docker; do command -v "$c" >/dev/null || die "$c is not on PATH"; done

railway_token="${RAILWAY_TOKEN:-${RAILWAY_API_TOKEN:-}}"
if [ -z "$railway_token" ]; then
  railway_token="$(jq -r '.user.accessToken // .user.token // empty' ~/.railway/config.json 2>/dev/null || true)"
fi

# Prints the latest deployment node as JSON ({id,status,meta}) or nothing when unreadable.
latest_deployment() {
  [ -n "$railway_token" ] || return 0
  local body out
  body="$(jq -nc --arg p "$RAILWAY_PROJECT_ID" --arg e "$RAILWAY_ENVIRONMENT_ID" --arg s "$RAILWAY_SERVICE_ID" '{
    query: "query($p:String!,$e:String!,$s:String!){deployments(input:{projectId:$p,environmentId:$e,serviceId:$s},first:1){edges{node{id status createdAt meta}}}}",
    variables: {p: $p, e: $e, s: $s}}')"
  if [ "$auth_header" = Project-Access-Token ]; then
    out="$(printf '%s' "$body" | curl -fsS --max-time 30 "$RAILWAY_GQL" \
      -H @<(printf 'Project-Access-Token: %s\n' "$railway_token") \
      -H 'content-type: application/json' --data-binary @- 2>/dev/null)" || return 0
  else
    out="$(printf '%s' "$body" | curl -fsS --max-time 30 "$RAILWAY_GQL" \
      -H @<(printf 'Authorization: Bearer %s\n' "$railway_token") \
      -H 'content-type: application/json' --data-binary @- 2>/dev/null)" || return 0
  fi
  printf '%s' "$out" | jq -c '.data.deployments.edges[0].node // empty' 2>/dev/null || true
}

if [ "$print_id" = 1 ]; then
  node="$(latest_deployment)"
  id="$(printf '%s' "$node" | jq -r '.id // empty' 2>/dev/null || true)"
  [ -n "$id" ] || { say "error: could not read the latest Railway deployment ($auth_source)"; exit 1; }
  printf '%s\n' "$id"
  exit 0
fi

# ── wait for the new deployment (release.yml, after `railway redeploy`) ──────
node=""
if [ "$wait_mode" = 1 ]; then
  deadline=$(( $(date +%s) + timeout ))
  while :; do
    node="$(latest_deployment)"
    id="$(printf '%s' "$node" | jq -r '.id // empty' 2>/dev/null || true)"
    status="$(printf '%s' "$node" | jq -r '.status // empty' 2>/dev/null || true)"
    if [ -n "$id" ] && [ "$id" != "$wait_prev" ]; then
      case "$status" in
        SUCCESS|SLEEPING) say "deployment $id: $status"; break ;;
        FAILED|CRASHED|REMOVED|SKIPPED) say "MISMATCH: deployment $id ended $status"; exit 1 ;;
      esac
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      say "MISMATCH: no finished deployment after ${timeout}s (latest ${id:-unreadable} ${status:-})"; exit 1
    fi
    say "waiting for a new deployment (latest ${id:-unreadable} ${status:-})"
    sleep "$POLL_SECONDS"
  done
else
  node="$(latest_deployment)"
fi

# ── GHCR ─────────────────────────────────────────────────────────────────────
manifest_of() { docker buildx imagetools inspect "$1" --format '{{json .Manifest}}' 2>/dev/null; }
revision_of() {
  docker buildx imagetools inspect "$1" --format '{{json .Image}}' 2>/dev/null \
    | jq -r '[.. | objects | select(has("Labels")) | .Labels["org.opencontainers.image.revision"] // empty] | first // empty'
}

latest_manifest="$(manifest_of "$IMAGE:latest")" || die "cannot inspect $IMAGE:latest"
latest_digest="$(printf '%s' "$latest_manifest" | jq -r '.digest')"
latest_amd64="$(printf '%s' "$latest_manifest" | jq -r '[.manifests[]? | select(.platform.os == "linux" and .platform.architecture == "amd64") | .digest] | first // empty')"
latest_rev="$(revision_of "$IMAGE:latest" || true)"

if [ -z "$release_tag" ]; then
  if command -v gh >/dev/null; then
    release_tag="$(gh api "repos/$REPO/releases/latest" --jq .tag_name 2>/dev/null || true)"
  fi
  if [ -z "$release_tag" ]; then
    release_tag="$(git tag --list 'v[0-9]*' --sort=-v:refname 2>/dev/null | head -1 || true)"
    [ -n "$release_tag" ] && say "note: no GitHub Latest release readable; using the highest git tag $release_tag"
  fi
fi
[ -n "$release_tag" ] || die "no release tag found"
release_digest="$(manifest_of "$IMAGE:${release_tag#v}" | jq -r '.digest // empty' || true)"

running_digest="$(printf '%s' "$node" | jq -r '.meta.imageDigest // empty' 2>/dev/null || true)"
deploy_id="$(printf '%s' "$node" | jq -r '.id // empty' 2>/dev/null || true)"
deploy_status="$(printf '%s' "$node" | jq -r '.status // empty' 2>/dev/null || true)"

printf 'running   %s  (deployment %s %s)\n' "${running_digest:-unreadable}" "${deploy_id:-unreadable}" "$deploy_status"
printf 'latest    %s  (revision %s)\n' "$latest_digest" "${latest_rev:-none}"
printf 'release   %s  (%s)\n' "${release_digest:-missing}" "$release_tag"

verdict=MATCH
check=""
reason=""
if [ -z "$release_digest" ] || [ "$release_digest" != "$latest_digest" ]; then
  verdict=MISMATCH; reason=":latest is not $release_tag"
elif [ -n "$running_digest" ]; then
  if [ "$running_digest" = "$latest_digest" ] || { [ -n "$latest_amd64" ] && [ "$running_digest" = "$latest_amd64" ]; }; then
    check="digest (Railway meta.imageDigest)"
  else
    verdict=MISMATCH; reason="Railway runs $running_digest, not :latest"
  fi
else
  running_rev="$(curl -fsS --max-time 15 "$HEALTHZ_URL" 2>/dev/null | jq -r '.revision // empty' 2>/dev/null || true)"
  printf 'healthz   revision %s\n' "${running_rev:-unreadable}"
  if [ -n "$running_rev" ] && [ "$running_rev" != unknown ] && [ "$running_rev" = "$latest_rev" ]; then
    check="revision (/healthz)"
  else
    verdict=MISMATCH; reason="running image unverified: no Railway digest and /healthz revision '${running_rev:-unreadable}' != '${latest_rev:-none}'"
  fi
fi

if [ "$verdict" = MATCH ]; then
  printf 'MATCH  (check: %s)\n' "$check"
else
  printf 'MISMATCH  (%s)\n' "$reason"
fi

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "### Release check: $verdict"
    echo ""
    echo "| | digest | detail |"
    echo "| --- | --- | --- |"
    echo "| running | \`${running_digest:-unreadable}\` | deployment \`${deploy_id:-unreadable}\` ${deploy_status} |"
    echo "| \`:latest\` | \`$latest_digest\` | revision \`${latest_rev:-none}\` |"
    echo "| release | \`${release_digest:-missing}\` | \`$release_tag\` |"
    echo ""
    if [ "$verdict" = MATCH ]; then echo "Check that passed: $check."; else echo "Reason: $reason."; fi
  } >>"$GITHUB_STEP_SUMMARY"
fi

[ "$verdict" = MATCH ]
