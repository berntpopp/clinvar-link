"""ClinVar somatic classifications: clinical impact (AMP/ASCO/CAP tier) + oncogenicity.

Fixtures are REAL ClinVar data: ``variant_summary_somatic.txt`` holds the unedited
variant_summary rows of BRAF V600E (13961), SMAD4 Q388* (24842), a TAT variant whose somatic
columns read "no classification for the single variant" (2626791) and germline-only BRCA1
c.5266dup (17677); ``vcv_somatic_sample.xml`` is the efetch VCV XML of 13961 and 24842 trimmed
to a few SCVs (the parser's input shape is the monthly dump's ``VariationArchive``).
"""

from __future__ import annotations

import io
import shutil
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from clinvar_link.config import Settings
from clinvar_link.data.repository import ClinVarRepository
from clinvar_link.ingest.builder import build_database
from clinvar_link.ingest.parsing import parse_somatic_columns
from clinvar_link.ingest.submissions import iter_submissions, load_submissions
from clinvar_link.mcp.facade import create_clinvar_mcp
from clinvar_link.services import ClinVarService
from tests._fixture_db import call_tool

FIXTURES = Path(__file__).parent / "fixtures"
TSV = FIXTURES / "variant_summary_somatic.txt"
XML = FIXTURES / "vcv_somatic_sample.xml"
TOOL = "search_somatic_classifications"


@pytest.fixture(scope="module")
def somatic_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    cfg = Settings(DATA_DIR=tmp_path_factory.mktemp("somatic"), DB_FILENAME="s.sqlite")
    build_database(
        cfg,
        source_path=TSV,
        secondary_loader=lambda conn: load_submissions(conn, io.BytesIO(XML.read_bytes())),
        last_modified="Mon, 16 Nov 2026 21:41:14 GMT",
    )
    return cfg.db_path


@pytest.fixture
def mcp(somatic_db: Path) -> Any:
    service = ClinVarService(ClinVarRepository(somatic_db))
    yield create_clinvar_mcp(service_factory=lambda: service)
    service.repo.close()


def _texts(fenced: list[dict[str, Any]]) -> list[str]:
    assert all(f["kind"] == "untrusted_text" for f in fenced)
    return [f["text"] for f in fenced]


# --------------------------------------------------------------------------- ingest


def test_parse_somatic_columns_normalizes_placeholders() -> None:
    assert parse_somatic_columns({"SomaticClinicalImpact": "-", "Oncogenicity": "-"}) is None
    unclassified = "no classification for the single variant"
    assert (
        parse_somatic_columns({"SomaticClinicalImpact": unclassified, "Oncogenicity": unclassified})
        is None
    )
    row = {
        "SomaticClinicalImpact": "Tier I - Strong",
        "SomaticClinicalImpactLastEvaluated": "Nov 05, 2025",
        "ReviewStatusClinicalImpact": "criteria provided, multiple submitters",
        "Oncogenicity": "-",
        "OncogenicityLastEvaluated": "-",
        "ReviewStatusOncogenicity": "-",
    }
    assert parse_somatic_columns(row) == {
        "clinical_impact": "Tier I - Strong",
        "clinical_impact_review_status": "criteria provided, multiple submitters",
        "clinical_impact_last_evaluated": "Nov 05, 2025",
        "oncogenicity": None,
        "oncogenicity_review_status": None,
        "oncogenicity_last_evaluated": None,
    }


def test_vcv_stream_collects_somatic_scvs_with_tumour_and_therapy() -> None:
    somatic: list[dict[str, Any]] = []
    stats: Counter[str] = Counter()
    subs = list(
        iter_submissions(io.BytesIO(XML.read_bytes()), stats=stats, on_somatic=somatic.append)
    )
    assert subs == []  # no OMIM SCV in this fixture: the OMIM stream is unaffected
    by_scv = {s["scv"]: s for s in somatic}
    # The germline SCV000061601 on the same record is not a somatic assertion.
    assert sorted(by_scv) == [
        "SCV004565360",
        "SCV004565362",
        "SCV005094141",
        "SCV005870910",
        "SCV005872010",
        "SCV006076886",
        "SCV007105098",
    ]
    civic = by_scv["SCV004565360"]
    assert civic == {
        "scv": "SCV004565360",
        "variation_id": 13961,
        "submitter": "CIViC Knowledgebase, Washington University School of Medicine",
        "kind": "clinical_impact",
        "classification": "Tier I - Strong",
        "assertion_type": "therapeutic",
        "significance": "sensitivity/response",
        "drugs": ["Dabrafenib", "Trametinib"],
        "tumour_types": [{"name": "Melanoma", "medgen_id": "C0025202"}],
        "review_status": "criteria provided, single submitter",
        "last_evaluated": "2018-05-15",
    }
    # A trait submitted only as an XRef is named through the record's TraitMapping.
    assert by_scv["SCV007105098"]["tumour_types"] == [
        {"name": "Diffuse midline glioma, H3 K27M-mutant", "medgen_id": "C4289688"}
    ]
    onco = by_scv["SCV005094141"]
    assert (onco["kind"], onco["classification"], onco["drugs"]) == (
        "oncogenicity",
        "Oncogenic",
        [],
    )
    assert stats["somatic"] == 7


