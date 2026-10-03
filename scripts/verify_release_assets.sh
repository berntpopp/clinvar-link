#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: verify_release_assets.sh <tag> <asset-directory> <owner/repo>" >&2
  exit 2
fi

tag=$1
asset_dir=$2
repository=$3

source "$(dirname "${BASH_SOURCE[0]}")/validate_release_checksums.sh"
validate_release_checksums "$asset_dir"

for asset in SHA256SUMS bundle-metadata.json clinvar.sqlite.zst clinvar.sqlite.zst.sha256; do
  gh release verify-asset "$tag" "$asset_dir/$asset" --repo "$repository"
done
