"""Executable contract tests for checksum and provenance verification of release assets."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts" / "verify_release_assets.sh"
DRAFT_VERIFIER = ROOT / "scripts" / "verify_draft_release_assets.sh"


def _run_script(script: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed in-repository verifier scripts
        [str(script), *args], check=False, capture_output=True, text=True, env=env
    )


def _write_asset(directory: Path, name: str, content: bytes) -> str:
    (directory / name).write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def _write_asset_set(directory: Path) -> list[tuple[str, str]]:
    hashes = [
        (
            _write_asset(directory, "bundle-metadata.json", b'{"tag":"bundle-test"}\n'),
            "bundle-metadata.json",
        ),
        (
            _write_asset(directory, "clinvar.sqlite.zst", b"published snapshot"),
            "clinvar.sqlite.zst",
        ),
        (
            _write_asset(directory, "clinvar.sqlite.zst.sha256", b"snapshot checksum\n"),
            "clinvar.sqlite.zst.sha256",
        ),
    ]
    (directory / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for digest, name in hashes), encoding="utf-8"
    )
    return hashes


def test_release_asset_verifier_checks_all_bytes_before_calling_github(
    tmp_path: Path,
) -> None:
    """A tampered release asset fails checksum validation before attestation lookup."""
    assets = tmp_path / "assets with spaces"
    assets.mkdir()
    _write_asset_set(assets)

    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    calls_file = tmp_path / "gh-calls.txt"
    fake_gh = binary_dir / "gh"
    fake_gh.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$GH_CALL_LOG"\n'
        'test -z "${GH_FAIL_ATTESTATION:-}" || exit 1\n',
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    environment = os.environ | {
        "PATH": f"{binary_dir}:{os.environ['PATH']}",
        "GH_CALL_LOG": str(calls_file),
    }

    verified = _run_script(VERIFIER, "bundle-test", str(assets), "owner/repo", env=environment)
    assert verified.returncode == 0, verified.stderr
    assert calls_file.read_text(encoding="utf-8").splitlines() == [
        f"release verify-asset bundle-test {assets / name} --repo owner/repo"
        for name in (
            "SHA256SUMS",
            "bundle-metadata.json",
            "clinvar.sqlite.zst",
            "clinvar.sqlite.zst.sha256",
        )
    ]

    (assets / "bundle-metadata.json").write_bytes(b"tampered metadata")
    rejected = _run_script(VERIFIER, "bundle-test", str(assets), "owner/repo", env=environment)
    assert rejected.returncode != 0
    assert calls_file.read_text(encoding="utf-8").splitlines() == [
        f"release verify-asset bundle-test {assets / name} --repo owner/repo"
        for name in (
            "SHA256SUMS",
            "bundle-metadata.json",
            "clinvar.sqlite.zst",
            "clinvar.sqlite.zst.sha256",
        )
    ]


def test_draft_verifier_requires_exact_source_workflow_and_ref(tmp_path: Path) -> None:
    """A resumable draft must prove its existing bytes came from the pinned source run."""
    assets = tmp_path / "assets"
    assets.mkdir()
    _write_asset_set(assets)
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    calls_file = tmp_path / "gh-calls.txt"
    fake_gh = binary_dir / "gh"
    fake_gh.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$GH_CALL_LOG"\n'
        'test -z "${GH_FAIL_ATTESTATION:-}" || exit 1\n',
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    environment = os.environ | {
        "PATH": f"{binary_dir}:{os.environ['PATH']}",
        "GH_CALL_LOG": str(calls_file),
    }
    source_sha = "5aa176a5c42d89e2898bd11780be6f9ea6e7310b"

    verified = _run_script(
        DRAFT_VERIFIER, "bundle-test", str(assets), "owner/repo", source_sha, env=environment
    )
    assert verified.returncode == 0, verified.stderr
    assert calls_file.read_text(encoding="utf-8").splitlines() == [
        "attestation verify "
        f"{assets / name} --repo owner/repo "
        "--signer-workflow owner/repo/.github/workflows/data-bundle.yml "
        "--source-ref refs/heads/main "
        f"--source-digest {source_sha} --predicate-type https://slsa.dev/provenance/v1"
        for name in (
            "SHA256SUMS",
            "bundle-metadata.json",
            "clinvar.sqlite.zst",
            "clinvar.sqlite.zst.sha256",
        )
    ]

    (assets / "bundle-metadata.json").write_bytes(b"tampered metadata")
    rejected_bytes = _run_script(
        DRAFT_VERIFIER, "bundle-test", str(assets), "owner/repo", source_sha, env=environment
    )
    assert rejected_bytes.returncode != 0
    assert "FAILED" in rejected_bytes.stdout
    assert calls_file.read_text(encoding="utf-8").splitlines() == [
        "attestation verify "
        f"{assets / name} --repo owner/repo "
        "--signer-workflow owner/repo/.github/workflows/data-bundle.yml "
        "--source-ref refs/heads/main "
        f"--source-digest {source_sha} --predicate-type https://slsa.dev/provenance/v1"
        for name in (
            "SHA256SUMS",
            "bundle-metadata.json",
            "clinvar.sqlite.zst",
            "clinvar.sqlite.zst.sha256",
        )
    ]

    (assets / "bundle-metadata.json").write_bytes(b'{"tag":"bundle-test"}\n')
    rejected = _run_script(
        DRAFT_VERIFIER,
        "bundle-test",
        str(assets),
        "owner/repo",
        "not-a-source-sha",
        env=environment,
    )
    assert rejected.returncode != 0
    assert "full trusted source commit SHA" in rejected.stderr

    failed_provenance = _run_script(
        DRAFT_VERIFIER,
        "bundle-test",
        str(assets),
        "owner/repo",
        source_sha,
        env=environment | {"GH_FAIL_ATTESTATION": "1"},
    )
    assert failed_provenance.returncode != 0
    assert "lacks build provenance" in failed_provenance.stderr


@pytest.mark.parametrize(
    "entries",
    [
        "omit",  # one required asset is not covered by the inventory
        "duplicate",
        "unknown",
        "path-traversal",
        "malformed",
        "unterminated-malformed",
    ],
)
def test_checksum_inventory_must_be_exact_before_any_github_call(
    tmp_path: Path, entries: str
) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    hashes = _write_asset_set(assets)
    lines = [f"{digest}  {name}" for digest, name in hashes]
    if entries == "omit":
        lines.pop()
    elif entries == "duplicate":
        lines.append(lines[0])
    elif entries == "unknown":
        lines.append(f"{'0' * 64}  unexpected.bin")
    elif entries == "path-traversal":
        lines.append(f"{'0' * 64}  ../outside")
    elif entries == "malformed":
        lines.append("bad checksum entry")
    checksum_text = "\n".join(lines)
    if entries != "unterminated-malformed":
        checksum_text += "\n"
    else:
        checksum_text += "\nmalformed final entry"
    (assets / "SHA256SUMS").write_text(checksum_text, encoding="utf-8")

    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    calls_file = tmp_path / "gh-calls.txt"
    fake_gh = binary_dir / "gh"
    fake_gh.write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$GH_CALL_LOG"\n', encoding="utf-8")
    fake_gh.chmod(0o755)
    environment = os.environ | {
        "PATH": f"{binary_dir}:{os.environ['PATH']}",
        "GH_CALL_LOG": str(calls_file),
    }

    result = _run_script(VERIFIER, "bundle-test", str(assets), "owner/repo", env=environment)
    assert result.returncode != 0
    assert not calls_file.exists()
