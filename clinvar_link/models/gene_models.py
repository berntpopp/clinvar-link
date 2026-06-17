"""Pydantic models for gene-level ClinVar summaries."""

from pydantic import BaseModel, ConfigDict, Field


class GeneClinVarSummary(BaseModel):
    """Aggregated ClinVar statistics for a single gene."""

    gene_symbol: str = Field(..., description="HGNC gene symbol.")
    total_count: int = Field(..., description="Total number of ClinVar variants for the gene.")
    pathogenic_count: int = Field(..., description="Count of pathogenic variants.")
    likely_pathogenic_count: int = Field(..., description="Count of likely pathogenic variants.")
    vus_count: int = Field(..., description="Count of variants of uncertain significance.")
    likely_benign_count: int = Field(..., description="Count of likely benign variants.")
    benign_count: int = Field(..., description="Count of benign variants.")
    conflicting_count: int = Field(
        ..., description="Count of variants with conflicting interpretations."
    )
    not_provided_count: int = Field(
        ..., description="Count of variants with no provided classification."
    )
    other_count: int = Field(
        0, description="Variants whose classification falls outside the named buckets."
    )
    has_pathogenic: bool = Field(
        ..., description="Whether the gene has any pathogenic or likely pathogenic variants."
    )
    star_distribution: dict[str, int] = Field(
        default_factory=dict, description="Variant counts keyed by star rating."
    )
    consequence_categories: dict[str, int] = Field(
        default_factory=dict, description="Variant counts keyed by molecular consequence category."
    )
    top_traits: list[dict] = Field(
        default_factory=list, description="Most frequent associated traits with counts."
    )
    recommended_citation: str | None = Field(
        None, description="Recommended citation string for this summary."
    )

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "example": {
                "gene_symbol": "BRCA1",
                "total_count": 8421,
                "pathogenic_count": 1923,
                "likely_pathogenic_count": 412,
                "vus_count": 5210,
                "likely_benign_count": 480,
                "benign_count": 296,
                "conflicting_count": 80,
                "not_provided_count": 20,
                "other_count": 0,
                "has_pathogenic": True,
                "star_distribution": {"0": 120, "1": 5800, "2": 2300, "3": 180, "4": 21},
                "consequence_categories": {"missense variant": 4200, "nonsense": 900},
                "top_traits": [
                    {"name": "Hereditary breast ovarian cancer syndrome", "count": 6100}
                ],
                "recommended_citation": (
                    "ClinVar gene summary for BRCA1. National Center for Biotechnology Information."
                ),
            }
        },
    )
