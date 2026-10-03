#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 4 ]]; then
  echo "usage: verify_draft_release_assets.sh <tag> <asset-directory> <owner/repo> <source-commit>" >&2
  exit 2
fi

tag=$1
asset_dir=$2
repository=$3
source_commit=$4
if [[ ! "$source_commit" =~ ^[0-9a-f]{40,64}$ ]]; then
  echo "draft recovery requires a full trusted source commit SHA" >&2
  exit 1
fi
source "$(dirname "${BASH_SOURCE[0]}")/validate_release_checksums.sh"
validate_release_checksums "$asset_dir"

for asset in SHA256SUMS bundle-metadata.json clinvar.sqlite.zst clinvar.sqlite.zst.sha256; do
  if ! gh attestation verify "$asset_dir/$asset" \
    --repo "$repository" \
    --signer-workflow "$repository/.github/workflows/data-bundle.yml" \
    --source-ref refs/heads/main \
    --source-digest "$source_commit" \
    --predicate-type https://slsa.dev/provenance/v1; then
    echo "draft release $tag is incomplete: asset $asset lacks build provenance for source commit $source_commit; refusing publication" >&2
    exit 1
  fi
done
