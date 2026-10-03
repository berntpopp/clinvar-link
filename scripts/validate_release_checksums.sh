#!/usr/bin/env bash

validate_release_checksums() {
  local asset_dir=$1
  local line asset
  local -A expected=(
    [bundle-metadata.json]=1
    [clinvar.sqlite.zst]=1
    [clinvar.sqlite.zst.sha256]=1
  )
  local -A seen=()
  local count=0

  if [[ ! -f "$asset_dir/SHA256SUMS" ]]; then
    echo "release assets are missing SHA256SUMS" >&2
    return 1
  fi

  while IFS= read -r line; do
    if [[ ! "$line" =~ ^[0-9a-f]{64}\ \ ([A-Za-z0-9._-]+)$ ]]; then
      echo "SHA256SUMS contains a malformed entry" >&2
      return 1
    fi
    asset=${BASH_REMATCH[1]}
    if [[ -z "${expected[$asset]:-}" ]]; then
      echo "SHA256SUMS contains an unexpected asset: $asset" >&2
      return 1
    fi
    if [[ -n "${seen[$asset]:-}" ]]; then
      echo "SHA256SUMS contains a duplicate asset: $asset" >&2
      return 1
    fi
    seen[$asset]=1
    count=$((count + 1))
  done < "$asset_dir/SHA256SUMS"

  if [[ $count -ne 3 ]]; then
    echo "SHA256SUMS must list exactly the three canonical data assets" >&2
    return 1
  fi
  for asset in "${!expected[@]}"; do
    if [[ -z "${seen[$asset]:-}" || ! -f "$asset_dir/$asset" ]]; then
      echo "release assets are missing required file: $asset" >&2
      return 1
    fi
  done
  (cd "$asset_dir" && sha256sum --check SHA256SUMS)
}
