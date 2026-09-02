#!/usr/bin/env bash
# Pin the container smoke stack to the exact reviewed ClinVar data release.
#
# The smoke stack is built from `docker/docker-compose.yml`, whose defaults are the
# DEVELOPMENT ones (`BUNDLE_URL=latest`): they resolve whatever bundle release happens to
# be newest, which is not a reviewable identity and would make the runtime-v1 gate compare
# the deployment against a moving target. This hook replaces those defaults with the exact
# release named in `container-release.json`, for both the init sidecar and the application.
#
# The trust root is what this commit reviewed: `.data.release_tag` and the compressed
# SHA-256 in `.smoke_environment`. `bundle-metadata.json` is downloaded only as a carrier
# for the expanded-tree digest and the schema version, and is cross-checked against that
# trust root before either value is used. A tampered metadata file can make the smoke
# stack fail; it can never make the init sidecar accept different bundle bytes, because
# the compressed digest asserted here is the one the sidecar verifies against.
#
# The bundle itself is NOT downloaded here: `clinvar-data-init` is declared with
# `egress: approved-networks` and fetches the pinned asset URL itself, exactly as it does
# on the server.
#
# Contract (set by the router's reusable container-ci / container-release workflows):
#   GF_SMOKE_FIXTURE_DIR  directory to write fixtures into
#   GF_SMOKE_ENV_FILE     file to append bounded KEY=VALUE assignments to
set -euo pipefail

: "${GF_SMOKE_FIXTURE_DIR:?GF_SMOKE_FIXTURE_DIR is required}"
: "${GF_SMOKE_ENV_FILE:?GF_SMOKE_ENV_FILE is required}"

repository="${GITHUB_REPOSITORY:-berntpopp/clinvar-link}"
config="$(dirname "$0")/../container-release.json"

release_tag="$(jq -er '.data.release_tag' "$config")"
identity_digest="$(jq -er '.data.digest' "$config")"
bundle_assignment="$(jq -er \
  '.smoke_environment[] | select(startswith("CLINVAR_LINK_BUNDLE_EXPECTED_SHA256="))' "$config")"
expected="${bundle_assignment#CLINVAR_LINK_BUNDLE_EXPECTED_SHA256=}"
[[ "$expected" =~ ^[0-9a-f]{64}$ ]] || {
  echo "container-release.json smoke bundle digest is not a sha256 hex digest" >&2
  exit 1
}

base="https://github.com/${repository}/releases/download/${release_tag}"
asset_url="${base}/clinvar.sqlite.zst"
metadata="$GF_SMOKE_FIXTURE_DIR/bundle-metadata.json"
mkdir -p "$GF_SMOKE_FIXTURE_DIR"
curl -fsSL --proto '=https' --tlsv1.2 --max-time 120 -o "$metadata" "${base}/bundle-metadata.json"

# The metadata is not a trust root; it must agree with what this commit reviewed.
test "$(jq -er '.tag' "$metadata")" = "$release_tag"
test "$(jq -er '.asset_sha256' "$metadata")" = "$expected"

expanded="$(jq -er '.expanded_tree_sha256' "$metadata")"
schema_version="$(jq -er '.schema_version' "$metadata")"

{
  echo "CLINVAR_LINK_ENVIRONMENT=production"
  echo "CLINVAR_LINK_BUNDLE_URL=${asset_url}"
  echo "CLINVAR_LINK_BUNDLE_RELEASE_TAG=${release_tag}"
  echo "CLINVAR_LINK_BUNDLE_EXPECTED_SHA256=${expected}"
  echo "CLINVAR_LINK_BUNDLE_EXPECTED_EXPANDED_SHA256=${expanded}"
  echo "CLINVAR_LINK_BUNDLE_EXPECTED_SCHEMA_VERSION=${schema_version}"
  echo "CLINVAR_LINK_DEVELOPMENT_LATEST=false"
  echo "CLINVAR_LINK_DATA_IDENTITY_DIGEST=${identity_digest}"
} >> "$GF_SMOKE_ENV_FILE"

echo "pinned smoke stack to ${release_tag} (${asset_url})"
