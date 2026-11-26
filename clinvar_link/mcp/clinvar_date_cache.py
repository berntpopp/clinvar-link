"""Process-level cache of the live ClinVar release date.

A leaf module with no intra-package imports, so the metadata tool (the writer)
and the envelope/resource builders (the readers) can share one cache without an
import cycle: ``clinvar_link.mcp.errors`` and ``clinvar_link.mcp.resources`` read
it, while the capabilities tool (which imports ``errors``) writes it on the
first ``get_server_capabilities`` call.

Only SUCCESSFUL reads of the DB meta release date are recorded, so a transient
failure retries on the next call rather than pinning a wrong null for the
process lifetime.
"""

from __future__ import annotations

from datetime import UTC, datetime

_CACHE: dict[str, str | None] = {}


def has_cached_clinvar_release_date() -> bool:
    """True once a fetch (successful or explicitly None) has been recorded."""
    return "date" in _CACHE


def get_cached_clinvar_release_date() -> str | None:
    """Return the cached ClinVar release date, or None if not yet fetched."""
    return _CACHE.get("date")


def set_cached_clinvar_release_date(date: str | None) -> None:
    """Record a fetched ClinVar release date for the process lifetime."""
    _CACHE["date"] = date


def get_cached_retrieved_at() -> str | None:
    """Return when the served ClinVar snapshot was downloaded (RFC 3339 UTC, ``Z``), if known."""
    return _CACHE.get("retrieved_at")


def set_cached_retrieved_at(value: str | None) -> None:
    """Record the snapshot download time; normalizes any ISO-8601 UTC offset to ``Z``."""
    if value:
        _CACHE["retrieved_at"] = (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            .astimezone(UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )


def reset_clinvar_date_cache() -> None:
    """Test helper: clear the process-level ClinVar-date cache."""
    _CACHE.clear()
