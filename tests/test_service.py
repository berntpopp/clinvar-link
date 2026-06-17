"""Tests for the async ClinVarService orchestration layer."""

from pathlib import Path

import pytest

from clinvar_link.config import Settings
from clinvar_link.data.repository import ClinVarRepository
from clinvar_link.exceptions import DataNotFoundError, ToolInputError
from clinvar_link.ingest.builder import build_database
from clinvar_link.services.clinvar_service import ClinVarService

FIXTURE = Path(__file__).parent / "fixtures" / "variant_summary_sample.txt"


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    """Build a real SQLite index and yield a ClinVarService over it."""
    d = tmp_path_factory.mktemp("svc")
    cfg = Settings(DATA_DIR=d, DB_FILENAME="t.sqlite")
    build_database(cfg, source_path=FIXTURE, last_modified="Mon, 01 Jan 2026 00:00:00 GMT")
    repo = ClinVarRepository(cfg.db_path)
    yield ClinVarService(repo)
    repo.close()


async def test_get_variant_by_vcv(service):
    out = await service.get_variant("VCV000100001")
    assert out["variation_id"] == 100001 and out["classification"] == "pathogenic"
    assert "VCV000100001" in out["recommended_citation"]


async def test_get_variant_by_rsid_auto(service):
    out = await service.get_variant("rs80357906")
    assert out["variation_id"] == 100001


async def test_get_variant_not_found(service):
    with pytest.raises(DataNotFoundError):
        await service.get_variant("VCV999999999")


async def test_get_variant_resolves_gene_unqualified_hgvs(service):
    # A clean transcript-qualified HGVS without the (GENE) qualifier resolves
    # first-try (no search_variants detour needed).
    out = await service.get_variant("NM_007294.4:c.5266dupC")
    assert out["variation_id"] == 100001


async def test_get_variants_batch_mixed(service):
    out = await service.get_variants(["VCV000100001", "rs80357906", "VCV999999999"])
    assert out["requested"] == 3 and out["found_count"] == 2 and out["count"] == 3
    results = out["results"]
    assert all("identifier" in r for r in results)
    found = [r for r in results if r.get("found")]
    assert {r["variation_id"] for r in found} == {100001}
    # Misses are explicit rows, never silently dropped.
    missing = [r for r in results if not r.get("found")]
    assert missing and missing[0]["identifier"] == "VCV999999999"
    # Compact batch hoists the citation like other list responses.
    assert "{variation_id}" in out["_meta"]["citation_template"]


async def test_get_variants_batch_empty_raises(service):
    with pytest.raises(ToolInputError):
        await service.get_variants([])


async def test_response_mode_minimal_vs_full(service):
    mn = await service.get_variant("VCV000100001", response_mode="minimal")
    fl = await service.get_variant("VCV000100001", response_mode="full")
    assert set(mn).issubset(set(fl)) and "coordinates" not in mn and "coordinates" in fl


async def test_search_and_gene(service):
    s = await service.search_variants("BRCA1", limit=5)
    assert s["count"] >= 1
    assert s["total_count"] >= s["count"]
    # Compact list mode hoists the citation to one _meta template; rows do not
    # repeat recommended_citation but keep the IDs needed to reconstruct it.
    assert all("recommended_citation" not in r for r in s["results"])
    assert "{variation_id}" in s["_meta"]["citation_template"]
    assert all("variation_id" in r and "vcv_accession" in r for r in s["results"])
    gs = await service.get_gene_clinvar_summary("BRCA1")
    assert gs["total_count"] >= 1
    bygene = await service.get_variants_by_gene("BRCA1", min_stars=0)
    assert bygene["total_count"] >= 1 and bygene["gene_symbol"].upper() == "BRCA1"


async def test_search_pagination_metadata(service):
    page = await service.search_variants("AP5Z1", limit=2, offset=0)
    assert page["total_count"] >= 3
    assert page["has_more"] is True
    assert page["next_offset"] == 2
    full = await service.search_variants("AP5Z1", limit=100, offset=0)
    assert full["has_more"] is False
    assert full["next_offset"] is None


async def test_variants_by_gene_pagination_metadata(service):
    page = await service.get_variants_by_gene("AP5Z1", limit=2, offset=0)
    assert page["total_count"] == 5
    assert page["has_more"] is True and page["next_offset"] == 2


async def test_compact_list_drops_cdna_change_duplicate(service):
    # cdna_change is the full Name verbatim, so the compact list projection drops
    # it as a duplicate to save tokens (name is retained).
    s = await service.search_variants("BRCA1", limit=5)
    assert all("cdna_change" not in r for r in s["results"])
    assert all(r.get("name") for r in s["results"])


