#!/usr/bin/env bash
# Select the immutable release state for TAG from dist/bundle-metadata.json.
# Runs ungated (read-only token) before the data-release gate and again inside it.
set -euo pipefail
: "${TAG:?}" "${GITHUB_REPOSITORY:?}" "${GITHUB_OUTPUT:?}"
state=create
if gh release view "$TAG" --repo "$GITHUB_REPOSITORY" --json isDraft >/tmp/release.json; then
  mkdir -p /tmp/existing
  gh release download "$TAG" --repo "$GITHUB_REPOSITORY" --dir /tmp/existing
  expected_assets='SHA256SUMS
bundle-metadata.json
clinvar.sqlite.zst
clinvar.sqlite.zst.sha256'
  actual_assets="$(find /tmp/existing -maxdepth 1 -type f -printf '%f\n' | sort)"
  test "$actual_assets" = "$expected_assets" || {
    echo "release $TAG has missing, corrupt, or extra assets" >&2
    exit 1
  }
  (cd /tmp/existing && sha256sum --check SHA256SUMS)
  state="$(uv run python -m clinvar_link.ingest.release_metadata state \
    --current dist/bundle-metadata.json --existing /tmp/existing/bundle-metadata.json \
    $(test "$(jq -r .isDraft /tmp/release.json)" = true && echo --draft))"
  case "$state" in
    published_noop)
      ./scripts/verify_release_assets.sh "$TAG" "/tmp/existing" "$GITHUB_REPOSITORY"
      ;;
    draft_publish_existing)
      tag_object="$(gh api "repos/$GITHUB_REPOSITORY/git/ref/tags/$TAG")"
      object_type="$(jq -r '.object.type' <<<"$tag_object")"
      source_commit="$(jq -r '.object.sha' <<<"$tag_object")"
      if [[ "$object_type" == tag ]]; then
        tag_object="$(gh api "repos/$GITHUB_REPOSITORY/git/tags/$source_commit")"
        object_type="$(jq -r '.object.type' <<<"$tag_object")"
        source_commit="$(jq -r '.object.sha' <<<"$tag_object")"
      fi
      [[ "$object_type" == commit ]] || {
        echo "draft recovery requires an existing tag that resolves to a source commit" >&2
        exit 1
      }
      ./scripts/verify_draft_release_assets.sh \
        "$TAG" "/tmp/existing" "$GITHUB_REPOSITORY" "$source_commit"
      ;;
    collision) echo "immutable release collision: $TAG" >&2; exit 1 ;;
    *) echo "unexpected release state: $state" >&2; exit 1 ;;
  esac
else
  response=/tmp/release-view.txt
  if gh api --include "repos/$GITHUB_REPOSITORY/releases/tags/$TAG" >"$response" 2>&1; then
    echo "release lookup disagrees with gh release view" >&2
    exit 1
  fi
  grep -Eq '^HTTP/[0-9.]+ 404' "$response" || { cat "$response" >&2; exit 1; }
fi
echo "state=$state" >> "$GITHUB_OUTPUT"
