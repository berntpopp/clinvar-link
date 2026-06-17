"""Pure helper: derive data-freshness fields from the cached ClinVar release date.

Leaf module (stdlib only) so the envelope (``errors``) and capabilities
(``resources``) builders can both import it without a cycle.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def clinvar_freshness(
    release_date: str | None,
    ttl_days: int,
    *,
    now: datetime | None = None,
) -> dict[str, int | bool] | None:
    """Return ``{age_days, past_ttl}`` for an RFC1123 release date, or ``None``.

    ``age_days`` is whole days between the release date and ``now`` (UTC), floored
    at 0; ``past_ttl`` is ``age_days > ttl_days``. Returns ``None`` when the date
    is missing or unparseable so callers simply omit the fields.
    """
    if not release_date:
        return None
    try:
        released = parsedate_to_datetime(release_date)
    except (TypeError, ValueError):
        return None
    if released is None:
        return None
    if released.tzinfo is None:
        released = released.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    age_days = max(0, (current - released).days)
    return {"age_days": age_days, "past_ttl": age_days > ttl_days}
