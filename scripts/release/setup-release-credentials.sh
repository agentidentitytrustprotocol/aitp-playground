#!/usr/bin/env bash
#
# Provision (or rotate) the credentials that .github/workflows/release.yml needs to redeploy
# the hosted playground. Idempotent: a second run with nothing to do reports "unchanged".
#
#   scripts/release/setup-release-credentials.sh            # dry run: report state, change nothing
#   scripts/release/setup-release-credentials.sh --apply    # create what is missing, re-pipe what drifted
#   scripts/release/setup-release-credentials.sh --apply --rotate   # mint a new token, replace it everywhere,
#                                                                    # then delete the old one
#
# What it manages (names only are ever printed; values move by pipe):
#   Railway  project token "gha-aitp-playground-release" on aitp-runner / production
#            (project + environment is the narrowest scope Railway offers)
#   Doppler  aitp-playground/prd  RAILWAY_PLAYGROUND_TOKEN   (source of truth; deliberately NOT in
#            deploy/doppler-keys/playground.txt, so doppler-sync.sh never copies it into Railway)
#   GitHub   Environment "production" (branch main + tags v*, no required reviewers),
#            environment secret RAILWAY_PLAYGROUND_TOKEN,
#            repo variables RAILWAY_PROJECT_ID / RAILWAY_ENVIRONMENT_ID / RAILWAY_SERVICE_ID
#
# The secret name differs from the org-level RAILWAY_TOKEN (aitp-docs' token) on purpose: a repo
# or environment secret of the same name would shadow it, and either token can be revoked alone.
#
# Doppler and GitHub cannot be compared by reading GitHub (secrets are write-only), so the script
# records the sha256 prefix of the value it last piped to GitHub as a Doppler secret
# RAILWAY_PLAYGROUND_TOKEN_GH_SHA (a hash prefix, not a secret) and re-pipes when it differs.
#
# Needs: jq, curl, shasum, doppler (logged in), gh (repo admin), and a Railway ACCOUNT token in
# RAILWAY_API_TOKEN. Minting a project token (projectTokenCreate) needs an account token: Railway answers
# "Not Authorized" to the railway CLI login token and to project tokens. The account token lives in
# Doppler shared-ci/prd as RAILWAY_ACCOUNT_TOKEN; pass it through the environment, never argv:
#
#   RAILWAY_API_TOKEN="$(doppler secrets get RAILWAY_ACCOUNT_TOKEN --project shared-ci --config prd --plain)" \
#     scripts/release/setup-release-credentials.sh --apply
#
# Without RAILWAY_API_TOKEN the script falls back to the CLI login token, which is enough for the
# read-only dry run.

# Railway GraphQL queries are single-quoted on purpose.
# shellcheck disable=SC2016
set -euo pipefail

RAILWAY_PROJECT_ID=70754f0b-c930-4433-a268-7b408e8230e2     # aitp-runner
RAILWAY_ENVIRONMENT_ID=72787d79-e3d4-4ec8-81fa-ffdc476ad376 # production
RAILWAY_SERVICE_ID=f321666f-c5c3-45cf-9669-5f837bfcff5c     # aitp-playground
TOKEN_NAME=gha-aitp-playground-release
DOPPLER_PROJECT=aitp-playground
DOPPLER_CONFIG=prd
SECRET=RAILWAY_PLAYGROUND_TOKEN
GH_SHA_KEY=RAILWAY_PLAYGROUND_TOKEN_GH_SHA
GH_REPO=agentidentitytrustprotocol/aitp-playground
GH_ENV=production

mode=dry-run
rotate=0
for a in "$@"; do
  case "$a" in
    --apply) mode=apply ;;
    --rotate) rotate=1 ;;
    -h|--help) sed -n '2,38p' "$0"; exit 0 ;;
    *) echo "unknown argument: $a" >&2; exit 2 ;;
  esac
done
[ "$rotate" = 1 ] && [ "$mode" != apply ] && { echo "--rotate needs --apply" >&2; exit 2; }

