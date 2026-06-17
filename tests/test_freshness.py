"""Tests for the pure data-freshness helper."""

from datetime import datetime, timezone

from clinvar_link.mcp.freshness import clinvar_freshness


def test_freshness_within_ttl():
    now = datetime(2026, 6, 17, tzinfo=timezone.utc)
    assert clinvar_freshness("Mon, 15 Jun 2026 08:40:33 GMT", 7, now=now) == {
        "age_days": 1,
        "past_ttl": False,
    }


def test_freshness_past_ttl():
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    f = clinvar_freshness("Mon, 15 Jun 2026 08:40:33 GMT", 7, now=now)
    assert f["past_ttl"] is True and f["age_days"] >= 8


def test_freshness_none_for_missing_or_bad():
    assert clinvar_freshness(None, 7) is None
    assert clinvar_freshness("not-a-date", 7) is None
