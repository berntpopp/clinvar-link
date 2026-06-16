"""Pydantic models for ClinVar variant entities."""

from pydantic import BaseModel, ConfigDict, Field


class Coordinate(BaseModel):
    """Genomic coordinate for a ClinVar variant on a single assembly."""

    assembly: str = Field(..., description="Reference genome assembly (e.g., 'GRCh38').")
    chromosome_accession: str | None = Field(
        None, description="RefSeq chromosome accession (e.g., 'NC_000017.11')."
    )
    chromosome: str | None = Field(None, description="Chromosome name (e.g., '17').")
    start: int | None = Field(None, description="Start position (1-based).")
    stop: int | None = Field(None, description="Stop position (1-based).")
    reference_allele: str | None = Field(None, description="Reference allele (ClinVar style).")
    alternate_allele: str | None = Field(None, description="Alternate allele (ClinVar style).")
    position_vcf: int | None = Field(None, description="VCF-style position.")
    reference_allele_vcf: str | None = Field(None, description="VCF-style reference allele.")
    alternate_allele_vcf: str | None = Field(None, description="VCF-style alternate allele.")


class Trait(BaseModel):
    """A condition / phenotype associated with a ClinVar variant."""

    name: str = Field(..., description="Trait (condition) name.")
    omim_id: str | None = Field(None, description="OMIM identifier, when available.")
    medgen_id: str | None = Field(None, description="MedGen concept ID, when available.")
    mondo_id: str | None = Field(None, description="MONDO identifier, when available.")


class ClinVarVariant(BaseModel):
    """A normalized ClinVar variant record."""

    variation_id: int = Field(..., description="ClinVar VariationID.")
    vcv_accession: str = Field(..., description="ClinVar VCV accession (e.g., 'VCV000012345').")
    allele_id: int | None = Field(None, description="ClinVar AlleleID.")
    rsid: int | None = Field(None, description="dbSNP rsID (numeric, without 'rs' prefix).")
    name: str | None = Field(None, description="Preferred variant name / HGVS expression.")
    gene_symbol: str | None = Field(None, description="HGNC gene symbol.")
    gene_id: str | None = Field(None, description="NCBI Gene ID.")
    hgnc_id: str | None = Field(None, description="HGNC identifier.")
    variant_type: str | None = Field(
        None, description="Variant type (e.g., 'single nucleotide variant')."
    )
    clinical_significance: str | None = Field(
        None, description="Raw ClinVar clinical significance description."
    )
    classification: str = Field(..., description="Normalized classification category.")
    review_status: str | None = Field(None, description="ClinVar review status text.")
    star_rating: int = Field(..., description="Review confidence star rating (0-4).")
    protein_change: str | None = Field(
        None, description="Protein-level change (e.g., 'p.Val600Glu')."
    )
    cdna_change: str | None = Field(None, description="cDNA-level change (e.g., 'c.1799T>A').")
    molecular_consequence: list[str] = Field(
        default_factory=list, description="Molecular consequence terms."
    )
    traits: list[Trait] = Field(default_factory=list, description="Associated conditions.")
    rcv_accessions: list[str] = Field(
        default_factory=list, description="Associated RCV accessions."
    )
    number_submitters: int | None = Field(None, description="Number of submitters.")
    last_evaluated: str | None = Field(None, description="Date the variant was last evaluated.")
    origin: str | None = Field(None, description="Allele origin (e.g., 'germline').")
    canonical_assembly: str | None = Field(
        None, description="Preferred assembly for canonical coordinates."
    )
    coordinates: list[Coordinate] = Field(
        default_factory=list, description="Genomic coordinates across assemblies."
    )
    recommended_citation: str | None = Field(
        None, description="Recommended citation string for this record."
    )

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "example": {
                "variation_id": 12345,
                "vcv_accession": "VCV000012345",
                "allele_id": 27392,
                "rsid": 113993960,
                "name": "NM_004985.5(KRAS):c.35G>A (p.Gly12Asp)",
                "gene_symbol": "KRAS",
                "gene_id": "3845",
                "hgnc_id": "HGNC:6407",
                "variant_type": "single nucleotide variant",
                "clinical_significance": "Pathogenic",
                "classification": "pathogenic",
                "review_status": "criteria provided, multiple submitters, no conflicts",
                "star_rating": 2,
                "protein_change": "p.Gly12Asp",
                "cdna_change": "c.35G>A",
                "molecular_consequence": ["missense variant"],
                "traits": [
                    {
                        "name": "Noonan syndrome",
                        "omim_id": "163950",
                        "medgen_id": "C0028326",
                        "mondo_id": "MONDO:0018997",
                    }
                ],
                "rcv_accessions": ["RCV000037557"],
                "number_submitters": 5,
                "last_evaluated": "2023-01-15",
                "origin": "germline",
                "canonical_assembly": "GRCh38",
                "coordinates": [
                    {
                        "assembly": "GRCh38",
                        "chromosome_accession": "NC_000012.12",
                        "chromosome": "12",
                        "start": 25245350,
                        "stop": 25245350,
                        "reference_allele": "C",
                        "alternate_allele": "T",
                        "position_vcf": 25245350,
                        "reference_allele_vcf": "C",
                        "alternate_allele_vcf": "T",
                    }
                ],
                "recommended_citation": (
                    "ClinVar VCV000012345. National Center for Biotechnology Information."
                ),
            }
        },
    )