async def test_full_mode_keeps_per_row_citation(service):
    s = await service.search_variants("BRCA1", limit=3, response_mode="full")
    assert all(r.get("recommended_citation") for r in s["results"])
    # No hoisted template in full mode — rows are self-contained.
    assert "citation_template" not in s.get("_meta", {})


async def test_meta(service):
    m = await service.get_clinvar_meta()
    assert m["release_date"]


async def test_search_negative_limit_is_clamped_not_unbounded(service):
    # A negative limit must never become SQLite "LIMIT -1" (an unbounded dump).
    out = await service.search_variants("BRCA1", limit=-1)
    assert out["limit"] == 1
    assert out["count"] <= out["total_count"]


async def test_variants_by_gene_negative_offset_clamped(service):
    out = await service.get_variants_by_gene("BRCA1", min_stars=0, offset=-5)
    assert out["offset"] == 0


async def test_variants_by_gene_empty_filter_is_success_not_error(service):
    # Existing gene + impossible filter (min_stars above the 0-4 range) -> empty
    # success, NOT a not_found error.
    out = await service.get_variants_by_gene("BRCA1", min_stars=5)
    assert out["gene_symbol"].upper() == "BRCA1"
    assert out["results"] == [] and out["count"] == 0 and out["total_count"] == 0


async def test_variants_by_gene_unknown_gene_still_not_found(service):
    with pytest.raises(DataNotFoundError):
        await service.get_variants_by_gene("NOTAGENE")


async def test_get_variant_garbage_is_invalid_input_not_not_found(service):
    with pytest.raises(ToolInputError):
        await service.get_variant("@@bad@@")


async def test_get_variant_unknown_id_type_is_invalid_input(service):
    with pytest.raises(ToolInputError):
        await service.get_variant("VCV000100001", id_type="banana")


async def test_get_variant_recognized_but_absent_still_not_found(service):
    with pytest.raises(DataNotFoundError):
        await service.get_variant("VCV999999999")


async def test_get_variants_batch_tolerates_malformed_identifier(service):
    out = await service.get_variants(["VCV000100001", "@@bad@@"])
    assert out["found_count"] == 1
    miss = [r for r in out["results"] if not r.get("found")]
    assert miss and miss[0]["identifier"] == "@@bad@@"


async def test_get_variants_batch_unknown_id_type_is_invalid_input(service):
    # A bad id_type is a structural param error -> fail the whole batch, do not
    # silently turn every row into a miss.
    with pytest.raises(ToolInputError):
        await service.get_variants(["VCV000100001"], id_type="banana")


async def test_variants_by_gene_unknown_sort_is_invalid_input(service):
    with pytest.raises(ToolInputError):
        await service.get_variants_by_gene("BRCA1", sort="banana")


async def test_search_blank_query_without_filter_is_invalid_input(service):
    with pytest.raises(ToolInputError):
        await service.search_variants("   ")


async def test_search_blank_query_with_filter_is_allowed(service):
    out = await service.search_variants("", gene_symbol="TTN")
    assert out["count"] >= 1


async def test_search_auto_falls_back_to_or(service):
    # "BRCA1" AND "Lynch" co-occur in NO variant; OR finds both gene sets.
    out = await service.search_variants("BRCA1 Lynch")
    assert out["match_mode"] == "or_fallback"
    assert out["count"] > 0


async def test_search_auto_uses_and_when_it_matches(service):
    out = await service.search_variants("BRCA1 Cys61Gly")
    assert out["match_mode"] == "and"
    assert {r["variation_id"] for r in out["results"]} == {100002}


async def test_search_has_more_without_relying_on_count(service):
    out = await service.search_variants("BRCA1", limit=2, count_mode="none")
    assert out["total_count"] is None
    assert out["has_more"] is True
    assert out["next_offset"] == 2


async def test_search_reports_capped_total(service):
    out = await service.search_variants("BRCA1", count_mode="exact", limit=2)
    assert out["total_count"] in (5,)  # fixture is small; not capped here
    assert "total_count_capped" not in out  # only present when capped


async def test_gene_summary_buckets_reconcile_to_total(service):
    out = await service.get_gene_clinvar_summary("BRCA1")
    buckets = (
        out["pathogenic_count"]
        + out["likely_pathogenic_count"]
        + out["vus_count"]
        + out["likely_benign_count"]
        + out["benign_count"]
        + out["conflicting_count"]
        + out["not_provided_count"]
        + out["other_count"]
    )
    assert buckets == out["total_count"]
    assert out["other_count"] >= 0


async def test_forced_id_type_mismatch_is_invalid_input(service):
    # A VCV accession forced as variation_id (numeric) must be invalid_input.
    with pytest.raises(ToolInputError):
        await service.get_variant("VCV000100001", id_type="variation_id")
    # An rsID forced as hgvs (needs ":" or hint) must be invalid_input.
    with pytest.raises(ToolInputError):
        await service.get_variant("rs28897672", id_type="hgvs")