say() { printf '%s\n' "$*" >&2; }
die() { say "error: $*"; exit 1; }
for c in jq curl shasum doppler gh; do command -v "$c" >/dev/null || die "$c is not on PATH"; done

railway_token="${RAILWAY_API_TOKEN:-$(jq -r '.user.accessToken // .user.token // empty' ~/.railway/config.json 2>/dev/null || true)}"
[ -n "$railway_token" ] || die "no Railway account token: log in with the railway CLI or set RAILWAY_API_TOKEN"

# GraphQL body on stdin; the token header comes from a process substitution so it is not in argv.
gql() {
  local out
  out="$(curl -fsS https://backboard.railway.com/graphql/v2 \
    -H @<(printf 'Authorization: Bearer %s\n' "$railway_token") -H 'content-type: application/json' \
    --data-binary @-)" || die "Railway API request failed"
  if [ "$(printf '%s' "$out" | jq -r '(.errors // []) | length')" != 0 ] || [ "$(printf '%s' "$out" | jq -r '.data == null')" = true ]; then
    die "Railway API error: $(printf '%s' "$out" | jq -c '[.errors[]?.message]')"
  fi
  printf '%s' "$out"
}

sha_prefix() { shasum -a 256 | cut -c1-10; }

# ---- Railway: existing tokens with our name ---------------------------------------------------
tokens_json="$(printf '%s' "$(jq -nc --arg p "$RAILWAY_PROJECT_ID" \
  '{query:"query($p:String!){projectTokens(projectId:$p){edges{node{id name environmentId}}}}",variables:{p:$p}}')" | gql)"
existing_ids="$(printf '%s' "$tokens_json" | jq -r --arg n "$TOKEN_NAME" --arg e "$RAILWAY_ENVIRONMENT_ID" \
  '.data.projectTokens.edges[].node | select(.name==$n and .environmentId==$e) | .id')"
n_existing="$(printf '%s' "$existing_ids" | grep -c . || true)"

# ---- Doppler: do we hold the value? -------------------------------------------------------------
doppler_has=0
if doppler secrets get "$SECRET" --project "$DOPPLER_PROJECT" --config "$DOPPLER_CONFIG" --plain >/dev/null 2>&1; then
  if [ -n "$(doppler secrets get "$SECRET" --project "$DOPPLER_PROJECT" --config "$DOPPLER_CONFIG" --plain)" ]; then doppler_has=1; fi
fi

say "railway: $n_existing token(s) named $TOKEN_NAME on production"
say "doppler: $SECRET $([ "$doppler_has" = 1 ] && echo present || echo absent) in $DOPPLER_PROJECT/$DOPPLER_CONFIG"

need_new=0
if [ "$rotate" = 1 ] || [ "$doppler_has" = 0 ]; then need_new=1; fi
if [ "$doppler_has" = 1 ] && [ "$n_existing" = 0 ]; then
  say "doppler holds a value but Railway has no token of that name: it was revoked or renamed; minting a new one"
  need_new=1
fi

if [ "$mode" = dry-run ]; then
  if [ "$need_new" = 1 ]; then say "plan: mint token $TOKEN_NAME, store in Doppler, pipe to GitHub"; else say "plan: token unchanged"; fi
fi

# ---- Mint and store (apply) ---------------------------------------------------------------------
if [ "$mode" = apply ] && [ "$need_new" = 1 ]; then
  # The new token goes straight from the GraphQL response into Doppler through a pipe.
  jq -nc --arg p "$RAILWAY_PROJECT_ID" --arg e "$RAILWAY_ENVIRONMENT_ID" --arg n "$TOKEN_NAME" \
    '{query:"mutation($i:ProjectTokenCreateInput!){projectTokenCreate(input:$i)}",variables:{i:{projectId:$p,environmentId:$e,name:$n}}}' \
    | gql | jq -j '.data.projectTokenCreate' \
    | doppler secrets set "$SECRET" --project "$DOPPLER_PROJECT" --config "$DOPPLER_CONFIG" --silent --no-interactive >/dev/null
  [ -n "$(doppler secrets get "$SECRET" --project "$DOPPLER_PROJECT" --config "$DOPPLER_CONFIG" --plain)" ] \
    || die "Doppler did not store $SECRET"
  say "minted: $TOKEN_NAME (stored in Doppler $SECRET, sha256 $(doppler secrets get "$SECRET" --project "$DOPPLER_PROJECT" --config "$DOPPLER_CONFIG" --plain | sha_prefix))"
  # Old tokens of the same name are deleted only after the new value is stored (rotation, or a lost value).
  for id in $existing_ids; do
    jq -nc --arg id "$id" '{query:"mutation($id:String!){projectTokenDelete(id:$id)}",variables:{id:$id}}' | gql >/dev/null
    say "deleted old token id ${id:0:8}…"
  done
