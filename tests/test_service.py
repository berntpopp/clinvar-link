"""Tests for the async ClinVarService orchestration layer."""

from pathlib import Path

import pytest

from clinvar_link.config import Settings
from clinvar_link.data.repository import ClinVarRepository
from clinvar_link.exceptions import DataNotFoundError
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


async def test_response_mode_minimal_vs_full(service):
    mn = await service.get_variant("VCV000100001", response_mode="minimal")
    fl = await service.get_variant("VCV000100001", response_mode="full")
    assert set(mn).issubset(set(fl)) and "coordinates" not in mn and "coordinates" in fl


async def test_search_and_gene(service):
    s = await service.search_variants("BRCA1", limit=5)
    assert s["count"] >= 1 and all("recommended_citation" in r for r in s["results"])
    gs = await service.get_gene_clinvar_summary("BRCA1")
    assert gs["total_count"] >= 1
    bygene = await service.get_variants_by_gene("BRCA1", min_stars=0)
    assert bygene["total"] >= 1 and bygene["gene_symbol"].upper() == "BRCA1"


async def test_meta(service):
    m = await service.get_clinvar_meta()
    assert m["release_date"]
