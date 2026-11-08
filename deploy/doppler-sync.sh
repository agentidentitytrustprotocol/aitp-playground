#!/usr/bin/env bash
#
# Sync one aitp-runner Railway service's variables from Doppler, the source of truth.
#
#   deploy/doppler-sync.sh <service>                 # dry run (default): key names and changed/unchanged, never values
#   deploy/doppler-sync.sh <service> --apply         # push changed allowlisted keys from Doppler into Railway
#   deploy/doppler-sync.sh <service> --apply --no-redeploy   # set them but leave the running deployment alone
#
#   <service>   Railway service (aitp-runner)   Doppler project/config   allowlist
#   playground  aitp-playground                 aitp-playground/prd      deploy/doppler-keys/playground.txt
#
# Only the keys listed in the service's allowlist are managed. The script sets
# and updates those; it never deletes a variable, never touches a key it does
# not list, and refuses RAILWAY_* system variables and Railway references
# (values containing "${{"), which stay Railway-managed. A listed key that is
# set on neither side (Doppler nor Railway) is reported "absent" and skipped,
# so the allowlist can name a key before it gets a value; a key Railway has
# and Doppler lacks is refused, so drift is never silent.
#
# Values move by pipe only: Doppler -> jq -> Railway GraphQL request body on
# stdin (printf into pipes, never here-strings, which older bash spools to a
# temp file). Nothing is printed, written to disk or passed in argv. Each key
# is compared exactly inside jq; the summary check is a sha256 of the sorted
# KEY=VALUE lines of the managed keys, Doppler vs Railway (a value with a
# newline spans several lines; both sides hash it the same way). --apply
# writes the Doppler values it read for the plan, not a fresh read. A
# redeploy happens only when at least one value changed and the post-apply
# hash matches.
#
# Needs git, jq, curl and the doppler CLI logged in, plus a Railway token: the
# railway CLI login (~/.railway/config.json) or RAILWAY_API_TOKEN (an account
# or workspace token; a project token cannot use these GraphQL calls).
# --apply refuses to run on a dirty tree, so the allowlist it applies is the
# committed one.
#
# Derived from acdp-playground deploy/doppler-sync.sh (acdp-registry-rs #569 template):
# the service table, the "absent" status, the token header (kept out of argv),
# no here-strings and the single Doppler read differ.

# Railway GraphQL queries are single-quoted on purpose.
# shellcheck disable=SC2016
set -euo pipefail

RAILWAY_PROJECT_ID=70754f0b-c930-4433-a268-7b408e8230e2 # Railway: aitp-runner
RAILWAY_ENVIRONMENT=production
DOPPLER_CONFIG=prd

target="${1:-}"
case "$target" in
  playground) RAILWAY_SERVICE=aitp-playground; DOPPLER_PROJECT=aitp-playground ;;
  -h|--help) sed -n '2,38p' "$0"; exit 0 ;;
  *) echo "usage: $0 <playground> [--apply] [--no-redeploy]" >&2; exit 2 ;;
esac
shift

mode=dry-run
redeploy=1
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) mode=dry-run ;;
    --apply) mode=apply ;;
    --no-redeploy) redeploy=0 ;;
    -h|--help) sed -n '2,38p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '%s\n' "$*" >&2; }
die() { say "error: $*"; exit 1; }
for c in git jq curl doppler shasum; do command -v "$c" >/dev/null || die "$c is not on PATH"; done

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(git -C "$here" rev-parse --show-toplevel)"
keys_file="$here/doppler-keys/$target.txt"
keys_name="doppler-keys/$target.txt"

if [ "$mode" = apply ] && [ -n "$(git -C "$repo" status --porcelain)" ]; then
  die "the working tree is dirty; commit or stash first so --apply uses the committed allowlist"
fi