# --------------------------------------------------------------------------- acceptance


async def test_braf_v600e_shows_tier_i_and_oncogenic_beside_germline_conflicting(mcp) -> None:
    env = await call_tool(mcp, "get_variant", {"identifier": "VCV000013961"})
    assert env["success"] is True
    # The germline aggregate is untouched...
    assert env["classification"] == "conflicting"
    assert env["review_status"] == "criteria provided, conflicting classifications"
    # ...and the two somatic axes sit beside it, never merged into it.
    assert env["somatic_clinical_impact"] == {
        "classification": "Tier I - Strong",
        "review_status": "criteria provided, multiple submitters",
        "last_evaluated": "2025-11-05",
    }
    assert env["oncogenicity"] == {
        "classification": "Oncogenic",
        "review_status": "criteria provided, single submitter",
        "last_evaluated": "2025-03-04",
    }


async def test_standard_mode_lists_per_scv_tumour_and_therapy(mcp) -> None:
    env = await call_tool(mcp, "get_variant", {"identifier": "13961", "response_mode": "standard"})
    impact = env["somatic_clinical_impact"]["assertions"]
    civic = next(a for a in impact if a["scv"] == "SCV004565360")
    assert _texts([t["name"] for t in civic["tumour_types"]]) == ["Melanoma"]
    assert civic["tumour_types"][0]["medgen_id"] == "C0025202"
    assert _texts(civic["drugs"]) == ["Dabrafenib", "Trametinib"]
    assert civic["assertion_type"] == "therapeutic"
    assert [a["scv"] for a in env["oncogenicity"]["assertions"]] == ["SCV005094141"]


async def test_germline_only_and_unclassified_variants_carry_null_somatic_blocks(mcp) -> None:
    for vid in ("17677", "2626791"):
        env = await call_tool(mcp, "get_variant", {"identifier": vid})
        assert env["somatic_clinical_impact"] is None
        assert env["oncogenicity"] is None


async def test_index_without_somatic_tables_omits_blocks_and_tool_errors(
    somatic_db: Path, tmp_path: Path
) -> None:
    old = tmp_path / "old.sqlite"
    shutil.copy(somatic_db, old)
    with sqlite3.connect(old) as conn:
        conn.execute("DROP TABLE somatic_classification")
        conn.execute("DROP TABLE somatic_assertion")
    service = ClinVarService(ClinVarRepository(old))
    try:
        mcp = create_clinvar_mcp(service_factory=lambda: service)
        env = await call_tool(mcp, "get_variant", {"identifier": "13961"})
        assert env["success"] is True
        assert "somatic_clinical_impact" not in env and "oncogenicity" not in env
        err = await call_tool(mcp, TOOL, {})
        assert err["success"] is False and err["error_code"] == "internal"
    finally:
        service.repo.close()


# --------------------------------------------------------------------------- search tool


