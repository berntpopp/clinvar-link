"""Security primitives for streamed ingest downloads and decompression."""

from __future__ import annotations

import gzip
import io
import os
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, TextIO

import httpx

from clinvar_link.exceptions import DownloadError

_SAFE_REDIRECT_HEADERS = frozenset({"accept", "user-agent", "if-none-match", "if-modified-since"})
_REDIRECT_CODES = {301, 302, 303, 307, 308}


@dataclass(frozen=True)
class DownloadPolicy:
    """Allowed destinations and hard bounds for one download flow."""

    allowed_hosts: frozenset[str]
    max_redirects: int = 5
    max_bytes: int = 128 * 1024 * 1024
    max_seconds: float | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.max_redirects <= 5:
            raise ValueError("max_redirects must be between 0 and 5")


def validate_https_url(url: httpx.URL, policy: DownloadPolicy) -> None:
    """Reject non-HTTPS, userinfo, nonstandard ports, and unknown hosts."""
    host = (url.host or "").lower()
    if url.scheme != "https":
        raise DownloadError(f"download URL must use HTTPS: {url}")
    if url.userinfo:
        raise DownloadError("download URL must not contain user information")
    if url.port not in (None, 443):
        raise DownloadError(f"download URL port {url.port} is not allowed")
    if host not in policy.allowed_hosts:
        raise DownloadError(f"download host {host} is not allowed")


@contextmanager
def open_validated_stream(
    client: httpx.Client,
    url: str,
    *,
    headers: Mapping[str, str],
    policy: DownloadPolicy,
) -> Iterator[httpx.Response]:
    """Open a streamed GET after validating every redirect before network I/O."""
    try:
        current = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise DownloadError("download URL is invalid") from exc
    safe_headers = {
        name: value for name, value in headers.items() if name.lower() in _SAFE_REDIRECT_HEADERS
    }
    for hop in range(policy.max_redirects + 1):
        validate_https_url(current, policy)
        request = client.build_request("GET", current, headers=safe_headers)
        response = client.send(request, stream=True, follow_redirects=False)
        if response.status_code not in _REDIRECT_CODES:
            try:
                yield response
            finally:
                response.close()
            return
        location = response.headers.get("Location")
        response.close()
        if location is None:
            raise DownloadError("redirect response is missing Location")
        if hop == policy.max_redirects:
            raise DownloadError(f"download exceeded {policy.max_redirects} redirects")
        try:
            current = current.join(location)
        except httpx.InvalidURL as exc:
            raise DownloadError("redirect Location is invalid") from exc
    raise AssertionError("redirect loop exhausted unexpectedly")


def _content_length(response: httpx.Response) -> int | None:
    raw_length = response.headers.get("Content-Length")
    try:
        return int(raw_length) if raw_length is not None else None
    except ValueError:
        return None


def stream_atomic(
    response: httpx.Response,
    destination: Path,
    *,
    max_bytes: int,
    expected_size: int | None = None,
    hasher: Any | None = None,
    max_seconds: float | None = None,
    chunk_size: int = 1 << 16,
) -> int:
    """Stream a bounded response into a same-directory atomic replacement."""
    content_length = _content_length(response)
    if content_length is not None and content_length > max_bytes:
        raise DownloadError(f"download Content-Length {content_length} exceeded {max_bytes} bytes")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=destination.parent, suffix=".download.tmp")
    tmp_path = Path(tmp_name)
    started = time.monotonic()
    written = 0
    try:
        with os.fdopen(fd, "wb") as handle:
            for chunk in response.iter_bytes(chunk_size):
                written += len(chunk)
                if written > max_bytes:
                    raise DownloadError(f"download exceeded {max_bytes} bytes")
                if max_seconds is not None and time.monotonic() - started > max_seconds:
                    raise DownloadError(f"download exceeded {max_seconds:g} seconds")
                handle.write(chunk)
                if hasher is not None:
                    hasher.update(chunk)
        if expected_size is not None and written != expected_size:
            raise DownloadError(
                f"download size mismatch: expected {expected_size}, received {written}"
            )
        os.replace(tmp_path, destination)
        return written
    finally:
        tmp_path.unlink(missing_ok=True)


def copy_bounded(
    source: BinaryIO,
    destination: BinaryIO,
    *,
    max_bytes: int,
    label: str = "expanded artifact",
) -> int:
    """Copy a stream while rejecting output above ``max_bytes``."""
    written = 0
    while chunk := source.read(min(1 << 20, max_bytes - written + 1)):
        written += len(chunk)
        if written > max_bytes:
            raise DownloadError(f"{label} exceeded {max_bytes} bytes")
        destination.write(chunk)
    return written


class _BoundedRawReader(io.RawIOBase):
    """Count decompressed bytes before returning them to a text wrapper."""

    def __init__(self, source: BinaryIO, *, max_bytes: int, label: str) -> None:
        self._source = source
        self._max_bytes = max_bytes
        self._label = label
        self._read = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        chunk = self._source.read(len(buffer))
        self._read += len(chunk)
        if self._read > self._max_bytes:
            raise DownloadError(f"{self._label} exceeded {self._max_bytes} bytes")
        buffer[: len(chunk)] = chunk
        return len(chunk)

    def close(self) -> None:
        try:
            self._source.close()
        finally:
            super().close()


@contextmanager
def open_source_text(path: Path, *, max_expanded_bytes: int) -> Iterator[TextIO]:
    """Open a source file as text while bounding bytes returned from gzip."""
    raw = path.open("rb")
    source: Any = gzip.GzipFile(fileobj=raw, mode="rb") if path.suffix == ".gz" else raw
    bounded = _BoundedRawReader(
        source,
        max_bytes=max_expanded_bytes,
        label="expanded source",
    )
    buffered = io.BufferedReader(bounded)
    text = io.TextIOWrapper(buffered, encoding="utf-8", errors="replace")
    try:
        yield text
    finally:
        text.close()
