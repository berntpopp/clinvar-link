"""Tests for ClinVar pydantic models."""

from clinvar_link.models import (
    Assembly,
    Classification,
    ClinVarVariant,
    Coordinate,
    GeneClinVarSummary,
    IdType,
    ResponseMode,
    Trait,
)


def test_clinvar_variant_from_dict_ignores_extra_and_round_trips():
    data = {
        "variation_id": 12345,
        "vcv_accession": "VCV000012345",
        "allele_id": 27392,
        "rsid": 113993960,
        "name": "NM_004985.5(KRAS):c.35G>A (p.Gly12Asp)",
        "gene_symbol": "KRAS",
        "classification": "pathogenic",
        "star_rating": 2,
        "molecular_consequence": ["missense variant"],
        "coordinates": [
            {
                "assembly": "GRCh38",
                "chromosome": "12",
                "start": 25245350,
                "stop": 25245350,
                "reference_allele": "C",
                "alternate_allele": "T",
            }
        ],
        "traits": [
            {
                "name": "Noonan syndrome",
                "omim_id": "163950",
                "medgen_id": "C0028326",
            }
        ],
        # Extra unknown key — must be ignored, not raise.
        "some_unexpected_field": {"nested": "value"},
    }

    variant = ClinVarVariant(**data)

    assert isinstance(variant.coordinates[0], Coordinate)
    assert isinstance(variant.traits[0], Trait)
    assert variant.coordinates[0].assembly == "GRCh38"
    assert variant.traits[0].name == "Noonan syndrome"

    dumped = variant.model_dump()
    assert dumped["variation_id"] == 12345
    assert dumped["vcv_accession"] == "VCV000012345"
    assert dumped["gene_symbol"] == "KRAS"
    assert dumped["star_rating"] == 2
    assert dumped["classification"] == "pathogenic"
    assert "star_rating" in dumped
    assert "classification" in dumped
    # Extra key was ignored, so it must not appear in the dump.
    assert "some_unexpected_field" not in dumped


def test_gene_clinvar_summary():
    summary = GeneClinVarSummary(
        gene_symbol="BRCA1",
        variant_count=8421,
        pathogenic_count=1923,
        likely_pathogenic_count=412,
        vus_count=5210,
        likely_benign_count=480,
        benign_count=296,
        conflicting_count=80,
        not_provided_count=20,
        has_pathogenic=True,
    )

    assert summary.has_pathogenic is True
    assert summary.variant_count == 8421
    assert summary.star_distribution == {}
    assert summary.top_traits == []


def test_classification_enum_values():
    assert Classification.PATHOGENIC.value == "pathogenic"
    assert Classification.LIKELY_PATHOGENIC.value == "likely_pathogenic"
    assert Classification.VUS.value == "vus"
    assert Classification.CONFLICTING.value == "conflicting"
    assert Classification.NOT_PROVIDED.value == "not_provided"
    assert Classification.OTHER.value == "other"


def test_assembly_enum_values():
    assert Assembly.GRCH38.value == "GRCh38"
    assert Assembly.GRCH37.value == "GRCh37"


def test_response_mode_enum_values():
    assert ResponseMode.MINIMAL.value == "minimal"
    assert ResponseMode.COMPACT.value == "compact"
    assert ResponseMode.STANDARD.value == "standard"
    assert ResponseMode.FULL.value == "full"


def test_id_type_enum_values():
    assert IdType.AUTO.value == "auto"
    assert IdType.VCV.value == "vcv"
    assert IdType.VARIATION_ID.value == "variation_id"
    assert IdType.RSID.value == "rsid"
    assert IdType.HGVS.value == "hgvs"
    assert IdType.ALLELE_ID.value == "allele_id"