# The allowlist: one KEY per line; '#' comments and blank lines are ignored.
[ -f "$keys_file" ] || die "$keys_name not found"
keys=()
while IFS= read -r line; do
  k="${line%%#*}"
  k="$(printf '%s' "$k" | tr -d '[:space:]')"
  [ -n "$k" ] || continue
  [[ "$k" =~ ^[A-Z_][A-Z0-9_]*$ ]] || die "$keys_name: '$k' is not a valid variable name"
  [[ "$k" == RAILWAY_* ]] && die "$keys_name: $k is a Railway system variable; Railway owns it"
  keys+=("$k")
done <"$keys_file"
[ "${#keys[@]}" -gt 0 ] || die "$keys_file lists no keys"
keys_json="$(printf '%s\n' "${keys[@]}" | jq -R . | jq -sc 'unique')"

railway_token="${RAILWAY_API_TOKEN:-$(jq -r '.user.accessToken // .user.token // empty' ~/.railway/config.json 2>/dev/null || true)}"
[ -n "$railway_token" ] || die "no Railway token: log in with the railway CLI or set RAILWAY_API_TOKEN"

# POST a GraphQL request whose JSON body arrives on stdin, so values never reach argv.
# The token header comes from a process substitution fed by the printf builtin,
# so the token is not in curl's argv (visible in ps) either.
railway_gql() {
  curl -fsS https://backboard.railway.com/graphql/v2 \
    -H @<(printf 'Authorization: Bearer %s\n' "$railway_token") -H 'content-type: application/json' --data-binary @- |
    jq -e 'if .errors then error("railway: \(.errors | map(.message) | join("; "))") else .data end'
}
gql() { jq -nc --arg q "$1" --argjson v "${2:-{\}}" '{query: $q, variables: $v}' | railway_gql; }

ids="$(gql 'query($p: String!){project(id: $p){environments{edges{node{id name}}} services{edges{node{id name}}}}}' \
  "$(jq -nc --arg p "$RAILWAY_PROJECT_ID" '{p: $p}')")" || die "Railway project lookup failed (see above)"
env_id="$(jq -r --arg n "$RAILWAY_ENVIRONMENT" '(.project.environments.edges // [])[].node | select(.name == $n) | .id' <<<"$ids")"
svc_id="$(jq -r --arg n "$RAILWAY_SERVICE" '(.project.services.edges // [])[].node | select(.name == $n) | .id' <<<"$ids")"
[ -n "$env_id" ] || die "Railway environment $RAILWAY_ENVIRONMENT not found in project $RAILWAY_PROJECT_ID"
[ -n "$svc_id" ] || die "Railway service $RAILWAY_SERVICE not found in project $RAILWAY_PROJECT_ID"
scope="$(jq -nc --arg p "$RAILWAY_PROJECT_ID" --arg e "$env_id" --arg s "$svc_id" '{p: $p, e: $e, s: $s}')"

# Raw (unrendered) service variables, so a reference shows up as ${{...}}, not its resolved value.
railway_vars() {
  gql 'query($p: String!, $e: String!, $s: String!){variables(projectId: $p, environmentId: $e, serviceId: $s, unrendered: true)}' \
    "$scope" | jq -c '.variables'
}
doppler_vars() {
  doppler secrets download --project "$DOPPLER_PROJECT" --config "$DOPPLER_CONFIG" --no-file --format json
}

# sha256 of the sorted KEY=VALUE lines of the managed keys (a missing key hashes as absent).
managed_hash() {
  jq -r --argjson k "$keys_json" '. as $v | $k[] as $key | select($v | has($key)) | "\($key)=\($v[$key])"' | LC_ALL=C sort | shasum -a 256 | cut -c1-16
}

rv="$(railway_vars)" || die "reading Railway variables failed (see above)"
dv="$(doppler_vars)" || die "doppler secrets download failed for $DOPPLER_PROJECT/$DOPPLER_CONFIG (see above)"
printf '%s' "$rv" | jq -e 'type == "object"' >/dev/null || die "Railway variables are not a JSON object"
printf '%s' "$dv" | jq -e 'type == "object"' >/dev/null || die "Doppler download is not a JSON object"