async def test_search_lists_every_somatic_classification_by_tier(mcp) -> None:
    env = await call_tool(mcp, TOOL, {})
    assert env["success"] is True
    assert [r["vcv_accession"] for r in env["results"]] == ["VCV000013961", "VCV000024842"]
    assert env["total_count"] == 2 and env["has_more"] is False
    braf = env["results"][0]
    assert braf["germline_classification"] == "conflicting"  # get_variant's own vocabulary
    assert braf["somatic_clinical_impact"]["classification"] == "Tier I - Strong"
    assert braf["oncogenicity"]["classification"] == "Oncogenic"
    assert "citation_template" in env["_meta"]
    assert env["_meta"]["next_commands"][0] == {
        "tool": TOOL,
        "arguments": {"identifier": "VCV000013961", "response_mode": "standard"},
    }


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"gene_symbol": "braf"}, ["VCV000013961"]),
        ({"tier": "tier_ii"}, ["VCV000024842"]),
        ({"oncogenicity": "likely_oncogenic"}, ["VCV000024842"]),
        ({"oncogenicity": "oncogenic", "tier": "tier_i"}, ["VCV000013961"]),
        ({"tumour_type": "melanoma"}, ["VCV000013961"]),
        ({"tumour_type": "colorectal"}, ["VCV000013961", "VCV000024842"]),
        ({"tumour_type": "c0025202"}, ["VCV000013961"]),  # a MedGen id matches exactly
        ({"gene_symbol": "BRCA1"}, []),  # a known gene with no somatic call: honest zero
        ({"tier": "tier_iv"}, []),
    ],
)
async def test_search_filters(mcp, args: dict[str, Any], expected: list[str]) -> None:
    env = await call_tool(mcp, TOOL, args)
    assert env["success"] is True
    assert [r["vcv_accession"] for r in env["results"]] == expected
    assert env["total_count"] == len(expected)


async def test_search_pagination_is_honest(mcp) -> None:
    env = await call_tool(mcp, TOOL, {"limit": 1})
    assert (env["count"], env["total_count"], env["has_more"], env["next_offset"]) == (
        1,
        2,
        True,
        1,
    )
    # The next page is the first suggested command, filters kept.
    assert env["_meta"]["next_commands"][0] == {
        "tool": TOOL,
        "arguments": {"limit": 1, "offset": 1, "response_mode": "compact"},
    }
    last = await call_tool(mcp, TOOL, {"limit": 1, "offset": 1})
    assert last["_meta"]["next_commands"][0]["arguments"]["identifier"] == "VCV000024842"


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({"gene_symbol": "NOTAGENE1"}, "gene_symbol"),
        ({"tumour_type": "__no_such_tumour__"}, "tumour_type"),
        ({"tier": "Tier V"}, "tier"),
        ({"oncogenicity": "pathogenic"}, "oncogenicity"),
    ],
)
async def test_search_rejects_unknown_filter_values_naming_the_parameter(
    mcp, args: dict[str, Any], field: str
) -> None:
    env = await call_tool(mcp, TOOL, args)
    assert env["success"] is False and env["error_code"] == "invalid_input"
    assert [e["field"] for e in env["field_errors"]] == [field]


async def test_search_standard_rows_carry_fenced_assertions(mcp) -> None:
    env = await call_tool(mcp, TOOL, {"tumour_type": "melanoma", "response_mode": "standard"})
    row = env["results"][0]
    assert "recommended_citation" in row
    scvs = [a["scv"] for a in row["somatic_clinical_impact"]["assertions"]]
    assert "SCV004565360" in scvs
    minimal = await call_tool(mcp, TOOL, {"response_mode": "minimal"})
    assert "germline_classification" not in minimal["results"][0]
    assert "assertions" not in minimal["results"][0]["somatic_clinical_impact"]


def test_build_fails_when_the_vcv_xml_yields_no_somatic_scv(respx_mock, tmp_path: Path) -> None:
    """Somatic aggregates in variant_summary but none in the XML = a parse drift: never ship it."""
    import httpx

    from clinvar_link.exceptions import ClinVarDataError
    from clinvar_link.ingest.submissions import make_loader
    from tests.test_submissions import HGNC, URL, _gz_payload, _summary_gz

    body, md5 = _gz_payload()  # the OMIM fixture: no somatic SCV at all
    cfg = Settings(DATA_DIR=tmp_path / "data", DB_FILENAME="t.sqlite", VCV_XML_URL=URL)
    respx_mock.get(f"{URL}.md5").mock(return_value=httpx.Response(200, text=md5))
    respx_mock.get(URL).mock(return_value=httpx.Response(200, content=body))
    loader = make_loader(
        cfg,
        summary={"path": str(_summary_gz(tmp_path, 5))},
        hgnc={"path": str(HGNC)},
        report={},
    )
    with pytest.raises(ClinVarDataError, match="no somatic SCV"):
        build_database(cfg, source_path=TSV, secondary_loader=loader)
    assert not cfg.db_path.exists()


async def test_germline_last_evaluated_is_iso_like_the_somatic_dates(mcp) -> None:
    env = await call_tool(mcp, "get_variant", {"identifier": "13961", "response_mode": "standard"})
    assert env["last_evaluated"] == "2025-01-23"


