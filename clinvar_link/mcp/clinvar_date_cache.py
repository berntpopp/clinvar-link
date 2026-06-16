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


def reset_clinvar_date_cache() -> None:
    """Test helper: clear the process-level ClinVar-date cache."""
    _CACHE.clear()
