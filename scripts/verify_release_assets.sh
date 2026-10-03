#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: verify_release_assets.sh <tag> <asset-directory> <owner/repo>" >&2
  exit 2
fi

tag=$1
asset_dir=$2
repository=$3

if [[ ! -f "$asset_dir/SHA256SUMS" ]]; then
  echo "release assets are missing SHA256SUMS" >&2
  exit 1
fi
(cd "$asset_dir" && sha256sum --check SHA256SUMS)

while read -r _ asset; do
  [[ -n "$asset" ]] || continue
  gh release verify-asset "$tag" "$asset_dir/$asset" --repo "$repository"
done < "$asset_dir/SHA256SUMS"
