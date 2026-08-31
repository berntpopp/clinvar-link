"""Tests for immutable ClinVar bundle release identity."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from clinvar_link.config import Settings
from clinvar_link.ingest.builder import build_database
from clinvar_link.ingest.release_metadata import (
    ReleaseIdentityError,
    ReleaseState,
    build_release_metadata,
    decide_release_state,
)

FIXTURE = Path(__file__).parent / "fixtures" / "variant_summary_sample.txt"


def _database_with_source_identity(tmp_path: Path, *, etag: str | None = '"clinvar-etag"') -> Path:
    config = Settings(DATA_DIR=tmp_path, DB_FILENAME="clinvar.sqlite")
    database = build_database(
        config,
        source_path=FIXTURE,
        last_modified="Sun, 23 Aug 2026 00:00:00 GMT",
        etag=etag,
        source_sha256="a" * 64,
        source_retrieved_at="2026-08-24T01:02:03Z",
    )["db_path"]
    db_path = Path(database)
    return db_path


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Sun, 23 Aug 2026 00:00:00 GMT", "bundle-2026-08-23"),
        ("2026-08-23", "bundle-2026-08-23"),
        ("2026-08-23T00:00:00+00:00", "bundle-2026-08-23"),
    ],
)
def test_release_tag_for_date_normalizes_strict_source_dates(value: str, expected: str) -> None:
    """RFC-1123 and ISO source dates select the same immutable tag."""
    from clinvar_link.ingest.bundle import release_tag_for_date

    assert release_tag_for_date(value) == expected


@pytest.mark.parametrize("value", [None, "", "not a publication date", "2026-99-99", 1, True])
def test_release_tag_for_date_rejects_absent_or_malformed_identity(value: object) -> None:
    """A missing source identity may never become a ``bundle-unknown`` release."""
    from clinvar_link.ingest.bundle import release_tag_for_date

    with pytest.raises(ReleaseIdentityError):
        release_tag_for_date(value)


def test_release_metadata_rejects_a_missing_schema_version(tmp_path: Path) -> None:
    """A corrupt database cannot define a fresh immutable release."""
    db_path = _database_with_source_identity(tmp_path)
    asset_path = tmp_path / "clinvar.sqlite.zst"
    asset_path.write_bytes(b"compressed ClinVar fixture")
    import sqlite3

    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE meta SET schema_version = NULL WHERE id = 1")

    with pytest.raises(ReleaseIdentityError, match="schema version"):
        build_release_metadata(
            db_path,
            asset_path,
            tmp_path / "dist",
            retrieved_at=datetime(2026, 8, 24, 1, 2, 3, tzinfo=UTC),
        )


def test_build_release_metadata_streams_hashes_and_records_source_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Metadata preserves provenance without whole-file reads or timestamp relabeling."""
    db_path = _database_with_source_identity(tmp_path)
    asset_path = tmp_path / "clinvar.sqlite.zst"
    asset_path.write_bytes(b"compressed ClinVar fixture")

    def no_whole_file_reads(_: Path) -> bytes:
        raise AssertionError("release metadata must stream large files")

    monkeypatch.setattr(Path, "read_bytes", no_whole_file_reads)
    metadata = build_release_metadata(
        db_path,
        asset_path,
        tmp_path / "dist",
        retrieved_at=datetime(2026, 8, 24, 1, 2, 3, tzinfo=UTC),
    )

    payload = json.loads((tmp_path / "dist" / "bundle-metadata.json").read_text())
    assert metadata.tag == "bundle-2026-08-23"
    assert payload["clinvar_release_date_raw"] == "Sun, 23 Aug 2026 00:00:00 GMT"
    assert payload["clinvar_release_date"] == "2026-08-23"
    assert payload["source_url"].endswith("variant_summary.txt.gz")
    assert payload["source_etag"] == '"clinvar-etag"'
    assert payload["source_sha256"] == "a" * 64
    assert payload["source_retrieved_at"] == "2026-08-24T01:02:03Z"
    assert payload["retrieved_at"] == "2026-08-24T01:02:03Z"
    assert payload["asset_sha256"]
    assert payload["expanded_tree_sha256"]


