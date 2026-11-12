"""Shape indexed submissions (OMIM SCVs from the VCV XML, #122) into response rows.

The description and condition names are the SUBMITTER's free text, so both are fenced as
Response-Envelope v1.1 ``untrusted_text``; ``minimal`` drops them (identifiers + PMIDs only).
"""

from __future__ import annotations

from typing import Any

from clinvar_link.mcp.untrusted_content import UntrustedText, fence_untrusted_text
from clinvar_link.services.citation import recommended_citation


def submission_row(
    sub: dict[str, Any], release: str | None, mode: str
) -> tuple[dict[str, Any], list[UntrustedText]]:
    """Return one projected submission row and the fenced objects it carries."""
    scv = sub["scv"]
    row: dict[str, Any] = {
        "scv": scv,
        "scv_version": sub["scv_version"],
        "submitter": sub["submitter"],
        "variation_id": sub["variation_id"],
        "vcv_accession": sub["vcv_accession"],
        "pmids": sub["pmids"],
    }
    fenced: list[UntrustedText] = []
    if mode != "minimal":
        row["variant_name"] = sub["variation_name"]
        row["submitted_gene_symbols"] = sub["submitted_genes"]
        conditions = [
            fence_untrusted_text(name, source="clinvar", record_id=f"{scv}#condition:{i}")
            for i, name in enumerate(sub["conditions"])
        ]
        description = (
            fence_untrusted_text(
                sub["description"], source="clinvar", record_id=f"{scv}#description"
            )
            if sub["description"]
            else None
        )
        fenced = [*conditions, *([description] if description else [])]
        row["conditions"] = [c.model_dump(mode="json") for c in conditions]
        row["description"] = description.model_dump(mode="json") if description else None
    row["recommended_citation"] = recommended_citation(
        sub["variation_id"], sub["vcv_accession"], release
    )
    return row, fenced
