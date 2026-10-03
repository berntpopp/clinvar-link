"""The GeneFoundry runtime-v1 identity must be provable from the materialized bytes.

The digest published on ``/health`` is only evidence if it is a pure function of what is
actually on the data volume. These tests pin that: the same bytes always hash to the same
digest, and every way the volume can drift from its sealed manifest — an edited file, a
truncated file, a removed file, an extra file, a symlink — fails closed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clinvar_link.runtime_data_identity import (
    MANIFEST_NAME,
    RuntimeDataIdentityError,
    build_identity_manifest,
    verify_runtime_identity,
    write_identity_manifest,
)

RELEASE_TAG = "bundle-2026-09-29"


@pytest.fixture
def sealed_root(tmp_path: Path) -> Path:
    """A materialized data root whose identity manifest has already been written."""
    root = tmp_path / "463a"
    root.mkdir()
    (root / "clinvar.sqlite").write_bytes(b"sqlite-bytes")
    (root / "data-identity.json").write_text('{"release_tag":"bundle-2026-09-29"}\n')
    write_identity_manifest(
        root, RELEASE_TAG, [root / "clinvar.sqlite", root / "data-identity.json"]
    )
    return root


def test_sealed_root_verifies_to_a_stable_identity(sealed_root: Path) -> None:
    identity = verify_runtime_identity(sealed_root)
    assert identity["release_tag"] == RELEASE_TAG
    assert identity["digest"].startswith("sha256:")
    assert len(identity["digest"]) == len("sha256:") + 64
    assert verify_runtime_identity(sealed_root) == identity


def test_identical_bytes_in_a_different_directory_yield_the_same_digest(
    sealed_root: Path, tmp_path: Path
) -> None:
    """The digest names the data, not where it happens to be materialized."""
    other = tmp_path / "elsewhere"
    other.mkdir()
    for name in ("clinvar.sqlite", "data-identity.json"):
        (other / name).write_bytes((sealed_root / name).read_bytes())
    write_identity_manifest(
        other, RELEASE_TAG, [other / "clinvar.sqlite", other / "data-identity.json"]
    )
    assert verify_runtime_identity(other) == verify_runtime_identity(sealed_root)


def test_a_different_release_tag_changes_the_digest(sealed_root: Path, tmp_path: Path) -> None:
    other = tmp_path / "other-tag"
    other.mkdir()
    for name in ("clinvar.sqlite", "data-identity.json"):
        (other / name).write_bytes((sealed_root / name).read_bytes())
    write_identity_manifest(
        other, "bundle-2026-09-07", [other / "clinvar.sqlite", other / "data-identity.json"]
    )
    assert (
        verify_runtime_identity(other)["digest"] != verify_runtime_identity(sealed_root)["digest"]
    )


def test_edited_data_fails_closed(sealed_root: Path) -> None:
    target = sealed_root / "clinvar.sqlite"
    target.chmod(0o644)
    target.write_bytes(b"tampered----")  # same length, different bytes
    with pytest.raises(RuntimeDataIdentityError, match="sha256 mismatch"):
        verify_runtime_identity(sealed_root)


def test_truncated_data_fails_closed(sealed_root: Path) -> None:
    target = sealed_root / "clinvar.sqlite"
    target.chmod(0o644)
    target.write_bytes(b"short")
    with pytest.raises(RuntimeDataIdentityError, match="size_bytes mismatch"):
        verify_runtime_identity(sealed_root)


def test_missing_input_fails_closed(sealed_root: Path) -> None:
    (sealed_root / "data-identity.json").unlink()
    with pytest.raises(RuntimeDataIdentityError, match="input file is missing"):
        verify_runtime_identity(sealed_root)


def test_unexpected_extra_file_fails_closed(sealed_root: Path) -> None:
    (sealed_root / "clinvar.sqlite-wal").write_bytes(b"a write happened")
    with pytest.raises(RuntimeDataIdentityError, match="unexpected regular file"):
        verify_runtime_identity(sealed_root)


def test_symlinked_input_fails_closed(sealed_root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.sqlite"
    outside.write_bytes(b"sqlite-bytes")
    target = sealed_root / "clinvar.sqlite"
    target.chmod(0o644)
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(RuntimeDataIdentityError, match="symlink"):
        verify_runtime_identity(sealed_root)


def test_missing_manifest_fails_closed(sealed_root: Path) -> None:
    (sealed_root / MANIFEST_NAME).unlink()
    with pytest.raises(RuntimeDataIdentityError, match="identity manifest is missing"):
        verify_runtime_identity(sealed_root)


def test_manifest_rejects_a_mutable_release_tag(sealed_root: Path) -> None:
    with pytest.raises(RuntimeDataIdentityError, match="immutable"):
        build_identity_manifest(sealed_root, "latest", [sealed_root / "clinvar.sqlite"])


def test_manifest_is_canonical_and_sorted(sealed_root: Path) -> None:
    manifest = json.loads((sealed_root / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert set(manifest) == {"schema_version", "release_tag", "inputs"}
    assert manifest["schema_version"] == 1
    paths = [entry["path"] for entry in manifest["inputs"]]
    assert paths == sorted(paths)
    assert MANIFEST_NAME not in paths
