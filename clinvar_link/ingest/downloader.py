"""Conditional download of the ClinVar ``variant_summary.txt.gz`` bulk dump.

NCBI's FTP-over-HTTPS endpoint honours ``ETag`` / ``Last-Modified``. We cache
the last-seen validators in a ``download_cache.json`` beside the database and
issue conditional ``GET`` requests, so a daily cron check is almost always a
cheap ``304 Not Modified`` and only transfers a body when the upstream data
actually changed. The response body is streamed to ``dest_path`` in chunks to
keep memory flat on the multi-hundred-MB dump.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx

from clinvar_link.exceptions import DownloadError

_CHUNK_SIZE = 1 << 16
_TIMEOUT = httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0)
_USER_AGENT = "clinvar-link/ingest (+https://github.com/berntpopp/clinvar-link)"


def _read_cache(cache_path: Path) -> dict[str, dict[str, str | None]]:
    if not cache_path.exists():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(
    cache_path: Path, url: str, *, etag: str | None, last_modified: str | None
) -> None:
    data = _read_cache(cache_path)
    data[url] = {"etag": etag, "last_modified": last_modified}
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _stream_to_file(response: httpx.Response, path: Path) -> str:
    """Stream the response body to ``path``, returning its SHA-256 hex digest.

    Hashing happens inline as chunks are written, so the body is only read once.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with path.open("wb") as handle:
        for chunk in response.iter_bytes(_CHUNK_SIZE):
            handle.write(chunk)
            digest.update(chunk)
    return digest.hexdigest()


def download_source(
    url: str,
    dest_path: Path,
    *,
    cache_path: Path,
    force: bool = False,
) -> dict[str, str | None]:
    """Conditionally download ``url`` to ``dest_path``.

    Sends ``If-None-Match`` / ``If-Modified-Since`` from ``cache_path`` unless
    ``force``. A ``304`` reuses the existing local file without a body transfer
    and returns ``status="not_modified"``; a ``200`` streams the new body and
    persists the fresh validators to ``cache_path``.

    Returns a dict: ``status`` (``"ok"`` | ``"not_modified"``), ``path``,
    ``etag``, ``last_modified``, ``sha256`` (the body digest on a 200; ``None``
    on a 304 since no body was transferred).
    """
    headers = {"User-Agent": _USER_AGENT}
    if not force:
        cached = _read_cache(cache_path).get(url, {})
        if cached.get("etag"):
            headers["If-None-Match"] = str(cached["etag"])
        if cached.get("last_modified"):
            headers["If-Modified-Since"] = str(cached["last_modified"])

    try:
        with (
            httpx.Client(follow_redirects=True, timeout=_TIMEOUT) as client,
            client.stream("GET", url, headers=headers) as response,
        ):
            if response.status_code == httpx.codes.NOT_MODIFIED:
                return {
                    "status": "not_modified",
                    "path": str(dest_path) if dest_path.exists() else None,
                    "etag": headers.get("If-None-Match"),
                    "last_modified": headers.get("If-Modified-Since"),
                    "sha256": None,
                }
            response.raise_for_status()
            etag = response.headers.get("ETag")
            last_modified = response.headers.get("Last-Modified")
            sha256 = _stream_to_file(response, dest_path)
    except httpx.HTTPStatusError as exc:
        raise DownloadError(
            f"GET {url} failed: HTTP {exc.response.status_code}",
            status_code=exc.response.status_code,
        ) from exc
    except httpx.HTTPError as exc:
        raise DownloadError(f"GET {url} failed: {exc}") from exc

    _write_cache(cache_path, url, etag=etag, last_modified=last_modified)
    return {
        "status": "ok",
        "path": str(dest_path),
        "etag": etag,
        "last_modified": last_modified,
        "sha256": sha256,
    }
