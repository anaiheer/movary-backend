#!/usr/bin/env bash
# Canonical release verification script. Authoritative copy lives in
# movary-orchestration/ci/verify-release.sh; sibling repos keep a synced copy
# under scripts/verify-release.sh (checked by ci/check-drift.sh).
#
# Usage: ./scripts/verify-release.sh <production-branch> [version-source]
#   version-source: "app"         -> python3 -c "import app; print(app.__version__)"
#                   "package.json" -> node -p "require('./package.json').version"
#                   (omitted)       -> skip the version match check
set -euo pipefail

branch="${1:?usage: verify-release.sh <production-branch> [version-source]}"
version_source="${2:-}"

tag="${GITHUB_REF_NAME:-${GITEA_REF_NAME:-}}"
if [[ -z "$tag" ]]; then
  echo "::error::Unable to resolve release tag (GITHUB_REF_NAME / GITEA_REF_NAME empty)." >&2
  exit 1
fi
if [[ ! "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "::error::Release tag must be vX.Y.Z: $tag" >&2
  exit 1
fi
expected="${tag#v}"

if ! git fetch origin "$branch" --depth=1; then
  echo "::error::Unable to fetch origin/$branch." >&2
  exit 1
fi
if ! git merge-base --is-ancestor "$GITHUB_SHA" "origin/$branch"; then
  echo "::error::Tagged commit $GITHUB_SHA is not on origin/$branch." >&2
  exit 1
fi

actual=""
case "$version_source" in
  "")
    ;;
  app)
    actual=$(python3 -c "import app; print(app.__version__)")
    ;;
  package.json)
    actual=$(node -p "require('./package.json').version")
    ;;
  *)
    echo "::error::Unknown version source: $version_source" >&2
    exit 1
    ;;
esac

if [[ -n "$version_source" && "$expected" != "$actual" ]]; then
  echo "::error::Tag version $expected does not match $version_source version $actual." >&2
  exit 1
fi

echo "Release verification passed: $tag on origin/$branch"
