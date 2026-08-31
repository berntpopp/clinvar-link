"""Build and compare bounded immutable ClinVar bundle release metadata."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from clinvar_link.exceptions import ReleaseIdentityError
from clinvar_link.ingest.bundle import _expanded_tree_sha256, _sha256_file, release_tag_for_date

_MAX_METADATA_BYTES = 1 << 20
_ASSET_NAME = "clinvar.sqlite.zst"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_STABLE_FIELDS = (
    "tag",
    "asset_sha256",
    "asset_size",
    "expanded_tree_sha256",
    "expanded_size",
    "schema_version",
    "source_sha256",
    "source_url",
    "source_etag",
    "source_last_modified",
    "clinvar_release_date_raw",
    "clinvar_release_date",
    "variant_count",
    "gene_count",
)
_INTEGER_STABLE_FIELDS = frozenset({"asset_size", "expanded_size", "variant_count", "gene_count"})


class ReleaseState(StrEnum):
    """Exclusive release mutation state selected from immutable identities."""

    CREATE = "create"
    PUBLISHED_NOOP = "published_noop"
    DRAFT_PUBLISH_EXISTING = "draft_publish_existing"
    COLLISION = "collision"


@dataclass(frozen=True)
class ReleaseMetadata:
    """Written bundle metadata and its immutable tag."""

    tag: str
    path: Path


def _utc_z(value: datetime) -> str:
    if value.tzinfo is None:
        raise ReleaseIdentityError("source retrieval time must include a timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _metadata_row(db_path: Path) -> sqlite3.Row:
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute("SELECT * FROM meta WHERE id = 1").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise ReleaseIdentityError(f"cannot read release metadata from {db_path}") from exc
    if row is None:
        raise ReleaseIdentityError("ClinVar database has no source metadata")
    required = {"source_retrieved_at", "source_url", "source_sha256", "clinvar_release_date"}
    if not required.issubset(row.keys()):
        raise ReleaseIdentityError("ClinVar database lacks complete source provenance")
    return cast(sqlite3.Row, row)


def _source_retrieved_at(row: sqlite3.Row) -> str:
    value = row["source_retrieved_at"]
    if not isinstance(value, str) or not value:
        raise ReleaseIdentityError("ClinVar source retrieval time is required")
    try:
        return _utc_z(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as exc:
        raise ReleaseIdentityError("ClinVar source retrieval time is malformed") from exc


def _required_text(row: sqlite3.Row, field: str) -> str:
    value = row[field]
    if not isinstance(value, str) or not value:
        raise ReleaseIdentityError(f"ClinVar {field.replace('_', ' ')} is required")
    return value


def _optional_text(row: sqlite3.Row, field: str) -> str | None:
    value = row[field]
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ReleaseIdentityError(f"ClinVar {field.replace('_', ' ')} is malformed")
    return value


def _required_nonnegative_int(row: sqlite3.Row, field: str) -> int:
    value = row[field]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReleaseIdentityError(f"ClinVar {field.replace('_', ' ')} is malformed")
    return value


def _write_checksums(out_dir: Path) -> None:
    checksum_path = out_dir / "SHA256SUMS"
    entries = [path for path in out_dir.iterdir() if path.is_file() and path != checksum_path]
    checksum_path.write_text(
        "".join(f"{_sha256_file(path)}  {path.name}\n" for path in sorted(entries)),
        encoding="utf-8",
    )


def build_release_metadata(
    db_path: Path,
    asset_path: Path,
    out_dir: Path,
    *,
    retrieved_at: datetime,
) -> ReleaseMetadata:
    """Write exact source and artifact provenance using bounded streaming hashes."""
    if not asset_path.is_file():
        raise ReleaseIdentityError(f"bundle asset does not exist: {asset_path}")
    row = _metadata_row(db_path)
    source_retrieved_at = _source_retrieved_at(row)
    if _utc_z(retrieved_at) != source_retrieved_at:
        raise ReleaseIdentityError(
            "provided source retrieval time differs from database provenance"
        )
    raw_date = _required_text(row, "clinvar_release_date")
    source_sha256 = _required_text(row, "source_sha256")
    if _SHA256_RE.fullmatch(source_sha256) is None:
        raise ReleaseIdentityError("ClinVar source SHA-256 is malformed")
    tag = release_tag_for_date(raw_date)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "tag": tag,
        "asset_sha256": _sha256_file(asset_path),
        "asset_size": asset_path.stat().st_size,
        "expanded_tree_sha256": _expanded_tree_sha256(db_path, db_path.name),
        "expanded_size": db_path.stat().st_size,
        "schema_version": f"{_required_nonnegative_int(row, 'schema_version')}.0.0",
        "source_sha256": source_sha256,
        "source_url": _required_text(row, "source_url"),
        "source_etag": _optional_text(row, "source_etag"),
        "source_last_modified": _required_text(row, "source_last_modified"),
        "clinvar_release_date_raw": raw_date,
        "clinvar_release_date": tag.removeprefix("bundle-"),
        "source_retrieved_at": source_retrieved_at,
        "retrieved_at": source_retrieved_at,
        "variant_count": _required_nonnegative_int(row, "variant_count"),
        "gene_count": _required_nonnegative_int(row, "gene_count"),
    }
    path = out_dir / "bundle-metadata.json"
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    _write_checksums(out_dir)
    return ReleaseMetadata(tag=tag, path=path)


def _read_metadata(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as source:
            data = source.read(_MAX_METADATA_BYTES + 1)
    except OSError as exc:
        raise ReleaseIdentityError(f"cannot read release metadata: {path}") from exc
    if len(data) > _MAX_METADATA_BYTES:
        raise ReleaseIdentityError("release metadata exceeds 1 MiB")
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ReleaseIdentityError("release metadata is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ReleaseIdentityError("release metadata must be a JSON object")
    for field in _STABLE_FIELDS:
        if field not in payload:
            raise ReleaseIdentityError(f"release metadata lacks required field: {field}")
        value = payload[field]
        if field == "source_etag":
            if value is not None and (not isinstance(value, str) or not value):
                raise ReleaseIdentityError(f"release metadata has malformed {field}")
            continue
        if field in _INTEGER_STABLE_FIELDS:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ReleaseIdentityError(f"release metadata has malformed {field}")
        elif not isinstance(value, str) or not value:
            raise ReleaseIdentityError(f"release metadata has malformed {field}")
    return payload


def decide_release_state(
    current_path: Path, existing_path: Path | None, *, is_draft: bool
) -> ReleaseState:
    """Select an immutable release state; any identity difference is a collision."""
    current = _read_metadata(current_path)
    if existing_path is None:
        return ReleaseState.CREATE
    existing = _read_metadata(existing_path)
    if any(current[field] != existing[field] for field in _STABLE_FIELDS):
        return ReleaseState.COLLISION
    return ReleaseState.DRAFT_PUBLISH_EXISTING if is_draft else ReleaseState.PUBLISHED_NOOP


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--db-path", type=Path, required=True)
    build.add_argument("--asset-path", type=Path, required=True)
    build.add_argument("--out-dir", type=Path, required=True)
    state = commands.add_parser("state")
    state.add_argument("--current", type=Path, required=True)
    state.add_argument("--existing", type=Path)
    state.add_argument("--draft", action="store_true")
    return parser


def main() -> None:
    """Run the workflow-facing metadata or identity-state command."""
    args = _parser().parse_args()
    if args.command == "build":
        row = _metadata_row(args.db_path)
        source_time = datetime.fromisoformat(_source_retrieved_at(row).replace("Z", "+00:00"))
        metadata = build_release_metadata(
            args.db_path, args.asset_path, args.out_dir, retrieved_at=source_time
        )
        print(metadata.tag)
        return
    print(decide_release_state(args.current, args.existing, is_draft=args.draft).value)


if __name__ == "__main__":
    main()
