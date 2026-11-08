#!/usr/bin/env bash
#
# Release gate for .github/workflows/release.yml. Read-only: it never tags, retags or deploys.
# It refuses (exit 1, reason on stderr) unless the commit may be released, and on success prints
# the facts the workflow needs as key=value lines (also appended to $GITHUB_OUTPUT when set).
#
# A release promotes an image; it never rebuilds one (DECISIONS.md D-33). So the gate asks:
#   1. Are the credentials present? (RAILWAY_TOKEN and the three Railway ids: a release that
#      retags and then cannot redeploy leaves :latest ahead of what runs.)
#   2. Is VERSION well formed (X.Y.Z), and greater than every other v* tag? A tag vVERSION that
#      already exists must point at COMMIT: that is a human-pushed tag or a resumed release.
#   3. Is COMMIT on main?
#   4. Does GHCR hold sha-<short> for COMMIT, with the revision label equal to COMMIT?
#   5. Did the docker.yml checks on COMMIT ("docker-compose e2e", "Build & push playground image")
#      conclude success? ci.yml has no push-to-main trigger (D-29); its checks are covered by
#      strict branch protection plus "the commit is on main".
# Steps 4 and 5 wait (bounded) for the main build to finish; anything but success refuses at once.
#
# Rollback mode (RELEASE_MODE=rollback) re-points :latest at an existing release, so it asks only
# that tag vVERSION exists on main and that GHCR's VERSION image carries that commit's revision.
#
# Environment (all inputs; nothing is read from argv):
#   RELEASE_MODE          release (default) | rollback
#   RELEASE_EVENT         dispatch (default) | tag   (tag: a human pushed vVERSION; it must exist)
#   VERSION               X.Y.Z, no leading v
#   COMMIT                full commit sha to release (rollback: optional, resolved from the tag)
#   RAILWAY_TOKEN, RAILWAY_PROJECT_ID, RAILWAY_ENVIRONMENT_ID, RAILWAY_SERVICE_ID   must be non-empty
#   IMAGE                 default ghcr.io/agentidentitytrustprotocol/aitp-playground
#   GITHUB_REPOSITORY     default agentidentitytrustprotocol/aitp-playground
#   MAIN_REF              default origin/main
#   REQUIRED_CHECKS       newline-separated check-run names (default: the two docker.yml jobs)
#   WAIT_TIMEOUT_SECONDS  default 2100 (35 min);  POLL_SECONDS default 30
#
# Needs git, jq, gh (authenticated), docker buildx. Tested with PATH stubs in
# tests/unit/test_release_preflight.py.

# The docker --format templates are single-quoted on purpose.
# shellcheck disable=SC2016
set -euo pipefail

RELEASE_MODE="${RELEASE_MODE:-release}"
RELEASE_EVENT="${RELEASE_EVENT:-dispatch}"
VERSION="${VERSION:-}"
COMMIT="${COMMIT:-}"
IMAGE="${IMAGE:-ghcr.io/agentidentitytrustprotocol/aitp-playground}"
REPO="${GITHUB_REPOSITORY:-agentidentitytrustprotocol/aitp-playground}"
MAIN_REF="${MAIN_REF:-origin/main}"
WAIT_TIMEOUT_SECONDS="${WAIT_TIMEOUT_SECONDS:-2100}"
POLL_SECONDS="${POLL_SECONDS:-30}"
DEFAULT_CHECKS=$'docker-compose e2e\nBuild & push playground image'
REQUIRED_CHECKS="${REQUIRED_CHECKS:-$DEFAULT_CHECKS}"

say() { printf 'preflight: %s\n' "$*" >&2; }
refuse() { say "REFUSED: $*"; exit 1; }

for c in git jq gh docker; do command -v "$c" >/dev/null || refuse "$c is not on PATH"; done

# ── 1. credentials (names only; a value is never printed) ────────────────────
for v in RAILWAY_TOKEN RAILWAY_PROJECT_ID RAILWAY_ENVIRONMENT_ID RAILWAY_SERVICE_ID; do
  [ -n "${!v:-}" ] || refuse "$v is empty (Environment 'production' secret / repo variable)"
done

case "$RELEASE_MODE" in release|rollback) ;; *) refuse "RELEASE_MODE must be release or rollback, got '$RELEASE_MODE'" ;; esac
case "$RELEASE_EVENT" in dispatch|tag) ;; *) refuse "RELEASE_EVENT must be dispatch or tag, got '$RELEASE_EVENT'" ;; esac

# ── 2. version ───────────────────────────────────────────────────────────────
semver_re='^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'
[[ "$VERSION" =~ $semver_re ]] || refuse "version '$VERSION' is not X.Y.Z"
TAG="v$VERSION"

# 0 when $1 > $2 (both X.Y.Z).
version_gt() {
  local a1 a2 a3 b1 b2 b3
  IFS=. read -r a1 a2 a3 <<<"$1"
  IFS=. read -r b1 b2 b3 <<<"$2"
  if [ "$a1" -ne "$b1" ]; then [ "$a1" -gt "$b1" ]; return; fi
  if [ "$a2" -ne "$b2" ]; then [ "$a2" -gt "$b2" ]; return; fi
  [ "$a3" -gt "$b3" ]
}

tag_commit() { git rev-parse -q --verify "refs/tags/$1^{commit}" 2>/dev/null || true; }

