"""Recommended-citation builders for ClinVar variant and gene responses.

These produce the stable, paste-verbatim attribution strings attached to
variant and gene-summary payloads, linking back to the public NCBI ClinVar
pages and (when known) the weekly release the local index was built from.
"""

from __future__ import annotations


def recommended_citation(variation_id: int, vcv_accession: str, release_date: str | None) -> str:
    """Build the recommended citation for a single ClinVar variant."""
    rel = f" ClinVar weekly release {release_date}." if release_date else ""
    return (
        f"ClinVar (NCBI). VariationID {variation_id} ({vcv_accession})."
        f"{rel} https://www.ncbi.nlm.nih.gov/clinvar/variation/{variation_id}/"
    )


def citation_template(release_date: str | None) -> str:
    """Build a per-list citation template the client fills from each row's IDs.

    Lifting one template into ``_meta`` (instead of repeating the full
    recommended_citation on every row) is the largest avoidable token sink in
    list responses. The ``{variation_id}`` / ``{vcv_accession}`` placeholders are
    left literal for the caller to substitute from the per-row fields.
    """
    rel = f" ClinVar weekly release {release_date}." if release_date else ""
    return (
        "ClinVar (NCBI). VariationID {variation_id} ({vcv_accession})."
        + rel
        + " https://www.ncbi.nlm.nih.gov/clinvar/variation/{variation_id}/"
    )


def gene_citation(gene_symbol: str, release_date: str | None) -> str:
    """Build the recommended citation for a gene-level ClinVar summary."""
    rel = f" ClinVar weekly release {release_date}." if release_date else ""
    return (
        f"ClinVar (NCBI), gene {gene_symbol}.{rel} "
        f"https://www.ncbi.nlm.nih.gov/clinvar/?term={gene_symbol}%5Bgene%5D"
    )
