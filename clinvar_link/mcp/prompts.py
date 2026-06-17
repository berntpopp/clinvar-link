"""Canonical workflow prompts for ClinVar Link MCP."""

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

# Local, self-contained patterns so this module does not depend on the tool
# modules (registered in a later task). Kept loose on purpose: get_variant
# accepts several identifier shapes (VCV / rsID / HGVS / AlleleID).
_VARIANT_QUERY_PATTERN = r"^.{1,256}$"
_GENE_SYMBOL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9\-\.]{0,31}$"


def register_workflow_prompts(mcp: FastMCP) -> None:
    """Register canonical workflow prompts that guide LLM callers through tool chains."""

    @mcp.prompt(name="variant_classification_workflow")
    def variant_classification_workflow(
        variant: Annotated[
            str,
            Field(
                pattern=_VARIANT_QUERY_PATTERN,
                description=(
                    "A variant identifier: VCV accession, dbSNP rsID, HGVS expression, "
                    "or ClinVar AlleleID."
                ),
            ),
        ],
    ) -> str:
        """Resolve a variant to its ClinVar classification and star rating."""
        return (
            f"ClinVar classification workflow for {variant}:\n"
            "1. Call get_variant(identifier='{variant}') to fetch the ClinVar record: "
            "classification (normalized), review_status, and star_rating "
            "(0-4 stars).\n"
            "2. If not_found or invalid_input, call search_variants(query='{variant}') "
            "to locate the matching record, then re-call get_variant with the returned "
            "vcv_accession.\n"
            "3. Report the classification together with the review status and star "
            "rating; a higher star rating reflects stronger review-status support.\n"
            "4. Cite the vcv_accession and the clinvar_release / clinvar_release_date "
            "from _meta. Canonical source: ClinVar (NCBI). "
            "https://www.ncbi.nlm.nih.gov/clinvar/.\n"
            "IMPORTANT: ClinVar entries reflect submitter assertions and are not a "
            "clinical diagnosis. Research use only; not for clinical decision support."
        ).replace("{variant}", variant)

    @mcp.prompt(name="gene_clinvar_landscape_workflow")
    def gene_clinvar_landscape_workflow(
        gene_symbol: Annotated[
            str,
            Field(
                pattern=_GENE_SYMBOL_PATTERN,
                description=f"HGNC gene symbol matching {_GENE_SYMBOL_PATTERN}.",
            ),
        ],
    ) -> str:
        """Summarize a gene's ClinVar variant landscape."""
        return (
            f"ClinVar gene landscape workflow for {gene_symbol}:\n"
            "1. Call get_gene_clinvar_summary(gene_symbol='{gene_symbol}') for the "
            "classification landscape: counts by clinical_significance and by "
            "review-status star rating.\n"
            "2. Call get_variants_by_gene(gene_symbol='{gene_symbol}') to enumerate the "
            "per-variant rows; narrow with the supported filters (e.g. clinical "
            "significance) and raise limit only as needed.\n"
            "3. For any individual variant of interest, call "
            "get_variant(identifier='<vcv_accession>') for the full record.\n"
            "4. Cite the gene symbol, the relevant vcv_accessions, and the "
            "clinvar_release / clinvar_release_date from _meta. Canonical source: "
            "ClinVar (NCBI). "
            "https://www.ncbi.nlm.nih.gov/clinvar/.\n"
            "Research use only; not for clinical decision support."
        ).replace("{gene_symbol}", gene_symbol)
