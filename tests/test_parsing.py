"""Tests for clinvar_link.ingest.parsing — pure ClinVar parsing utilities."""

from clinvar_link.ingest.parsing import (
    GeneAccumulator,
    format_accession,
    infer_effect_category,
    load_star_map,
    map_classification,
    map_star_rating,
    parse_protein_change,
    parse_protein_position,
    parse_variant_row,
)


def test_map_classification():
    assert map_classification("Pathogenic/Likely pathogenic") == "likely_pathogenic"
    assert map_classification("Conflicting classifications of pathogenicity") == "conflicting"
    assert map_classification("Benign") == "benign"
    assert map_classification("Likely benign") == "likely_benign"
    assert map_classification("Uncertain significance") == "vus"
    assert map_classification("") == "not_provided"
    assert map_classification("Pathogenic") == "pathogenic"


def test_format_accession():
    assert format_accession("12345") == "VCV000012345"


def test_load_and_map_star_rating():
    sm = load_star_map()
    assert map_star_rating("reviewed by expert panel", sm) == 3
    assert map_star_rating("practice guideline", sm) == 4
    assert map_star_rating("criteria provided, multiple submitters, no conflicts", sm) == 2
    assert map_star_rating("criteria provided, single submitter", sm) == 1
    assert map_star_rating("no assertion criteria provided", sm) == 0
    assert map_star_rating("totally unknown status", sm) == 0


def test_map_star_rating_empty_and_dash():
    sm = load_star_map()
    assert map_star_rating("", sm) == 0
    assert map_star_rating("-", sm) == 0


def test_parse_protein_change():
    name = "NM_014855.3(AP5Z1):c.80_83delinsTGCTGTAAACTGTAACTGTAAA (p.Arg27_Ile28delinsLeuLeuTer)"
    assert "p.Arg27" in parse_protein_change(name)


def test_parse_protein_position():
    assert parse_protein_position("p.Arg4206Trp") == 4206


def test_infer_effect_category():
    frameshift_name = "NM_000000.1(GENE):c.100del (p.Arg34GlyfsTer10)"
    assert infer_effect_category(frameshift_name) == "truncating"
    assert infer_effect_category("NM_x:c.80+1G>A") == "splice_region"


def _build_row() -> dict[str, str]:
    header = [
        "#AlleleID",
        "Type",
        "Name",
        "GeneID",
        "GeneSymbol",
        "HGNC_ID",
        "ClinicalSignificance",
        "ClinSigSimple",
        "LastEvaluated",
        "RS# (dbSNP)",
        "nsv/esv (dbVar)",
        "RCVaccession",
        "PhenotypeIDS",
        "PhenotypeList",
        "Origin",
        "OriginSimple",
        "Assembly",
        "ChromosomeAccession",
        "Chromosome",
        "Start",
        "Stop",
        "ReferenceAllele",
        "AlternateAllele",
        "Cytogenetic",
        "ReviewStatus",
        "NumberSubmitters",
        "Guidelines",
        "TestedInGTR",
        "OtherIDs",
        "SubmitterCategories",
        "VariationID",
        "PositionVCF",
        "ReferenceAlleleVCF",
        "AlternateAlleleVCF",
    ]
    values = [
        "15041",
        "Indel",
        "NM_014855.3(AP5Z1):c.80_83delinsTGCTGTAAACTGTAACTGTAAA (p.Arg27_Ile28delinsLeuLeuTer)",
        "9907",
        "AP5Z1",
        "HGNC:22197",
        "Pathogenic/Likely pathogenic",
        "1",
        "Dec 17, 2024",
        "397704705",
        "-",
        "RCV000000012|RCV005255549",
        "MONDO:0013342",
        "Hereditary spastic paraplegia 48|not provided",
        "germline",
        "germline",
        "GRCh37",
        "NC_000007.13",
        "7",
        "4820844",
        "4820847",
        "na",
        "na",
        "7p22.1",
        "criteria provided, multiple submitters, no conflicts",
        "4",
        "-",
        "N",
        "ClinGen:CA215070",
        "3",
        "15041",
        "4820844",
        "GGAT",
        "TGCTGTAAACTGTAACTGTAAA",
    ]
    return dict(zip(header, values, strict=True))


