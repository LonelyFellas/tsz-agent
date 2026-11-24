#!/usr/bin/env bash
# Exit 3 for an obsolete candidate, 1 for unverifiable or mismatched evidence.
set -euo pipefail
repo=LonelyFellas/tsz-agent
sha=$1
run_id=$2
run_number=$3
attempt=$4
[[ "$sha" =~ ^[0-9a-f]{40}$ && "$run_id" =~ ^[1-9][0-9]*$ \
  && "$run_number" =~ ^[1-9][0-9]*$ && "$attempt" =~ ^[1-9][0-9]*$ ]]

obsolete() {
  echo "Agent deployment candidate is no longer current"
  exit 3
}

main=$(gh api "repos/$repo/git/ref/heads/main" --jq '.object.sha')
[[ "$main" =~ ^[0-9a-f]{40}$ ]]
[[ "$main" == "$sha" ]] || obsolete

latest=$(gh api "repos/$repo/actions/workflows/ci.yml/runs?branch=main&event=push&head_sha=$sha&per_page=1" \
  --jq '.workflow_runs[0].id')
[[ "$latest" =~ ^[1-9][0-9]*$ ]]
[[ "$latest" == "$run_id" ]] || obsolete

data=$(gh api "repos/$repo/actions/runs/$run_id")
recorded_attempt=$(jq -er '.run_attempt | numbers' <<< "$data")
if (( recorded_attempt > attempt )); then
  obsolete
fi
jq -e --arg sha "$sha" --arg repo "$repo" --argjson number "$run_number" --argjson attempt "$attempt" '
  .name == "CI" and .head_sha == $sha and .head_repository.full_name == $repo
  and .head_branch == "main" and .event == "push" and .run_number == $number
  and .run_attempt == $attempt and .status == "completed" and .conclusion == "success"
' <<< "$data" > /dev/null