def test_release_metadata_records_an_absent_source_etag_without_fabricating_one(
    tmp_path: Path,
) -> None:
    """NCBI currently supplies Last-Modified but no ETag; preserve that absence exactly."""
    db_path = _database_with_source_identity(tmp_path, etag=None)
    asset_path = tmp_path / "clinvar.sqlite.zst"
    asset_path.write_bytes(b"compressed ClinVar fixture")

    build_release_metadata(
        db_path,
        asset_path,
        tmp_path / "dist",
        retrieved_at=datetime(2026, 8, 24, 1, 2, 3, tzinfo=UTC),
    )

    payload = json.loads((tmp_path / "dist" / "bundle-metadata.json").read_text())
    assert "source_etag" in payload
    assert payload["source_etag"] is None
    assert payload["source_last_modified"] == "Sun, 23 Aug 2026 00:00:00 GMT"


def test_release_identity_distinguishes_absent_and_present_source_etag(tmp_path: Path) -> None:
    """A server-validator change cannot silently reuse an immutable release tag."""
    db_path = _database_with_source_identity(tmp_path, etag=None)
    asset_path = tmp_path / "clinvar.sqlite.zst"
    asset_path.write_bytes(b"compressed ClinVar fixture")
    build_release_metadata(
        db_path,
        asset_path,
        tmp_path / "current",
        retrieved_at=datetime(2026, 8, 24, 1, 2, 3, tzinfo=UTC),
    )
    current_path = tmp_path / "current" / "bundle-metadata.json"
    existing = json.loads(current_path.read_text())
    existing["source_etag"] = '"later-etag"'
    existing_path = tmp_path / "existing.json"
    existing_path.write_text(json.dumps(existing), encoding="utf-8")

    assert (
        decide_release_state(current_path, existing_path, is_draft=False) is ReleaseState.COLLISION
    )


def test_release_states_fail_closed_for_any_existing_identity_mismatch(tmp_path: Path) -> None:
    """Only identical releases may no-op or publish an already sealed draft."""
    db_path = _database_with_source_identity(tmp_path)
    asset_path = tmp_path / "clinvar.sqlite.zst"
    asset_path.write_bytes(b"compressed ClinVar fixture")
    build_release_metadata(
        db_path,
        asset_path,
        tmp_path / "current",
        retrieved_at=datetime(2026, 8, 24, 1, 2, 3, tzinfo=UTC),
    )
    current_path = tmp_path / "current" / "bundle-metadata.json"

    assert decide_release_state(current_path, None, is_draft=False) is ReleaseState.CREATE
    assert (
        decide_release_state(current_path, current_path, is_draft=False)
        is ReleaseState.PUBLISHED_NOOP
    )
    assert (
        decide_release_state(current_path, current_path, is_draft=True)
        is ReleaseState.DRAFT_PUBLISH_EXISTING
    )

    mismatching = json.loads(current_path.read_text())
    mismatching["source_sha256"] = "b" * 64
    existing_path = tmp_path / "existing.json"
    existing_path.write_text(json.dumps(mismatching), encoding="utf-8")
    assert (
        decide_release_state(current_path, existing_path, is_draft=False) is ReleaseState.COLLISION
    )
    assert (
        decide_release_state(current_path, existing_path, is_draft=True) is ReleaseState.COLLISION
    )

    typed_mismatch = json.loads(current_path.read_text())
    typed_mismatch["variant_count"] = float(typed_mismatch["variant_count"])
    typed_path = tmp_path / "typed-mismatch.json"
    typed_path.write_text(json.dumps(typed_mismatch), encoding="utf-8")
    with pytest.raises(ReleaseIdentityError, match="variant_count"):
        decide_release_state(current_path, typed_path, is_draft=False)
