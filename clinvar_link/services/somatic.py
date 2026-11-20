"""Shape ClinVar's SOMATIC classifications into response blocks.

ClinVar keeps three independent aggregates per variant (Landrum et al., NAR 2025, PMID 39578691):
the germline classification, the somatic clinical impact (AMP/ASCO/CAP 2017 Tier I-IV) and the
oncogenicity (ClinGen/CGC/VICC 2022). The two somatic ones are emitted as their own blocks,
``somatic_clinical_impact`` and ``oncogenicity``, and are never folded into the germline
``classification``. Tumour-type and drug names are the submitter's text, so both are fenced as
Response-Envelope v1.1 ``untrusted_text``.
"""

from __future__ import annotations

from typing import Any

from clinvar_link.mcp.untrusted_content import UntrustedText, fence_untrusted_text
from clinvar_link.models.variant_models import iso_date
from clinvar_link.services.citation import recommended_citation

# Filter token -> ClinVar's own text ("Tier I" is matched as a prefix of "Tier I - Strong").
TIERS = {"tier_i": "Tier I", "tier_ii": "Tier II", "tier_iii": "Tier III", "tier_iv": "Tier IV"}
ONCOGENICITY = {
    "oncogenic": "Oncogenic",
    "likely_oncogenic": "Likely oncogenic",
    "uncertain_significance": "Uncertain significance",
    "likely_benign": "Likely benign",
    "benign": "Benign",
    "conflicting": "Conflicting classifications of oncogenicity",
}
_BLOCKS = (("somatic_clinical_impact", "clinical_impact"), ("oncogenicity", "oncogenicity"))
_ASSERTION_FIELDS = (
    "scv",
    "submitter",
    "classification",
    "assertion_type",
    "significance",
    "review_status",
    "last_evaluated",
)
DETAIL_MODES = frozenset({"standard", "full"})


def _assertion(rec: dict[str, Any]) -> tuple[dict[str, Any], list[UntrustedText]]:
    scv = rec["scv"]
    out = {key: rec[key] for key in _ASSERTION_FIELDS if rec.get(key) is not None}
    tumours = [
        (fence_untrusted_text(t["name"], source="clinvar", record_id=f"{scv}#tumour:{i}"), t)
        for i, t in enumerate(rec["tumour_types"])
    ]
    drugs = [
        fence_untrusted_text(d, source="clinvar", record_id=f"{scv}#drug:{i}")
        for i, d in enumerate(rec["drugs"])
    ]
    out["tumour_types"] = [
        {"name": obj.model_dump(mode="json"), "medgen_id": t.get("medgen_id")} for obj, t in tumours
    ]
    if drugs:
        out["drugs"] = [d.model_dump(mode="json") for d in drugs]
    return out, [obj for obj, _ in tumours] + drugs


def _names_tumour(rec: dict[str, Any], tumour: str) -> bool:
    """The repository's tumour match: MedGen-name substring or exact MedGen id."""
    return any(
        tumour.casefold() in t["name"].casefold() or t.get("medgen_id") == tumour.upper()
        for t in rec["tumour_types"]
    )


def somatic_blocks(
    flat: dict[str, Any] | None,
    assertions: list[dict[str, Any]],
    mode: str,
    tumour: str | None = None,
) -> tuple[dict[str, Any], list[UntrustedText]]:
    """Return ``{somatic_clinical_impact, oncogenicity}`` (each None when ClinVar has none).

    Every mode carries the aggregate call, review status and date; ``standard`` / ``full`` add the
    per-SCV ``assertions`` (tumour type, therapy, assertion type) from the VCV XML. With a
    ``tumour`` filter only that tumour's assertions are sent; ``other_assertions`` counts the rest.
    """
    flat = flat or {}
    out: dict[str, Any] = {}
    fenced: list[UntrustedText] = []
    for key, prefix in _BLOCKS:
        mine = [a for a in assertions if a["kind"] == prefix]
        if flat.get(prefix) is None and not mine:
            out[key] = None
            continue
        block: dict[str, Any] = {
            "classification": flat.get(prefix),
            "review_status": flat.get(f"{prefix}_review_status"),
            "last_evaluated": iso_date(flat.get(f"{prefix}_last_evaluated")),
        }
        if mode in DETAIL_MODES:
            shown = [a for a in mine if _names_tumour(a, tumour)] if tumour else mine
            if tumour:
                block["other_assertions"] = len(mine) - len(shown)
            shaped = [_assertion(a) for a in shown]
            block["assertions"] = [row for row, _ in shaped]
            fenced.extend(obj for _, objs in shaped for obj in objs)
        out[key] = block
    return out, fenced


def somatic_row(
    rec: dict[str, Any],
    assertions: list[dict[str, Any]],
    release: str | None,
    mode: str,
    tumour: str | None = None,
) -> tuple[dict[str, Any], list[UntrustedText]]:
    """One ``search_somatic_classifications`` row: the variant, its germline call, both blocks."""
    row: dict[str, Any] = {
        "variation_id": rec["variation_id"],
        "vcv_accession": rec["vcv_accession"],
        "gene_symbol": rec["gene_symbol"],
    }
    if mode != "minimal":
        row["name"] = rec["name"]
        row["germline_classification"] = rec["clinical_significance"]
    blocks, fenced = somatic_blocks(rec, assertions, mode, tumour)
    row.update(blocks)
    row["recommended_citation"] = recommended_citation(
        rec["variation_id"], rec["vcv_accession"], release
    )
    return row, fenced