git rev-parse -q --verify "$MAIN_REF^{commit}" >/dev/null || refuse "$MAIN_REF is not a commit (fetch-depth 0?)"

existing="$(tag_commit "$TAG")"
if [ "$RELEASE_MODE" = rollback ]; then
  [ -n "$existing" ] || refuse "rollback target $TAG is not a tag"
  [ -z "$COMMIT" ] || [ "$COMMIT" = "$existing" ] || refuse "COMMIT $COMMIT is not $TAG's commit $existing"
  COMMIT="$existing"
  prev_tag=""
else
  [[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || refuse "COMMIT must be a full 40-hex sha, got '$COMMIT'"
  if [ "$RELEASE_EVENT" = tag ] && [ -z "$existing" ]; then refuse "tag $TAG does not exist"; fi
  if [ -n "$existing" ] && [ "$existing" != "$COMMIT" ]; then
    refuse "tag $TAG already exists at $existing, not at $COMMIT"
  fi
  prev_tag=""
  while IFS= read -r t; do
    [ "$t" = "$TAG" ] && continue
    [[ "${t#v}" =~ $semver_re ]] || continue
    prev_tag="$t"; break
  done < <(git tag --list 'v*' --sort=-v:refname)
  if [ -n "$prev_tag" ] && ! version_gt "$VERSION" "${prev_tag#v}"; then
    refuse "version $VERSION is not greater than the last tag $prev_tag"
  fi
fi

# ── 3. on main ───────────────────────────────────────────────────────────────
git cat-file -e "$COMMIT^{commit}" 2>/dev/null || refuse "commit $COMMIT is not in this checkout"
git merge-base --is-ancestor "$COMMIT" "$MAIN_REF" || refuse "commit $COMMIT is not on $MAIN_REF"
short="${COMMIT:0:7}"

if [ "$RELEASE_MODE" = rollback ]; then source_ref="$IMAGE:$VERSION"; else source_ref="$IMAGE:sha-$short"; fi

# ── 4/5. image and checks, with a bounded wait ──────────────────────────────
# Each probe sets $pending (empty when satisfied) and refuses outright on a wrong answer.
source_digest=""
probe_image() {
  local manifest image rev
  pending=""
  if ! manifest="$(docker buildx imagetools inspect "$source_ref" --format '{{json .Manifest}}' 2>/dev/null)"; then
    pending="image $source_ref not in GHCR yet"; return
  fi
  source_digest="$(printf '%s' "$manifest" | jq -r '.digest // empty')"
  [[ "$source_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || refuse "could not read the digest of $source_ref"
  image="$(docker buildx imagetools inspect "$source_ref" --format '{{json .Image}}' 2>/dev/null)" \
    || refuse "could not read the image config of $source_ref"
  rev="$(printf '%s' "$image" | jq -r '[.. | objects | select(has("Labels")) | .Labels["org.opencontainers.image.revision"] // empty] | first // empty')"
  [ "$rev" = "$COMMIT" ] || refuse "$source_ref carries revision '${rev:-none}', expected $COMMIT"
}

probe_checks() {
  local runs name latest status conclusion
  pending=""
  runs="$(gh api "repos/$REPO/commits/$COMMIT/check-runs?per_page=100")" || { pending="check-runs API unavailable"; return; }
  while IFS= read -r name; do
    [ -n "$name" ] || continue
    latest="$(printf '%s' "$runs" | jq -c --arg n "$name" '[.check_runs[]? | select(.name == $n)] | max_by(.id) // empty')"
    if [ -z "$latest" ]; then pending="${pending:+$pending; }check '$name' not reported yet"; continue; fi
    status="$(printf '%s' "$latest" | jq -r '.status')"
    conclusion="$(printf '%s' "$latest" | jq -r '.conclusion // ""')"
    if [ "$status" != completed ]; then pending="${pending:+$pending; }check '$name' is $status"; continue; fi
    [ "$conclusion" = success ] || refuse "check '$name' on $short concluded '$conclusion', not success"
  done <<<"$REQUIRED_CHECKS"
}

deadline=$(( $(date +%s) + WAIT_TIMEOUT_SECONDS ))
while :; do
  probe_image
  waiting="$pending"
  if [ -z "$waiting" ] && [ "$RELEASE_MODE" = release ]; then probe_checks; waiting="$pending"; fi
  [ -n "$waiting" ] || break
  # A rollback target was published long ago; there is nothing to wait for.
  [ "$RELEASE_MODE" = release ] || refuse "$waiting"
  if [ "$(date +%s)" -ge "$deadline" ]; then refuse "gave up after ${WAIT_TIMEOUT_SECONDS}s: $waiting"; fi
  say "waiting: $waiting"
  sleep "$POLL_SECONDS"
done

say "ok: $RELEASE_MODE $TAG at $COMMIT, source $source_ref = $source_digest"
out="mode=$RELEASE_MODE
version=$VERSION
minor=${VERSION%.*}
tag=$TAG
commit=$COMMIT
short=$short
prev_tag=$prev_tag
source_ref=$source_ref
source_digest=$source_digest"
printf '%s\n' "$out"
if [ -n "${GITHUB_OUTPUT:-}" ]; then printf '%s\n' "$out" >>"$GITHUB_OUTPUT"; fi