async def test_search_by_tumour_type_shows_only_that_tumour_s_assertions(mcp) -> None:
    env = await call_tool(mcp, TOOL, {"tumour_type": "melanoma", "response_mode": "standard"})
    block = env["results"][0]["somatic_clinical_impact"]
    names = {t["name"]["text"] for a in block["assertions"] for t in a["tumour_types"]}
    assert names == {"Melanoma"}
    assert block["other_assertions"] >= 1  # the colorectal SCV, counted not sent


@pytest.mark.parametrize(
    "args",
    [{"tier": "Tier I"}, {"tier": "Tier 1"}, {"tier": "Tier I - Strong"}, {"tier": "TIER_I"}],
)
async def test_tier_accepts_clinvar_s_own_wording(mcp, args: dict[str, Any]) -> None:
    env = await call_tool(mcp, TOOL, args)
    assert [r["vcv_accession"] for r in env["results"]] == ["VCV000013961"]


async def test_oncogenicity_accepts_clinvar_s_wording_and_explains_pathogenic(mcp) -> None:
    env = await call_tool(mcp, TOOL, {"oncogenicity": "Likely oncogenic"})
    assert [r["vcv_accession"] for r in env["results"]] == ["VCV000024842"]
    err = await call_tool(mcp, TOOL, {"oncogenicity": "pathogenic"})
    assert "germline" in err["message"]


async def test_a_tumour_phrase_miss_says_whether_one_word_of_it_matches(mcp) -> None:
    part = await call_tool(mcp, TOOL, {"tumour_type": "metastatic melanoma"})
    none = await call_tool(mcp, TOOL, {"tumour_type": "zzqqxx qqzzyy"})
    assert part["error_code"] == none["error_code"] == "invalid_input"
    assert "one word of it" in part["message"] and "one word of it" not in none["message"]
    assert "'melanoma'" in part["message"]  # the matching word, a substring of a known name


async def test_batch_get_variants_carries_the_somatic_blocks_too(mcp) -> None:
    env = await call_tool(mcp, "get_variants", {"identifiers": ["VCV000013961", "17677"]})
    braf, germline_only = env["results"]
    assert braf["somatic_clinical_impact"]["classification"] == "Tier I - Strong"
    assert braf["oncogenicity"]["classification"] == "Oncogenic"
    assert germline_only["somatic_clinical_impact"] is None
    assert germline_only["oncogenicity"] is None


async def test_search_by_identifier_gives_one_variant_s_somatic_detail(mcp) -> None:
    env = await call_tool(mcp, TOOL, {"identifier": "VCV000013961", "response_mode": "standard"})
    (row,) = env["results"]
    assert row["vcv_accession"] == "VCV000013961"
    assert row["somatic_clinical_impact"]["assertions"]
    assert "submissions" not in row and "traits" not in row  # no germline payload
    germline_only = await call_tool(mcp, TOOL, {"identifier": "17677"})
    assert germline_only["success"] is True and germline_only["total_count"] == 0
    bad = await call_tool(mcp, TOOL, {"identifier": "BRAF V600E"})
    assert bad["error_code"] == "invalid_input"


async def test_get_variant_points_a_somatic_variant_at_its_somatic_detail(mcp) -> None:
    env = await call_tool(mcp, "get_variant", {"identifier": "VCV000013961"})
    assert env["_meta"]["next_commands"][0] == {
        "tool": TOOL,
        "arguments": {"identifier": "VCV000013961", "response_mode": "standard"},
    }
    plain = await call_tool(mcp, "get_variant", {"identifier": "17677"})
    assert all(c["tool"] != TOOL for c in plain["_meta"]["next_commands"])


async def test_loose_text_identifier_recovery_names_the_fallback_tool(mcp) -> None:
    env = await call_tool(mcp, "get_variant", {"identifier": "BRAF V600E"})
    assert env["fallback_tool"] == "search_variants"
    assert "search_variants" in env["recovery"]


async def test_identifier_search_does_not_suggest_itself(mcp) -> None:
    env = await call_tool(mcp, TOOL, {"identifier": "VCV000013961", "response_mode": "standard"})
    assert all(c["tool"] != TOOL for c in env["_meta"]["next_commands"])


async def test_a_bad_tier_does_not_point_at_the_germline_gene_summary(mcp) -> None:
    env = await call_tool(mcp, TOOL, {"gene_symbol": "BRAF", "tier": "Tier V"})
    assert env["fallback_tool"] != "get_gene_clinvar_summary"