def test_parse_variant_row():
    sm = load_star_map()
    row = _build_row()
    r = parse_variant_row(row, sm)
    assert r["variation_id"] == "15041"
    assert r["accession"] == "VCV000015041"
    assert r["classification"] == "likely_pathogenic"
    assert r["star_rating"] == 2
    assert r["rsid"] == 397704705
    assert r["gene_symbol"] == "AP5Z1"
    assert r["assembly"] == "GRCh37"
    assert r["start"] == 4820844
    assert len(r["traits"]) >= 1
    trait_names = [t["name"] for t in r["traits"]]
    assert "not provided" not in trait_names
    assert r["rcv_accessions"] == ["RCV000000012", "RCV005255549"]


def test_parse_variant_row_allele_id_variants():
    sm = load_star_map()
    row = _build_row()
    r = parse_variant_row(row, sm)
    assert r["allele_id"] == "15041"

    # Also works when the header has "AlleleID" (no leading #).
    row2 = _build_row()
    row2["AlleleID"] = row2.pop("#AlleleID")
    r2 = parse_variant_row(row2, sm)
    assert r2["allele_id"] == "15041"


def test_gene_accumulator():
    acc = GeneAccumulator(load_star_map())
    acc.add_variant(
        {
            "classification": "pathogenic",
            "review_status": "reviewed by expert panel",
            "star_rating": 3,
            "gene_symbol": "TEST",
            "molecular_consequences": ["nonsense"],
            "variant_type": "single nucleotide variant",
            "protein_change": "p.Arg34Ter",
            "traits": [{"name": "disease one"}],
        }
    )
    acc.add_variant(
        {
            "classification": "vus",
            "review_status": "criteria provided, single submitter",
            "star_rating": 1,
            "gene_symbol": "TEST",
            "molecular_consequences": ["missense variant"],
            "variant_type": "single nucleotide variant",
            "protein_change": "p.Arg40Gly",
            "traits": [{"name": "disease two"}],
        }
    )
    acc.add_variant(
        {
            "classification": "benign",
            "review_status": "criteria provided, single submitter",
            "star_rating": 1,
            "gene_symbol": "TEST",
            "molecular_consequences": ["synonymous variant"],
            "variant_type": "single nucleotide variant",
            "protein_change": "p.Arg50=",
            "traits": [{"name": "disease three"}],
        }
    )
    stats = acc.finalize()
    assert stats["total_count"] == 3
    assert stats["pathogenic_count"] == 1
    assert stats["has_pathogenic"] is True
    assert stats["star_distribution"][3] == 1

    # Per-variant detail lists are no longer emitted (they were never read).
    assert "protein_variants" not in stats
    assert "genomic_variants" not in stats

    # Retained aggregate fields the service reads stay present.
    for field in (
        "total_count",
        "pathogenic_count",
        "likely_pathogenic_count",
        "vus_count",
        "benign_count",
        "likely_benign_count",
        "conflicting_count",
        "not_provided_count",
        "high_confidence_count",
        "variant_type_counts",
        "molecular_consequences",
        "top_molecular_consequences",
        "consequence_categories",
        "star_distribution",
        "top_traits",
        "high_confidence_percentage",
        "pathogenic_percentage",
        "has_pathogenic",
    ):
        assert field in stats


def test_finalize_other_count_catches_unbucketed():
    acc = GeneAccumulator(load_star_map())
    acc.add_variant({"classification": "Pathogenic", "star_rating": 2})
    acc.add_variant({"classification": "risk factor", "star_rating": 1})  # outside named buckets
    stats = acc.finalize()
    known = (
        stats["pathogenic_count"]
        + stats["likely_pathogenic_count"]
        + stats["vus_count"]
        + stats["benign_count"]
        + stats["likely_benign_count"]
        + stats["conflicting_count"]
        + stats["not_provided_count"]
    )
    assert stats["other_count"] == stats["total_count"] - known
    assert stats["other_count"] == 1