fi

# ---- GitHub environment, secret, variables ---------------------------------------------------
if [ "$mode" = apply ]; then
  # Environment with deployment policy: main + v* tags, no reviewers (a dispatch is the deliberate act).
  jq -nc '{deployment_branch_policy:{protected_branches:false,custom_branch_policies:true}}' \
    | gh api -X PUT "repos/$GH_REPO/environments/$GH_ENV" --input - >/dev/null
  have_policies="$(gh api "repos/$GH_REPO/environments/$GH_ENV/deployment-branch-policies" --jq '[.branch_policies[] | "\(.type):\(.name)"] | join(",")')"
  for pol in branch:main tag:v*; do
    case ",$have_policies," in *",$pol,"*) ;; *)
      jq -nc --arg t "${pol%%:*}" --arg n "${pol#*:}" '{type:$t,name:$n}' \
        | gh api -X POST "repos/$GH_REPO/environments/$GH_ENV/deployment-branch-policies" --input - >/dev/null
      say "environment $GH_ENV: added deployment policy $pol" ;;
    esac
  done
fi

doppler_sha=""
if [ "$doppler_has" = 1 ] || [ "$mode" = apply ]; then
  doppler_sha="$(doppler secrets get "$SECRET" --project "$DOPPLER_PROJECT" --config "$DOPPLER_CONFIG" --plain 2>/dev/null | sha_prefix)"
fi
recorded_sha="$(doppler secrets get "$GH_SHA_KEY" --project "$DOPPLER_PROJECT" --config "$DOPPLER_CONFIG" --plain 2>/dev/null || true)"
gh_has="$(gh secret list --env "$GH_ENV" --repo "$GH_REPO" --json name --jq "[.[] | select(.name==\"$SECRET\")] | length" 2>/dev/null || echo 0)"

if [ "$gh_has" = 1 ] && [ -n "$doppler_sha" ] && [ "$doppler_sha" = "$recorded_sha" ]; then
  say "github: $GH_ENV/$SECRET unchanged (sha256 $doppler_sha)"
elif [ "$mode" = apply ]; then
  doppler secrets get "$SECRET" --project "$DOPPLER_PROJECT" --config "$DOPPLER_CONFIG" --plain \
    | gh secret set "$SECRET" --env "$GH_ENV" --repo "$GH_REPO" >/dev/null
  printf '%s' "$doppler_sha" | doppler secrets set "$GH_SHA_KEY" --project "$DOPPLER_PROJECT" --config "$DOPPLER_CONFIG" --silent --no-interactive >/dev/null
  say "github: $GH_ENV/$SECRET set (sha256 $doppler_sha)"
else
  say "plan: pipe $SECRET to GitHub environment $GH_ENV"
fi

for kv in "RAILWAY_PROJECT_ID=$RAILWAY_PROJECT_ID" "RAILWAY_ENVIRONMENT_ID=$RAILWAY_ENVIRONMENT_ID" "RAILWAY_SERVICE_ID=$RAILWAY_SERVICE_ID"; do
  k="${kv%%=*}"; v="${kv#*=}"
  cur="$(gh variable get "$k" --repo "$GH_REPO" 2>/dev/null || true)"
  if [ "$cur" = "$v" ]; then say "github var $k unchanged"
  elif [ "$mode" = apply ]; then gh variable set "$k" --repo "$GH_REPO" --body "$v" >/dev/null; say "github var $k set"
  else say "plan: set github var $k"; fi
done