# Per key: status only. The comparison happens inside jq; values never leave it.
plan="$(printf '%s\n%s\n' "$dv" "$rv" | jq -sc --argjson k "$keys_json" '
  .[0] as $d | .[1] as $r |
  [$k[] | . as $key | {key: $key, status: (
    if ($d | has($key) | not) and ($r | has($key) | not) then "absent"
    elif ($d | has($key) | not) then "missing-in-doppler"
    elif ($d[$key] | tostring | contains("${{")) then "doppler-value-is-a-reference"
    elif ($r | has($key)) and ($r[$key] | contains("${{")) then "railway-reference"
    elif ($r | has($key) | not) then "new"
    elif $r[$key] == $d[$key] then "unchanged"
    else "changed" end)}]')"

say "Doppler $DOPPLER_PROJECT/$DOPPLER_CONFIG -> Railway $RAILWAY_SERVICE ($RAILWAY_ENVIRONMENT), ${#keys[@]} managed key(s):"
jq -r '.[] | "  \(.key)\t\(.status)"' <<<"$plan" >&2
unmanaged="$(printf '%s' "$rv" | jq -r --argjson k "$keys_json" 'keys - $k | join(" ")')"
[ -z "$unmanaged" ] || say "  left alone (not in $keys_name): $unmanaged"

bad="$(jq -r '[.[] | select(.status | IN("missing-in-doppler", "doppler-value-is-a-reference", "railway-reference")) | .key] | join(" ")' <<<"$plan")"
[ -z "$bad" ] || die "refusing: $bad (see status above; a Railway reference must stay Railway-managed, so drop it from $keys_name)"
todo="$(jq -r '.[] | select(.status | IN("new", "changed")) | .key' <<<"$plan")"

before="$(printf '%s' "$rv" | managed_hash)"
want="$(printf '%s' "$dv" | managed_hash)"
say "hash: railway $before, doppler $want ($([ "$before" = "$want" ] && echo match || echo differ))"

if [ "$mode" = dry-run ]; then
  if [ -n "$todo" ]; then say "dry run: $(wc -w <<<"$todo" | tr -d ' ') key(s) would change; rerun with --apply"; else say "dry run: nothing to change"; fi
  exit 0
fi

if [ -z "$todo" ]; then
  say "apply: nothing to change; no redeploy"
  exit 0
fi

done_keys=""
for key in $todo; do
  # The planned Doppler value goes jq -> curl stdin; it is never in argv or on disk.
  if ! printf '%s' "$dv" | jq -c --arg key "$key" --argjson s "$scope" '{
      query: "mutation($i: VariableUpsertInput!){variableUpsert(input: $i)}",
      variables: {i: {projectId: $s.p, environmentId: $s.e, serviceId: $s.s, name: $key, value: .[$key], skipDeploys: true}}}' |
    railway_gql >/dev/null; then
    die "setting $key failed; already set: ${done_keys:-none}; no redeploy. Re-running is safe: set keys show as unchanged"
  fi
  done_keys="${done_keys:+$done_keys }$key"
  say "  set $key"
done

after="$(railway_vars | managed_hash)" || die "reading Railway back after apply failed; set: $done_keys; no redeploy"
[ "$after" = "$want" ] || die "hash after apply is $after, expected $want; check the service in Railway"
say "hash after apply: $after (match)"

if [ "$redeploy" = 1 ]; then
  gql 'mutation($e: String!, $s: String!){serviceInstanceRedeploy(environmentId: $e, serviceId: $s)}' "$scope" >/dev/null ||
    die "values set and verified, but the redeploy call failed; redeploy $RAILWAY_SERVICE in Railway"
  say "redeploy triggered for $RAILWAY_SERVICE so the new values take effect"
else
  say "no redeploy (--no-redeploy): the values apply on the next deploy"
fi
