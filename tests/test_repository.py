"""Tests for the read-only :class:`ClinVarRepository`.

A single fixture database is built once per module from the sample
``variant_summary.txt`` (20 variants across BRCA1/TTN/MLH1/AP5Z1), then queried
through every public repository method.

Concrete identifiers used below come from the fixture:

* BRCA1 ``c.5266dupC`` -> VariationID ``100001``, rsid ``80357906``,
  VCV accession ``VCV000100001`` (and both GRCh38 + GRCh37 coordinates).
"""

from pathlib import Path

import pytest

from clinvar_link.config import Settings
from clinvar_link.data.repository import ClinVarRepository
from clinvar_link.exceptions import ClinVarDataError
from clinvar_link.ingest.builder import build_database

FIXTURE = Path(__file__).parent / "fixtures" / "variant_summary_sample.txt"
FIXTURE_HGVS = Path(__file__).parent / "fixtures" / "hgvs4variation_sample.txt"


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    d = tmp_path_factory.mktemp("repo")
    cfg = Settings(DATA_DIR=d, DB_FILENAME="t.sqlite")
    build_database(cfg, source_path=FIXTURE, last_modified="Mon, 01 Jan 2026 00:00:00 GMT")
    r = ClinVarRepository(cfg.db_path)
    yield r
    r.close()


@pytest.fixture(scope="module")
def repo_with_hgvs(tmp_path_factory):
    """A repository whose index also ingested the hgvs4variation source."""
    d = tmp_path_factory.mktemp("repo_hgvs")
    cfg = Settings(DATA_DIR=d, DB_FILENAME="t.sqlite")
    build_database(
        cfg,
        source_path=FIXTURE,
        hgvs_source_path=FIXTURE_HGVS,
        last_modified="Mon, 01 Jan 2026 00:00:00 GMT",
    )
    r = ClinVarRepository(cfg.db_path)
    yield r
    r.close()


def test_missing_database_raises():
    with pytest.raises(ClinVarDataError):
        ClinVarRepository("/nonexistent.sqlite")


def test_get_by_variation_id(repo):
    v = repo.get_by_variation_id(100001)
    assert v is not None
    assert v["variation_id"] == 100001
    assert v["gene_symbol"] == "BRCA1"
    assert v["classification"] == "pathogenic"
    # JSON columns decode to Python lists.
    assert isinstance(v["traits"], list)
    assert isinstance(v["molecular_consequence"], list)
    assert isinstance(v["rcv_accessions"], list)
    assert v["rcv_accessions"] == ["RCV000100001"]


def test_get_by_variation_id_missing(repo):
    assert repo.get_by_variation_id(999999) is None


def test_get_by_rsid_and_coords(repo):
    v = repo.get_by_rsid(80357906)
    assert v is not None
    assert v["vcv_accession"].startswith("VCV")
    assert v["star_rating"] in range(5)
    assert len(v["coordinates"]) == 2  # both assemblies
    # GRCh38 is ordered first.
    assert v["coordinates"][0]["assembly"] == "GRCh38"
    assert v["coordinates"][1]["assembly"] == "GRCh37"


def test_get_by_rsid_missing(repo):
    assert repo.get_by_rsid(1) is None


def test_get_by_vcv_roundtrip(repo):
    # pick any variant, then re-fetch by its VCV accession
    any_v = repo.search("BRCA1", limit=1)[0]
    again = repo.get_by_vcv(any_v["vcv_accession"])
    assert again["variation_id"] == any_v["variation_id"]


def test_get_by_vcv_accepts_bare_number(repo):
    by_num = repo.get_by_vcv("100001")
    assert by_num is not None
    assert by_num["variation_id"] == 100001
    # Padded accession form resolves to the same record.
    assert repo.get_by_vcv("VCV000100001")["variation_id"] == 100001


def test_get_by_vcv_parse_failure_returns_none(repo):
    assert repo.get_by_vcv("not-an-id") is None


def test_get_by_allele_id(repo):
    # AlleleID 200001 maps to VariationID 100001 in the fixture.
    v = repo.get_by_allele_id(200001)
    assert v is not None
    assert v["variation_id"] == 100001


