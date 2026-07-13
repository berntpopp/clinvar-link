"""Tests for the prebuilt-bundle distribution path (pack / pull / install).

All HTTP is mocked with ``respx`` — these run entirely offline. The bundle is
packed from the committed TSV fixture so the ``.zst`` and its sibling sha256 are
real, then served back through respx to exercise the download/verify/install
flow against actual compressed bytes.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import httpx
import pytest
import respx
import zstandard
from typer.testing import CliRunner

from clinvar_link.config import Settings
from clinvar_link.exceptions import DownloadError
from clinvar_link.ingest import bundle
from clinvar_link.ingest.builder import build_database
from clinvar_link.ingest.bundle import (
    _decompress_bundle_bytes_for_test,
    download_verify_install,
    fetch_sibling_sha256,
    pack_bundle,
    pull_latest,
    resolve_latest_asset,
)

FIXTURE = Path(__file__).parent / "fixtures" / "variant_summary_sample.txt"

# A known VariationID present in the fixture (its canonical row resolves).
KNOWN_VID = 100001

runner = CliRunner()


def _build_fixture_db(tmp_path: Path) -> Path:
    """Build the fixture index and return its path."""
    cfg = Settings(DATA_DIR=tmp_path, DB_FILENAME="clinvar.sqlite")
    build_database(cfg, source_path=FIXTURE, last_modified="Mon, 01 Jan 2026 00:00:00 GMT")
    return cfg.db_path


def _open_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@respx.mock
def test_bundle_rejects_unapproved_intermediate_redirect(tmp_path: Path) -> None:
    asset = "https://github.com/berntpopp/clinvar-link/releases/download/v1/clinvar.sqlite.zst"
    blocked = respx.get("https://evil.example/payload").mock(return_value=httpx.Response(200))
    respx.get(asset).mock(
        return_value=httpx.Response(302, headers={"Location": "https://evil.example/payload"})
    )
    with pytest.raises(DownloadError, match=r"host evil\.example is not allowed"):
        download_verify_install(
            asset,
            db_path=tmp_path / "db.sqlite",
            staging_dir=tmp_path,
            expected_sha256="a" * 64,
            max_compressed_bytes=1024,
            max_expanded_bytes=1024,
        )
    assert blocked.called is False


@respx.mock
def test_checksum_sidecar_requires_sha256_hex() -> None:
    url = "https://github.com/owner/repo/releases/download/v1/db.zst"
    respx.get(f"{url}.sha256").mock(return_value=httpx.Response(200, text="not-a-digest db.zst"))
    with pytest.raises(DownloadError, match="invalid SHA-256"):
        fetch_sibling_sha256(url)


def test_bundle_expansion_limit_preserves_database(tmp_path: Path) -> None:
    db_path = tmp_path / "clinvar.sqlite"
    db_path.write_bytes(b"old-db")
    compressed = zstandard.ZstdCompressor().compress(b"x" * 65)
    with pytest.raises(DownloadError, match="expanded bundle exceeded 64 bytes"):
        _decompress_bundle_bytes_for_test(compressed, db_path, max_expanded_bytes=64)
    assert db_path.read_bytes() == b"old-db"


@respx.mock
def test_invalid_expected_sha256_is_rejected_before_download(tmp_path: Path) -> None:
    asset = "https://github.com/owner/repo/releases/download/v1/db.zst"
    route = respx.get(asset).mock(return_value=httpx.Response(200, content=b"payload"))
    with pytest.raises(DownloadError, match="invalid expected bundle SHA-256"):
        download_verify_install(
            asset,
            db_path=tmp_path / "db.sqlite",
            staging_dir=tmp_path,
            expected_sha256="not-a-digest",
        )
    assert route.called is False


# -- pack_bundle ---------------------------------------------------------------


def test_pack_bundle_roundtrip(tmp_path: Path) -> None:
    db_path = _build_fixture_db(tmp_path / "build")
    out_dir = tmp_path / "dist"

    result = pack_bundle(db_path, out_dir)

    zst_path = Path(result["zst_path"])
    sha_path = Path(result["sha256_path"])
    assert zst_path.exists()
    assert sha_path.exists()
    assert zst_path.name == "clinvar.sqlite.zst"

    # The recorded sha matches the actual .zst bytes and the sibling file.
    actual = hashlib.sha256(zst_path.read_bytes()).hexdigest()
    assert result["sha256"] == actual
    assert result["size_bytes"] == zst_path.stat().st_size
    # Sibling is sha256sum -c compatible: "<digest>  <name>\n".
    sidecar = sha_path.read_text(encoding="utf-8")
    assert sidecar == f"{actual}  {zst_path.name}\n"

    # The release tag is derived from the DB release date.
    assert result["release_tag"].startswith("bundle-")

    # Decompressing the packed .zst yields a valid, queryable DB.
    restored = tmp_path / "restored.sqlite"
    with zst_path.open("rb") as src, restored.open("wb") as dst:
        zstandard.ZstdDecompressor().copy_stream(src, dst)
    conn = _open_ro(restored)
    try:
        row = conn.execute(
            "SELECT variation_id FROM variant WHERE variation_id = ?", (KNOWN_VID,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row["variation_id"] == KNOWN_VID


def test_pack_bundle_missing_db(tmp_path: Path) -> None:
    with pytest.raises((DownloadError, FileNotFoundError, OSError)):
        pack_bundle(tmp_path / "nope.sqlite", tmp_path / "dist")


# -- resolve_latest_asset ------------------------------------------------------


@respx.mock
def test_resolve_latest_asset_picks_named_asset() -> None:
    repo = "owner/repo"
    respx.get(f"https://api.github.com/repos/{repo}/releases?per_page=5&page=1").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "tag_name": "bundle-2026-01-01",
                    "assets": [
                        {
                            "name": "other.txt",
                            "browser_download_url": "https://dl.test/other.txt",
                        },
                        {
                            "name": "clinvar.sqlite.zst",
                            "browser_download_url": "https://dl.test/clinvar.sqlite.zst",
                        },
                    ],
                }
            ],
        )
    )

    url, tag = resolve_latest_asset(repo, asset_name="clinvar.sqlite.zst")
    assert url == "https://dl.test/clinvar.sqlite.zst"
    assert tag == "bundle-2026-01-01"


@respx.mock
def test_resolve_latest_asset_falls_back_to_suffix() -> None:
    repo = "owner/repo"
    respx.get(f"https://api.github.com/repos/{repo}/releases?per_page=5&page=1").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "tag_name": "bundle-x",
                    "assets": [
                        {
                            "name": "snapshot-2026.sqlite.zst",
                            "browser_download_url": "https://dl.test/snapshot.sqlite.zst",
                        },
                    ],
                }
            ],
        )
    )

    url, tag = resolve_latest_asset(repo, asset_name="does-not-match.zst")
    assert url == "https://dl.test/snapshot.sqlite.zst"
    assert tag == "bundle-x"


@respx.mock
def test_resolve_latest_asset_no_asset_raises() -> None:
    repo = "owner/repo"
    respx.get(f"https://api.github.com/repos/{repo}/releases?per_page=5&page=1").mock(
        return_value=httpx.Response(200, json=[])
    )
    with pytest.raises(DownloadError):
        resolve_latest_asset(repo, asset_name="clinvar.sqlite.zst")


@respx.mock
def test_resolve_latest_asset_skips_assetless_code_release() -> None:
    repo = "owner/repo"
    respx.get(f"https://api.github.com/repos/{repo}/releases?per_page=5&page=1").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"tag_name": "v0.2.4", "assets": []},
                {
                    "tag_name": "bundle-2026-07-07",
                    "assets": [
                        {
                            "name": "clinvar.sqlite.zst",
                            "browser_download_url": "https://dl.test/clinvar.sqlite.zst",
                        }
                    ],
                },
            ],
        )
    )

    url, tag = resolve_latest_asset(repo, asset_name="clinvar.sqlite.zst")

    assert url == "https://dl.test/clinvar.sqlite.zst"
    assert tag == "bundle-2026-07-07"


@respx.mock
def test_resolve_latest_asset_checks_later_bounded_page() -> None:
    repo = "owner/repo"
    page_one = [{"tag_name": f"v1.0.{index}", "assets": []} for index in range(5)]
    respx.get(f"https://api.github.com/repos/{repo}/releases?per_page=5&page=1").mock(
        return_value=httpx.Response(200, json=page_one)
    )
    respx.get(f"https://api.github.com/repos/{repo}/releases?per_page=5&page=2").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "tag_name": "bundle-2026-01-01",
                    "assets": [
                        {
                            "name": "clinvar.sqlite.zst",
                            "browser_download_url": "https://dl.test/clinvar.sqlite.zst",
                        }
                    ],
                }
            ],
        )
    )

    url, tag = resolve_latest_asset(repo, asset_name="clinvar.sqlite.zst")

    assert url == "https://dl.test/clinvar.sqlite.zst"
    assert tag == "bundle-2026-01-01"


# -- fetch_sibling_sha256 ------------------------------------------------------


@respx.mock
def test_fetch_sibling_sha256_parses_first_token() -> None:
    url = "https://release-assets.githubusercontent.com/clinvar.sqlite.zst"
    digest = "a" * 64
    respx.get(f"{url}.sha256").mock(
        return_value=httpx.Response(200, text=f"{digest}  clinvar.sqlite.zst\n")
    )
    assert fetch_sibling_sha256(url) == digest


# -- download_verify_install ---------------------------------------------------


def _packed_fixture(tmp_path: Path) -> tuple[bytes, str]:
    """Build + pack the fixture, returning (.zst bytes, true sha256)."""
    db_path = _build_fixture_db(tmp_path / "build")
    result = pack_bundle(db_path, tmp_path / "dist")
    zst_bytes = Path(result["zst_path"]).read_bytes()
    return zst_bytes, result["sha256"]


@respx.mock
def test_download_verify_install_happy(tmp_path: Path) -> None:
    zst_bytes, sha = _packed_fixture(tmp_path)
    asset_url = "https://release-assets.githubusercontent.com/clinvar.sqlite.zst"
    respx.get(asset_url).mock(return_value=httpx.Response(200, content=zst_bytes))

    db_path = tmp_path / "install" / "clinvar.sqlite"
    staging = tmp_path / "staging"
    result = download_verify_install(
        asset_url, db_path=db_path, staging_dir=staging, expected_sha256=sha
    )

    assert Path(result["db_path"]) == db_path
    assert db_path.exists()
    conn = _open_ro(db_path)
    try:
        row = conn.execute(
            "SELECT variation_id FROM variant WHERE variation_id = ?", (KNOWN_VID,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    # The staging .zst is cleaned up on success.
    assert not (staging / "clinvar.sqlite.zst").exists()


@respx.mock
def test_exact_bundle_verifies_expanded_digest_and_schema(tmp_path: Path) -> None:
    zst_bytes, sha = _packed_fixture(tmp_path)
    with zstandard.ZstdDecompressor().stream_reader(zst_bytes) as reader:
        expanded = reader.read()
    file_sha = hashlib.sha256(expanded).hexdigest()
    expanded_sha = hashlib.sha256(
        f"clinvar.sqlite\0{0o444:04o}\0{len(expanded)}\0{file_sha}\n".encode()
    ).hexdigest()
    asset_url = (
        "https://github.com/berntpopp/clinvar-link/releases/download/"
        "bundle-2026-01-01/clinvar.sqlite.zst"
    )
    respx.get(asset_url).mock(return_value=httpx.Response(200, content=zst_bytes))

    result = download_verify_install(
        asset_url,
        db_path=tmp_path / "reference" / "clinvar.sqlite",
        staging_dir=tmp_path / "staging",
        expected_sha256=sha,
        expected_expanded_sha256=expanded_sha,
        expected_schema_version="1.0.0",
    )

    assert result["expanded_sha256"] == expanded_sha
    assert result["schema_version"] == "1.0.0"


@respx.mock
def test_exact_bundle_rejects_wrong_expanded_digest(tmp_path: Path) -> None:
    zst_bytes, sha = _packed_fixture(tmp_path)
    asset_url = "https://release-assets.githubusercontent.com/clinvar.sqlite.zst"
    respx.get(asset_url).mock(return_value=httpx.Response(200, content=zst_bytes))
    with pytest.raises(DownloadError, match="expanded bundle sha256 mismatch"):
        download_verify_install(
            asset_url,
            db_path=tmp_path / "reference" / "clinvar.sqlite",
            staging_dir=tmp_path / "staging",
            expected_sha256=sha,
            expected_expanded_sha256="0" * 64,
            expected_schema_version="1.0.0",
        )


@respx.mock
def test_download_verify_install_bad_sha_leaves_no_db(tmp_path: Path) -> None:
    zst_bytes, _ = _packed_fixture(tmp_path)
    asset_url = "https://release-assets.githubusercontent.com/clinvar.sqlite.zst"
    respx.get(asset_url).mock(return_value=httpx.Response(200, content=zst_bytes))

    db_path = tmp_path / "install" / "clinvar.sqlite"
    staging = tmp_path / "staging"
    with pytest.raises(DownloadError):
        download_verify_install(
            asset_url,
            db_path=db_path,
            staging_dir=staging,
            expected_sha256="deadbeef" * 8,
        )
    # No DB installed and no partial .zst left behind.
    assert not db_path.exists()
    assert not (staging / "clinvar.sqlite.zst").exists()


# -- pull_latest ---------------------------------------------------------------


@respx.mock
def test_pull_latest_happy(tmp_path: Path) -> None:
    zst_bytes, sha = _packed_fixture(tmp_path)
    repo = "owner/repo"
    asset_url = "https://release-assets.githubusercontent.com/clinvar.sqlite.zst"

    respx.get(f"https://api.github.com/repos/{repo}/releases?per_page=5&page=1").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "tag_name": "bundle-2026-01-01",
                    "assets": [{"name": "clinvar.sqlite.zst", "browser_download_url": asset_url}],
                }
            ],
        )
    )
    respx.get(asset_url).mock(return_value=httpx.Response(200, content=zst_bytes))
    respx.get(f"{asset_url}.sha256").mock(
        return_value=httpx.Response(200, text=f"{sha}  clinvar.sqlite.zst\n")
    )

    cfg = Settings(
        DATA_DIR=tmp_path / "install",
        DB_FILENAME="clinvar.sqlite",
        GITHUB_REPO=repo,
        BUNDLE_URL="latest",
        DEVELOPMENT_LATEST=True,
    )
    result = pull_latest(cfg)

    assert result["release_tag"] == "bundle-2026-01-01"
    assert cfg.db_path.exists()
    conn = _open_ro(cfg.db_path)
    try:
        row = conn.execute(
            "SELECT variation_id FROM variant WHERE variation_id = ?", (KNOWN_VID,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None


def test_pull_latest_disabled_raises(tmp_path: Path) -> None:
    cfg = Settings(DATA_DIR=tmp_path, DB_FILENAME="clinvar.sqlite", BUNDLE_URL="")
    with pytest.raises(DownloadError):
        pull_latest(cfg)


# -- CLI pack ------------------------------------------------------------------


def test_cli_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = _build_fixture_db(tmp_path / "build")
    out_dir = tmp_path / "dist"

    import clinvar_link.ingest.cli as cli_mod

    fixture_settings = Settings(DATA_DIR=db_path.parent, DB_FILENAME=db_path.name)
    monkeypatch.setattr(cli_mod, "settings", fixture_settings)

    result = runner.invoke(cli_mod.app, ["pack", "--out-dir", str(out_dir)])
    assert result.exit_code == 0, result.output
    assert "bundle-" in result.output
    assert (out_dir / "clinvar.sqlite.zst").exists()
    assert (out_dir / "clinvar.sqlite.zst.sha256").exists()


# -- CLI publish ---------------------------------------------------------------


def test_cli_publish_no_upload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`publish --no-upload` packs the existing DB and never touches gh/network."""
    db_path = _build_fixture_db(tmp_path / "build")
    out_dir = tmp_path / "dist"

    import clinvar_link.ingest.cli as cli_mod

    fixture_settings = Settings(DATA_DIR=db_path.parent, DB_FILENAME=db_path.name)
    monkeypatch.setattr(cli_mod, "settings", fixture_settings)

    # Guard: a no-upload run must not invoke gh at all.
    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("subprocess.run must not be called for --no-upload")

    monkeypatch.setattr(cli_mod.subprocess, "run", _boom)

    result = runner.invoke(
        cli_mod.app,
        ["publish", "--no-build", "--no-upload", "--out-dir", str(out_dir)],
    )
    assert result.exit_code == 0, result.output
    assert "bundle-" in result.output
    assert "upload skipped" in result.output
    assert (out_dir / "clinvar.sqlite.zst").exists()
    assert (out_dir / "clinvar.sqlite.zst.sha256").exists()


