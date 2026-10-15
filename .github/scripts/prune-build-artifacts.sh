#!/usr/bin/env bash
set -euo pipefail
# Only a completed branch build may supersede older successful branch builds.
[[ "$GITHUB_REF_TYPE" == branch ]] || exit 0
: "${GH_REPO:?}" "${GITHUB_RUN_ID:?}" "${BUILD_WORKFLOW:?}" "${ARTIFACT_PREFIX:?}"
runs=$(gh api --paginate "repos/$GH_REPO/actions/workflows/$BUILD_WORKFLOW/runs?branch=$GITHUB_REF_NAME&status=success&per_page=100" --jq '.workflow_runs[] | select(.event == "push" or .event == "workflow_dispatch") | .id')
while IFS= read -r run; do
  [[ -n "$run" && "$run" -lt "$GITHUB_RUN_ID" ]] || continue
  artifacts=$(gh api --paginate "repos/$GH_REPO/actions/runs/$run/artifacts?per_page=100" --jq ".artifacts[] | select(.expired == false and (.name | startswith(\"$ARTIFACT_PREFIX\"))) | .id")
  while IFS= read -r artifact; do
    [[ -n "$artifact" ]] || continue
    gh api --method DELETE "repos/$GH_REPO/actions/artifacts/$artifact"
    echo "Deleted superseded artifact $artifact from successful run $run"
  done <<< "$artifacts"
done <<< "$runs"
