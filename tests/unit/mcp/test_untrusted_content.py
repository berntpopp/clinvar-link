"""Structural untrusted-text fencing contracts."""

from __future__ import annotations

import hashlib

import pytest

from clinvar_link.mcp.untrusted_content import (
    DEFAULT_MAX_OBJECTS,
    UntrustedTextLimitError,
    bound_untrusted_rows,
    enforce_untrusted_text_limits,
    fence_untrusted_text,
)


def test_fence_normalizes_and_removes_forbidden_controls() -> None:
    raw = "Caf\u00e9\x00\u200b\u202e\nBRCA1"
    fenced = fence_untrusted_text(raw, source="clinvar", record_id="VCV000012345#trait:0")
    assert fenced.kind == "untrusted_text"
    assert fenced.text == "Caf\u00e9\nBRCA1"
    assert fenced.raw_sha256 == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert fenced.provenance.source == "clinvar"
    assert fenced.provenance.record_id == "VCV000012345#trait:0"


def test_fence_preserves_tabs_newlines_and_scientific_symbols() -> None:
    raw = "p.Gly12Asp\t\u0394G = \u22121.2 kcal/mol\r\n"
    assert fence_untrusted_text(raw, source="clinvar", record_id="VCV000012345#trait:0").text == raw


def test_limits_reject_oversized_object() -> None:
    big = fence_untrusted_text("x" * 10, source="clinvar", record_id="VCV000012345#trait:0")
    with pytest.raises(UntrustedTextLimitError):
        enforce_untrusted_text_limits([big], max_text_bytes=5)


def test_bound_untrusted_rows_cuts_whole_rows_at_count_and_byte_ceilings() -> None:
    def row(i: int, n: int, text: str = "x") -> tuple[int, list]:
        return i, [fence_untrusted_text(text, source="clinvar", record_id=f"{i}") for _ in range(n)]

    assert bound_untrusted_rows([row(i, 3) for i in range(10)]) == (list(range(10)), 0)
    kept, omitted = bound_untrusted_rows([row(i, 3) for i in range(100)])
    assert (len(kept), omitted) == (DEFAULT_MAX_OBJECTS // 3, 100 - DEFAULT_MAX_OBJECTS // 3)
    big = "y" * 2_000_000  # five of these pass the per-object ceiling but not the 8 MiB total
    assert bound_untrusted_rows([row(i, 1, big) for i in range(5)]) == ([0, 1, 2, 3], 1)
    with pytest.raises(UntrustedTextLimitError):  # one row over the ceiling alone is a typed error
        bound_untrusted_rows([row(0, DEFAULT_MAX_OBJECTS + 1)])