def test_cli_publish_upload_invokes_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`publish --upload` runs a gh release create/upload with tag + both assets."""
    db_path = _build_fixture_db(tmp_path / "build")
    out_dir = tmp_path / "dist"

    import clinvar_link.ingest.cli as cli_mod

    fixture_settings = Settings(DATA_DIR=db_path.parent, DB_FILENAME=db_path.name)
    monkeypatch.setattr(cli_mod, "settings", fixture_settings)

    calls: list[list[str]] = []

    class _FakeCompleted:
        # The release-view existence check expects a non-zero "does not exist".
        returncode = 1

    def _fake_run(args: list[str], *_a: object, **_k: object) -> _FakeCompleted:
        calls.append(list(args))
        return _FakeCompleted()

    monkeypatch.setattr(cli_mod.subprocess, "run", _fake_run)

    result = runner.invoke(
        cli_mod.app,
        ["publish", "--no-build", "--upload", "--out-dir", str(out_dir), "--repo", "owner/repo"],
    )
    assert result.exit_code == 0, result.output
    assert "published bundle-" in result.output

    zst = str(out_dir / "clinvar.sqlite.zst")
    sha = str(out_dir / "clinvar.sqlite.zst.sha256")

    # A `gh release create` (release did not exist) carrying the tag + both assets.
    create = [c for c in calls if c[:3] == ["gh", "release", "create"]]
    assert create, f"no gh release create call recorded: {calls}"
    args = create[0]
    assert any(tok.startswith("bundle-") for tok in args)
    assert zst in args
    assert sha in args
    assert "--repo" in args and "owner/repo" in args


def test_cli_publish_no_build_missing_db_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`publish --no-build` with no index exits 1 with a clear message."""
    import clinvar_link.ingest.cli as cli_mod

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    missing_settings = Settings(DATA_DIR=empty_dir, DB_FILENAME="clinvar.sqlite")
    monkeypatch.setattr(cli_mod, "settings", missing_settings)

    result = runner.invoke(cli_mod.app, ["publish", "--no-build", "--no-upload"])
    assert result.exit_code == 1
    assert "no index at" in result.output


def test_expanded_tree_sha256_streams_and_never_buffers_the_whole_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The expanded ClinVar index is multi-GB: hashing it must not read it into RAM.

    Reading the whole file (``Path.read_bytes()``) made the data-init sidecar exceed
    its container memory limit and get OOM-killed (exit 137) before it could install
    the bundle.
    """
    payload = b"clinvar-bundle-bytes" * 4096
    db = tmp_path / "clinvar.sqlite"
    db.write_bytes(payload)

    def _explode(self: Path, *args: object, **kwargs: object) -> bytes:
        raise AssertionError("the expanded bundle must be hashed as a stream")

    monkeypatch.setattr(Path, "read_bytes", _explode)

    file_sha256 = hashlib.sha256(payload).hexdigest()
    identity = f"clinvar.sqlite\0{0o444:04o}\0{len(payload)}\0{file_sha256}\n"
    expected = hashlib.sha256(identity.encode()).hexdigest()

    assert bundle._expanded_tree_sha256(db, "clinvar.sqlite") == expected