def test_get_by_hgvs(repo):
    name = repo.get_by_variation_id(100001)["name"]
    v = repo.get_by_hgvs(name)
    assert v is not None and v["variation_id"] == 100001
    # Normalization is case-insensitive.
    assert repo.get_by_hgvs(name.upper())["variation_id"] == 100001
    assert repo.get_by_hgvs("   ") is None


def test_get_by_hgvs_resolves_canonical_nucleotide_after_lean_build(repo):
    # LEAN build (no hgvs4variation source): the canonical nucleotide
    # expression derived from the variant_summary Name still resolves, even
    # though the stored full Name carries the trailing "(p....)" protein suffix.
    assert repo.get_by_hgvs("NM_007294.4(BRCA1):c.5266dupC")["variation_id"] == 100001
    # The ambiguous short protein form is not indexed in the lean build.
    assert repo.get_by_hgvs("p.Gln1756fs") is None


def test_get_by_hgvs_resolves_hgvs4variation_forms(repo_with_hgvs):
    # After ingesting hgvs4variation, get_by_hgvs resolves the full Nucleotide
    # expression and the full protein expression to the VariationID.
    repo = repo_with_hgvs
    assert repo.get_by_hgvs("NM_007294.4(BRCA1):c.5266dupC")["variation_id"] == 100001
    assert repo.get_by_hgvs("NP_009225.1:p.Gln1756fs")["variation_id"] == 100001
    # The bare short forms are ambiguous and no longer indexed -> no resolution.
    assert repo.get_by_hgvs("c.5266dupC") is None
    assert repo.get_by_hgvs("p.Gln1756fs") is None
    # Genomic g. expressions are intentionally not indexed.
    assert repo.get_by_hgvs("NC_000017.11:g.43094464dupG") is None


def test_search_and_gene(repo):
    hits = repo.search("BRCA1", limit=10)
    assert hits and all("variation_id" in h for h in hits)
    g = repo.variants_by_gene("brca1", min_stars=0)
    assert g and g == sorted(g, key=lambda x: -x["star_rating"])


def test_search_filters(repo):
    pathogenic = repo.search("BRCA1", classification="pathogenic")
    assert pathogenic and all(h["classification"] == "pathogenic" for h in pathogenic)

    starred = repo.search("BRCA1", min_stars=3)
    assert starred and all(h["star_rating"] >= 3 for h in starred)

    by_gene = repo.search("c.", gene_symbol="MLH1", limit=50)
    assert by_gene and all(h["gene_symbol"] == "MLH1" for h in by_gene)

    grch37 = repo.search("BRCA1", assembly="GRCh37")
    assert grch37
    for h in grch37:
        assert any(c["assembly"] == "GRCh37" for c in h["coordinates"])


def test_search_empty_query_like_fallback(repo):
    # Empty query exercises the LIKE fallback path; with a gene filter it should
    # still return that gene's variants.
    hits = repo.search("", gene_symbol="TTN", limit=50)
    assert hits and all(h["gene_symbol"] == "TTN" for h in hits)


def test_search_pagination(repo):
    page1 = repo.search("AP5Z1", limit=2, offset=0)
    page2 = repo.search("AP5Z1", limit=2, offset=2)
    assert len(page1) == 2
    ids1 = {h["variation_id"] for h in page1}
    ids2 = {h["variation_id"] for h in page2}
    assert ids1.isdisjoint(ids2)


def test_variants_by_gene_count_and_filters(repo):
    total = repo.count_variants_by_gene("AP5Z1")
    assert total == 5  # five AP5Z1 variants in the fixture
    listed = repo.variants_by_gene("AP5Z1", limit=100)
    assert len(listed) == total

    path_only = repo.count_variants_by_gene("AP5Z1", classification="pathogenic")
    assert 0 < path_only <= total


def test_variants_by_gene_sort_default(repo):
    listed = repo.variants_by_gene("MLH1")
    assert listed == sorted(listed, key=lambda x: -x["star_rating"])


def test_gene_summary_and_meta(repo):
    gs = repo.gene_summary("BRCA1")
    assert gs and gs["total_count"] >= 1
    # Case-insensitive lookup.
    assert repo.gene_summary("brca1")["total_count"] == gs["total_count"]
    assert repo.gene_summary("NOSUCHGENE") is None

    m = repo.meta()
    assert m["clinvar_release_date"]
    assert m["variant_count"] == 20
