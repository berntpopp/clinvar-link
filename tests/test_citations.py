"""Submission citations (var_citations.txt): per-variant PMIDs and the PMID -> variants lookup (#119)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from clinvar_link.config import Settings
from clinvar_link.data.repository import ClinVarRepository
from clinvar_link.ingest.builder import build_database
from clinvar_link.mcp.facade import create_clinvar_mcp
from clinvar_link.services import ClinVarService
from tests._fixture_db import FIXTURE, call_tool

CITATIONS = Path(__file__).parent / "fixtures" / "var_citations_sample.txt"


def _build(tmp_path: Path, *, drop_table: bool = False) -> Path:
    cfg = Settings(DATA_DIR=tmp_path, DB_FILENAME="t.sqlite")
    build_database(cfg, source_path=FIXTURE, citations_source_path=CITATIONS)
    if drop_table:  # simulate a bundle built before the citation index existed
        conn = sqlite3.connect(cfg.db_path)
        conn.execute("DROP TABLE variant_citation")
        conn.commit()
        conn.close()
    return cfg.db_path


@pytest.fixture
def mcp(tmp_path):
    service = ClinVarService(ClinVarRepository(_build(tmp_path)))
    yield create_clinvar_mcp(service_factory=lambda: service)
    service.repo.close()


def test_builder_dedups_and_drops_unknown_variants(tmp_path):
    conn = sqlite3.connect(_build(tmp_path))
    rows = conn.execute(
        "SELECT variation_id, source, citation_id FROM variant_citation ORDER BY 1, 2, 3"
    ).fetchall()
    conn.close()
    assert rows == [
        (100001, "NCBIBookShelf", "NBK1247"),
        (100001, "PubMed", "20613862"),
        (100001, "PubMed", "29058690"),
        (100002, "PubMed", "29058690"),
        (100006, "PubMedCentral", "4544753"),
    ]


async def test_get_variant_returns_its_citations(mcp):
    out = await call_tool(mcp, "get_variant", {"identifier": "VCV000100001"})
    assert out["success"] is True
    assert out["citation_count"] == 3
    assert {"source": "PubMed", "id": "29058690"} in out["citations"]


async def test_get_variant_minimal_omits_citations(mcp):
    out = await call_tool(
        mcp, "get_variant", {"identifier": "VCV000100001", "response_mode": "minimal"}
    )
    assert "citations" not in out


async def test_get_variant_uncited_is_an_honest_zero(mcp):
    out = await call_tool(mcp, "get_variant", {"identifier": "VCV000100003"})
    assert out["citations"] == [] and out["citation_count"] == 0


async def test_variants_by_pmid_lists_the_cited_records(mcp):
    out = await call_tool(mcp, "get_variants_by_pmid", {"pmid": "29058690"})
    assert out["success"] is True
    assert sorted(r["variation_id"] for r in out["results"]) == [100001, 100002]
    assert out["total_count"] == 2 and out["has_more"] is False
    assert out["_meta"]["next_commands"][0]["tool"] == "get_variant"


async def test_variants_by_pmid_paginates(mcp):
    out = await call_tool(mcp, "get_variants_by_pmid", {"pmid": "29058690", "limit": 1})
    assert out["count"] == 1 and out["has_more"] is True and out["next_offset"] == 1


async def test_variants_by_pmid_unknown_is_not_found(mcp):
    out = await call_tool(mcp, "get_variants_by_pmid", {"pmid": "1"})
    assert out["success"] is False and out["error_code"] == "not_found"


async def test_variants_by_pmid_rejects_a_non_numeric_pmid(mcp):
    out = await call_tool(mcp, "get_variants_by_pmid", {"pmid": "29058690a"})
    assert out["success"] is False and out["error_code"] == "invalid_input"


async def test_variants_by_pmid_accepts_the_pmid_prefix(mcp):
    out = await call_tool(mcp, "get_variants_by_pmid", {"pmid": "PMID: 29058690"})
    assert out["success"] is True and out["pmid"] == "29058690" and out["count"] == 2


async def test_variants_by_pmid_not_found_recovery_names_the_paper(mcp):
    out = await call_tool(mcp, "get_variants_by_pmid", {"pmid": "1"})
    assert "PubMed id" in out["recovery"]


async def test_index_without_citation_table_is_honest(tmp_path):
    service = ClinVarService(ClinVarRepository(_build(tmp_path, drop_table=True)))
    mcp = create_clinvar_mcp(service_factory=lambda: service)
    variant = await call_tool(mcp, "get_variant", {"identifier": "VCV000100001"})
    assert variant["success"] is True and "citations" not in variant
    by_pmid = await call_tool(mcp, "get_variants_by_pmid", {"pmid": "29058690"})
    assert by_pmid["success"] is False
    service.repo.close()
