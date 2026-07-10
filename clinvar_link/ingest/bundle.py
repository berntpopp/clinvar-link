"""Prebuilt-bundle distribution for the ClinVar SQLite index.

Building the index from NCBI's multi-hundred-MB ``variant_summary`` dump is slow
and memory-hungry. To let clients start fast, CI builds the index once, packs it
as a zstd-compressed ``clinvar.sqlite.zst`` snapshot, and publishes it (with a
sibling ``.sha256``) to GitHub Releases. Clients then download, verify, and
atomically install that snapshot instead of building locally.

This module is the client/CI half of that flow. Everything is SYNC ``httpx``
(mirroring :mod:`clinvar_link.ingest.downloader`): :func:`resolve_latest_asset`
discovers the newest release asset, :func:`fetch_sibling_sha256` reads its
integrity digest, :func:`download_verify_install` streams + verifies + installs
it, and :func:`pull_latest` orchestrates the three against a :class:`Settings`.
:func:`pack_bundle` is the CI producer side.

Compression streams in chunks via ``zstandard``'s ``copy_stream`` so memory
stays flat regardless of database size, and the final install is an atomic
:func:`os.replace` so readers never observe a half-written database.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import zstandard

from clinvar_link.exceptions import DownloadError
from clinvar_link.ingest.download_security import (
    DownloadPolicy,
    copy_bounded,
    open_validated_stream,
    stream_atomic,
)

if TYPE_CHECKING:
    from clinvar_link.config import Settings

_CHUNK_SIZE = 1 << 16
_TIMEOUT = httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0)
_USER_AGENT = "clinvar-link/ingest (+https://github.com/berntpopp/clinvar-link)"
_GITHUB_API = "https://api.github.com"
_ZST_NAME = "clinvar.sqlite.zst"
_BUNDLE_SUFFIX = ".sqlite.zst"
_DATE_PREFIX_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_BUNDLE_HOSTS = frozenset({"github.com", "release-assets.githubusercontent.com"})


def _read_limited_response(
    response: httpx.Response,
    *,
    max_bytes: int,
    max_seconds: float | None,
) -> bytes:
    raw_length = response.headers.get("Content-Length")
    try:
        content_length = int(raw_length) if raw_length is not None else None
    except ValueError:
        content_length = None
    if content_length is not None and content_length > max_bytes:
        raise DownloadError(f"metadata Content-Length {content_length} exceeds {max_bytes} bytes")
    started = time.monotonic()
    body = bytearray()
    for chunk in response.iter_bytes(_CHUNK_SIZE):
        body.extend(chunk)
        if len(body) > max_bytes:
            raise DownloadError(f"metadata exceeded {max_bytes} bytes")
        if max_seconds is not None and time.monotonic() - started > max_seconds:
            raise DownloadError(f"metadata download exceeded {max_seconds:g} seconds")
    return bytes(body)


def _read_release_date(db_path: Path) -> str | None:
    """Read ``meta.clinvar_release_date`` from a built DB (read-only), or None."""
    if not db_path.exists():
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT clinvar_release_date FROM meta WHERE id = 1").fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _release_tag(release_date: str | None) -> str:
    """Derive a ``bundle-<YYYY-MM-DD>`` tag from a release date string.

    Accepts either an ISO-ish date (a leading ``YYYY-MM-DD`` is extracted) or an
    RFC 2822 / HTTP ``Last-Modified`` string; falls back to ``bundle-unknown``.
    """
    if release_date:
        match = _DATE_PREFIX_RE.search(release_date)
        if match:
            return f"bundle-{match.group(1)}"
        try:
            parsed = parsedate_to_datetime(release_date)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, datetime):
            return f"bundle-{parsed.date().isoformat()}"
    return "bundle-unknown"


def pack_bundle(db_path: Path, out_dir: Path, *, level: int = 19) -> dict[str, Any]:
    """Compress ``db_path`` into ``out_dir/clinvar.sqlite.zst`` with a sha256 sidecar.

    Streams the SQLite file through a zstd compressor in chunks, computes the
    SHA-256 of the resulting ``.zst``, and writes a ``sha256sum -c`` compatible
    sibling ``clinvar.sqlite.zst.sha256``. The DB's ``meta.clinvar_release_date``
    is read to derive a suggested release tag (``bundle-<YYYY-MM-DD>``).

    Returns a dict: ``zst_path``, ``sha256_path``, ``sha256``, ``size_bytes``,
    ``release_tag``, ``release_date``.
    """
    if not db_path.exists():
        raise DownloadError(f"cannot pack bundle: database not found at {db_path}")

    out_dir.mkdir(parents=True, exist_ok=True)
    zst_path = out_dir / _ZST_NAME
    sha256_path = out_dir / f"{_ZST_NAME}.sha256"

    release_date = _read_release_date(db_path)

    # threads=-1 uses all logical CPUs — multi-GB SQLite packs in a fraction of
    # the single-threaded time with negligible ratio loss.
    compressor = zstandard.ZstdCompressor(level=level, threads=-1)
    try:
        with db_path.open("rb") as src, zst_path.open("wb") as dst:
            compressor.copy_stream(src, dst, read_size=_CHUNK_SIZE, write_size=_CHUNK_SIZE)
    except OSError as exc:
        zst_path.unlink(missing_ok=True)
        raise DownloadError(f"failed to pack bundle from {db_path}: {exc}") from exc

    digest = hashlib.sha256()
    with zst_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    sha256 = digest.hexdigest()
    sha256_path.write_text(f"{sha256}  {zst_path.name}\n", encoding="utf-8")

    return {
        "zst_path": str(zst_path),
        "sha256_path": str(sha256_path),
        "sha256": sha256,
        "size_bytes": zst_path.stat().st_size,
        "release_tag": _release_tag(release_date),
        "release_date": release_date,
    }


def _github_headers() -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "User-Agent": _USER_AGENT,
    }


def resolve_latest_asset(
    repo: str,
    *,
    asset_name: str,
    max_bytes: int = 1 << 20,
    max_seconds: float | None = None,
) -> tuple[str, str]:
    """Resolve the newest release's bundle asset for ``repo``.

    Returns ``(browser_download_url, tag_name)`` for the asset whose name equals
    ``asset_name``; otherwise the first asset ending in ``.sqlite.zst``. Raises
    :class:`DownloadError` when the API call fails or no bundle asset exists.
    """
    url = f"{_GITHUB_API}/repos/{repo}/releases/latest"
    try:
        with httpx.Client(follow_redirects=False, timeout=_TIMEOUT) as client:
            with client.stream(
                "GET", url, headers=_github_headers(), follow_redirects=False
            ) as response:
                response.raise_for_status()
                payload = json.loads(
                    _read_limited_response(
                        response,
                        max_bytes=max_bytes,
                        max_seconds=max_seconds,
                    )
                )
    except httpx.HTTPStatusError as exc:
        raise DownloadError(
            f"GET {url} failed: HTTP {exc.response.status_code}",
            status_code=exc.response.status_code,
        ) from exc
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise DownloadError(f"GET {url} failed: {exc}") from exc

    tag = str(payload.get("tag_name", "")) if isinstance(payload, dict) else ""
    assets = payload.get("assets", []) if isinstance(payload, dict) else []

    exact: str | None = None
    fallback: str | None = None
    for asset in assets:
        name = str(asset.get("name", ""))
        download_url = asset.get("browser_download_url")
        if not download_url:
            continue
        if name == asset_name:
            exact = str(download_url)
            break
        if fallback is None and name.endswith(_BUNDLE_SUFFIX):
            fallback = str(download_url)

    selected = exact or fallback
    if selected is None:
        raise DownloadError(
            f"no bundle asset (name '{asset_name}' or '*{_BUNDLE_SUFFIX}') "
            f"in the latest release of {repo}"
        )
    return selected, tag


def fetch_sibling_sha256(
    url: str,
    *,
    max_bytes: int = 1 << 20,
    max_seconds: float | None = None,
) -> str:
    """Fetch ``{url}.sha256`` and return its leading whitespace-delimited digest."""
    sha_url = f"{url}.sha256"
    try:
        policy = DownloadPolicy(
            allowed_hosts=_BUNDLE_HOSTS,
            max_bytes=max_bytes,
            max_seconds=max_seconds,
        )
        with httpx.Client(follow_redirects=False, timeout=_TIMEOUT) as client:
            with open_validated_stream(
                client,
                sha_url,
                headers={"User-Agent": _USER_AGENT},
                policy=policy,
            ) as response:
                response.raise_for_status()
                text = _read_limited_response(
                    response,
                    max_bytes=max_bytes,
                    max_seconds=max_seconds,
                ).decode("utf-8", errors="replace")
    except httpx.HTTPStatusError as exc:
        raise DownloadError(
            f"GET {sha_url} failed: HTTP {exc.response.status_code}",
            status_code=exc.response.status_code,
        ) from exc
    except httpx.HTTPError as exc:
        raise DownloadError(f"GET {sha_url} failed: {exc}") from exc

    tokens = text.split()
    if not tokens:
        raise DownloadError(f"empty sha256 sidecar at {sha_url}")
    digest = tokens[0]
    if re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
        raise DownloadError(f"invalid SHA-256 in sidecar at {sha_url}")
    return digest.lower()


def _decompress_bundle(
    zst_path: Path,
    db_path: Path,
    *,
    max_expanded_bytes: int,
) -> int:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=db_path.parent, suffix=".sqlite.tmp")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        decompressor = zstandard.ZstdDecompressor()
        with zst_path.open("rb") as zst_source:
            with decompressor.stream_reader(zst_source) as reader, tmp_path.open("wb") as writer:
                bytes_db = copy_bounded(
                    reader,
                    writer,
                    max_bytes=max_expanded_bytes,
                    label="expanded bundle",
                )
        os.replace(tmp_path, db_path)
        return bytes_db
    except (OSError, zstandard.ZstdError) as exc:
        raise DownloadError(f"failed to install bundle into {db_path}: {exc}") from exc
    finally:
        tmp_path.unlink(missing_ok=True)


def _decompress_bundle_bytes_for_test(
    compressed: bytes,
    db_path: Path,
    *,
    max_expanded_bytes: int,
) -> int:
    """Exercise the production decompression path without a network request."""
    fd, tmp_name = tempfile.mkstemp(dir=db_path.parent, suffix=".zst.tmp")
    os.close(fd)
    zst_path = Path(tmp_name)
    try:
        zst_path.write_bytes(compressed)
        return _decompress_bundle(
            zst_path,
            db_path,
            max_expanded_bytes=max_expanded_bytes,
        )
    finally:
        zst_path.unlink(missing_ok=True)


def download_verify_install(
    asset_url: str,
    *,
    db_path: Path,
    staging_dir: Path,
    expected_sha256: str,
    max_compressed_bytes: int = 2 << 30,
    max_expanded_bytes: int = 8 << 30,
    max_seconds: float | None = None,
) -> dict[str, Any]:
    """Download, verify, decompress, and atomically install a bundle.

    Streams the ``.zst`` from ``asset_url`` into ``staging_dir`` while computing
    its SHA-256. If the digest mismatches ``expected_sha256`` the partial file is
    removed and :class:`DownloadError` is raised. Otherwise the ``.zst`` is
    zstd-decompressed into a temp file beside ``db_path`` and atomically moved
    into place with :func:`os.replace`. The staging ``.zst`` and any temp file
    are cleaned up on both success and failure.

    Returns a dict: ``db_path``, ``bytes_compressed``, ``bytes_db``.
    """
    if re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256) is None:
        raise DownloadError("invalid expected bundle SHA-256")
    expected_sha256 = expected_sha256.lower()
    staging_dir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fd, zst_name = tempfile.mkstemp(dir=staging_dir, suffix=".zst.download")
    os.close(fd)
    zst_path = Path(zst_name)
    zst_path.unlink(missing_ok=True)

    digest = hashlib.sha256()
    try:
        policy = DownloadPolicy(
            allowed_hosts=_BUNDLE_HOSTS,
            max_bytes=max_compressed_bytes,
            max_seconds=max_seconds,
        )
        with httpx.Client(follow_redirects=False, timeout=_TIMEOUT) as client:
            with open_validated_stream(
                client,
                asset_url,
                headers={"User-Agent": _USER_AGENT},
                policy=policy,
            ) as response:
                response.raise_for_status()
                bytes_compressed = stream_atomic(
                    response,
                    zst_path,
                    max_bytes=max_compressed_bytes,
                    hasher=digest,
                    max_seconds=max_seconds,
                    chunk_size=_CHUNK_SIZE,
                )
    except httpx.HTTPStatusError as exc:
        zst_path.unlink(missing_ok=True)
        raise DownloadError(
            f"GET {asset_url} failed: HTTP {exc.response.status_code}",
            status_code=exc.response.status_code,
        ) from exc
    except httpx.HTTPError as exc:
        zst_path.unlink(missing_ok=True)
        raise DownloadError(f"GET {asset_url} failed: {exc}") from exc

    actual = digest.hexdigest()
    if actual != expected_sha256:
        zst_path.unlink(missing_ok=True)
        raise DownloadError(f"bundle sha256 mismatch: expected {expected_sha256}, got {actual}")

    try:
        bytes_db = _decompress_bundle(
            zst_path,
            db_path,
            max_expanded_bytes=max_expanded_bytes,
        )
    finally:
        zst_path.unlink(missing_ok=True)

    return {
        "db_path": str(db_path),
        "bytes_compressed": bytes_compressed,
        "bytes_db": bytes_db,
    }


def pull_latest(config: Settings) -> dict[str, Any]:
    """Resolve, download, verify, and install the configured bundle.

    Honours ``config.BUNDLE_URL``: a full ``http(s)`` URL is used directly,
    ``"latest"`` resolves the newest release asset of ``config.GITHUB_REPO``, and
    ``""`` raises :class:`DownloadError` (bundles disabled). Installs into
    ``config.db_path`` using ``config.BUNDLE_DOWNLOAD_DIR`` as the staging area.

    Returns a summary dict including the resolved ``release_tag`` and the install
    byte counts.
    """
    bundle_url = config.BUNDLE_URL.strip()
    if not bundle_url:
        raise DownloadError(
            "bundle download is disabled (BUNDLE_URL is empty); "
            "set BUNDLE_URL to 'latest' or a .sqlite.zst URL, or build locally"
        )

    if bundle_url.startswith("http"):
        asset_url = bundle_url
        release_tag = "bundle-pinned"
    elif bundle_url == "latest":
        asset_url, release_tag = resolve_latest_asset(
            config.GITHUB_REPO,
            asset_name=config.BUNDLE_ASSET_NAME,
            max_bytes=config.METADATA_MAX_BYTES,
            max_seconds=config.MAX_DOWNLOAD_SECONDS,
        )
    else:
        raise DownloadError(
            f"invalid BUNDLE_URL {bundle_url!r}: expected 'latest', '', or a full URL"
        )

    expected_sha256 = config.BUNDLE_EXPECTED_SHA256 or fetch_sibling_sha256(
        asset_url,
        max_bytes=config.METADATA_MAX_BYTES,
        max_seconds=config.MAX_DOWNLOAD_SECONDS,
    )
    install = download_verify_install(
        asset_url,
        db_path=config.db_path,
        staging_dir=config.BUNDLE_DOWNLOAD_DIR,
        expected_sha256=expected_sha256,
        max_compressed_bytes=config.BUNDLE_MAX_BYTES,
        max_expanded_bytes=config.BUNDLE_MAX_EXPANDED_BYTES,
        max_seconds=config.MAX_DOWNLOAD_SECONDS,
    )

    return {
        "release_tag": release_tag,
        "asset_url": asset_url,
        "sha256": expected_sha256,
        "db_path": install["db_path"],
        "bytes_compressed": install["bytes_compressed"],
        "bytes_db": install["bytes_db"],
    }
