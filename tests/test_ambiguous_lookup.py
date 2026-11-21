"""A lookup key held by more than one ClinVar record is ambiguous_query, never a random pick.

Real data: ~222k rsIDs are multi-allelic (rs267607823 = MLH1 c.1246A>G VUS and c.1246A>T
pathogenic) and ~1.4k Names are filed under several VCVs (duplicate CNV submissions).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clinvar_link.mcp.facade import create_clinvar_mcp
from tests._fixture_db import build_service, call_tool

AMBIGUOUS = Path(__file__).parent / "fixtures" / "variant_summary_ambiguous.txt"
CNV = "GRCh38/hg38 7q11.23(chr7:73040501-75255046)x3"


@pytest.fixture
def mcp(tmp_path):
    service = build_service(tmp_path, AMBIGUOUS)
    yield create_clinvar_mcp(service_factory=lambda: service)
    service.repo.close()


@pytest.mark.parametrize(
    ("identifier", "candidates"),
    [
        ("rs267607823", ["VCV000100031", "VCV000100032"]),  # multi-allelic rsID
        (CNV, ["VCV000100033", "VCV000100034"]),  # one Name, two VCVs
    ],
)
async def test_get_variant_key_on_two_records_is_ambiguous(mcp, identifier, candidates):
    out = await call_tool(mcp, "get_variant", {"identifier": identifier})
    assert out["success"] is False
    assert out["error_code"] == "ambiguous_query"
    assert out["candidates"] == candidates
    assert out["_meta"]["next_commands"][: len(candidates)] == [
        {"tool": "get_variant", "arguments": {"identifier": vcv}} for vcv in candidates
    ]
    for vcv in candidates:
        assert vcv in out["message"]


async def test_get_variants_batch_marks_the_ambiguous_row(mcp):
    out = await call_tool(
        mcp, "get_variants", {"identifiers": ["rs267607823", "VCV000100020", "VCV999999999"]}
    )
    ambiguous, unique, absent = out["results"]
    assert ambiguous == {
        "identifier": "rs267607823",
        "found": False,
        "error_code": "ambiguous_query",
        "candidates": ["VCV000100031", "VCV000100032"],
    }
    assert unique["found"] is True and unique["vcv_accession"] == "VCV000100020"
    assert absent == {"identifier": "VCV999999999", "found": False}
    assert out["found_count"] == 1


async def test_a_key_on_exactly_one_record_still_resolves(mcp):
    out = await call_tool(mcp, "get_variant", {"identifier": "NM_014855.3:c.358C>T"})
    assert out["success"] is True and out["variation_id"] == 100020
    out = await call_tool(mcp, "get_variant", {"identifier": "NM_000249.4:c.1246A>T"})
    assert out["success"] is True and out["variation_id"] == 100032


def test_candidates_are_capped(tmp_path):
    import sqlite3

    from clinvar_link.data.repository import MAX_CANDIDATES, ClinVarRepository
    from clinvar_link.exceptions import AmbiguousIdentifierError

    build_service(tmp_path, AMBIGUOUS).repo.close()
    db = tmp_path / "t.sqlite"
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO rsid_lookup (rsid, variation_id) VALUES (5, ?)",
            [(vid,) for vid in (100020, 100031, 100032, 100033, 100034, 999999)],
        )
    repo = ClinVarRepository(db)
    with pytest.raises(AmbiguousIdentifierError) as err:
        repo.get_by_rsid(5)
    # 999999 has no variant row: it never counts. Five real records fill the cap exactly.
    assert err.value.candidates == [f"VCV000{v}" for v in (100020, 100031, 100032, 100033, 100034)]
    assert len(err.value.candidates) == MAX_CANDIDATES and err.value.more is False
    repo.close()


async def test_somatic_identifier_on_two_records_is_ambiguous(mcp):
    """The somatic tool resolves its identifier the same way; its next call is itself."""
    out = await call_tool(mcp, "search_somatic_classifications", {"identifier": "rs267607823"})
    assert out["error_code"] == "ambiguous_query"
    assert out["_meta"]["next_commands"][0] == {
        "tool": "search_somatic_classifications",
        "arguments": {"identifier": "VCV000100031"},
    }
