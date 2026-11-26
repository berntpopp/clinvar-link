"""Gene identifier forms on `gene_symbol`, and `_meta.pagination` on list tools."""

from __future__ import annotations

import sqlite3

import pytest

from clinvar_link.data.repository import ClinVarRepository
from clinvar_link.exceptions import UnsupportedGeneIdError
from clinvar_link.mcp.facade import create_clinvar_mcp
from clinvar_link.services.gene_ids import resolve_gene_argument
from tests._fixture_db import build_service, call_tool


@pytest.fixture
def mcp(tmp_path):
    service = build_service(tmp_path)
    yield create_clinvar_mcp(service_factory=lambda: service)
    service.repo.close()


class _Repo:
    def symbol_for_hgnc_id(self, hgnc_id: str) -> str | None:
        return {"HGNC:1100": "BRCA1"}.get(hgnc_id)


def test_symbol_passes_through_and_hgnc_id_converts():
    assert resolve_gene_argument(_Repo(), "brca1") == ("brca1", None)
    assert resolve_gene_argument(_Repo(), None) == (None, None)
    assert resolve_gene_argument(_Repo(), "hgnc:1100") == (
        "BRCA1",
        {"input": "hgnc:1100", "form": "hgnc_id", "resolved_to": "BRCA1"},
    )


@pytest.mark.parametrize(
    ("value", "step"),
    [
        (
            "ENSG00000012048",
            {"tool": "hgnc_resolve_symbol", "arguments": {"query": "ENSG00000012048"}},
        ),
        ("HGNC:999999", {"tool": "hgnc_resolve_symbol", "arguments": {"query": "HGNC:999999"}}),
        (
            "NCBIGene:672",
            {
                "tool": "hgnc_resolve_gene_by_xref",
                "arguments": {"source": "entrez_id", "value": "672"},
            },
        ),
    ],
)
def test_unconvertible_forms_name_their_converter(value, step):
    with pytest.raises(UnsupportedGeneIdError) as err:
        resolve_gene_argument(_Repo(), value)
    assert err.value.converter == step


def test_symbol_for_hgnc_id_reads_the_approved_row_and_survives_an_old_index(tmp_path):
    path = tmp_path / "t.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meta (id)")
    conn.commit()
    repo = ClinVarRepository(path)
    assert repo.symbol_for_hgnc_id("HGNC:1100") is None  # an index built before gene_alias
    conn.execute("CREATE TABLE gene_alias (symbol_upper, hgnc_id, approved_symbol, kind)")
    conn.executemany(
        "INSERT INTO gene_alias VALUES (?, 'HGNC:1100', 'BRCA1', ?)",
        [("RNF53", "previous"), ("BRCA1", "approved")],
    )
    conn.commit()
    assert repo.symbol_for_hgnc_id("HGNC:1100") == "BRCA1"
    assert repo.symbol_for_hgnc_id("HGNC:1") is None
    repo.close()


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("get_gene_clinvar_summary", {}),
        ("get_variants_by_gene", {}),
        ("search_variants", {"query": "c.181T>G"}),
        ("search_somatic_classifications", {}),
    ],
)
async def test_foreign_gene_id_is_invalid_input_with_a_converter(mcp, tool, args):
    out = await call_tool(mcp, tool, {**args, "gene_symbol": "ENSG00000012048"})
    assert out["error_code"] == "invalid_input"
    assert out["field"] == "gene_symbol"
    assert out["allowed_values"]
    assert out["_meta"]["next_commands"][0] == {
        "tool": "hgnc_resolve_symbol",
        "arguments": {"query": "ENSG00000012048"},
    }


async def test_list_tool_reports_pagination_meta(mcp):
    out = await call_tool(mcp, "get_variants_by_gene", {"gene_symbol": "BRCA1", "limit": 1})
    assert out["_meta"]["pagination"] == {
        "total_count": out["total_count"],
        "has_more": out["has_more"],
        "next_cursor": None,
        "next_offset": out["next_offset"],
    }
