"""Capabilities, usage, and license payloads for the ClinVar Link MCP server."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any

from mcp.types import LATEST_PROTOCOL_VERSION as MCP_PROTOCOL_VERSION

from clinvar_link.config import settings
from clinvar_link.data.repository import ClinVarRepository
from clinvar_link.mcp.clinvar_date_cache import get_cached_clinvar_release_date
from clinvar_link.mcp.freshness import clinvar_freshness

RESEARCH_USE_NOTICE = "Research use only; not for clinical decision support."

# The ClinVar bulk release is a moving target; the live date is injected at
# runtime from the local DB meta via the date cache (see clinvar_date_cache).
# This static constant is the "version label" surfaced in _meta before that
# cache has been primed by the first get_server_capabilities call.
CLINVAR_DATA_RELEASE = "unknown"

# Source note echoed in capabilities so callers know this server reads a local
# SQLite index built from NCBI's weekly bulk dump, NOT the eUtils web API.
_DATA_SOURCE_NOTE = (
    "NCBI ClinVar weekly bulk variant_summary.txt; served from a local SQLite index; not eUtils."
)

# The exact tool surface this server exposes (kept in lockstep with the facade).
_TOOLS = [
    "get_server_capabilities",
    "get_variant",
    "get_variants",
    "search_variants",
    "get_gene_clinvar_summary",
    "get_variants_by_gene",
]


def server_version() -> str:
    try:
        return version("clinvar-link")
    except PackageNotFoundError:
        return "unknown"


def get_capabilities_resource() -> dict[str, Any]:
    date = get_cached_clinvar_release_date()
    caps: dict[str, Any] = {
        "server": "clinvar-link",
        "server_version": server_version(),
        "mcp_protocol_version": MCP_PROTOCOL_VERSION,
        # Version label derived from the same live release date; falls back to
        # the static sentinel only before the date cache has been primed.
        "clinvar_release": date or CLINVAR_DATA_RELEASE,
        # Echoes the process-cached live ClinVar release date once the first
        # get_server_capabilities tool call has read it from the DB meta; None
        # until then (the sync resource handler never touches the DB itself).
        "clinvar_release_date": date,
        "research_use_only": True,
        "data_source": _DATA_SOURCE_NOTE,
        "tools": list(_TOOLS),
        "response_modes": ["minimal", "compact", "standard", "full"],
        "sort_options": sorted(ClinVarRepository.SORT_ORDERS),
        "recommended_workflows": [
            "VCV / rsID / HGVS / AlleleID -> get_variant",
            "several identifiers at once -> get_variants (one batched call)",
            "free text / gene + change -> search_variants -> get_variant",
            "gene symbol -> get_gene_clinvar_summary (classification landscape)",
            "gene symbol -> get_variants_by_gene (per-variant ClinVar rows)",
        ],
        "error_codes": [
            "not_found",
            "invalid_input",
            "internal_error",
        ],
        "output_cheatsheet": {
            "classification_field": "classification",
            "raw_clinical_significance_field": "clinical_significance",
            "review_status_field": "review_status",
            "star_rating_field": "star_rating",
            "variant_accession_field": "vcv_accession",
            "variation_id_field": "variation_id",
            "citation_field": "recommended_citation",
            "next_commands_field": "_meta.next_commands",
        },
        "limitations": [
            "Local SQLite index built from the weekly variant_summary bulk file; "
            "it lags the live ClinVar website by up to a week.",
            "Per-submitter conflict detail (submission_summary) is not indexed in v1.",
            "Coordinates follow the bulk file (GRCh37 and GRCh38 rows where present).",
            RESEARCH_USE_NOTICE,
        ],
        "llm_driver_contract": {
            "recommended_entrypoint": "get_server_capabilities",
            "core_workflow_tools": [
                "get_variant",
                "get_variants",
                "search_variants",
                "get_gene_clinvar_summary",
                "get_variants_by_gene",
            ],
        },
        "resources": {
            "clinvar://capabilities": "this capabilities document",
            "clinvar://usage": "compact usage notes",
            "clinvar://license": "data license and canonical citation",
            "clinvar://research-use": "research-use-only notice",
            "clinvar://version": "server + protocol + data-release versions",
        },
    }
    fresh = clinvar_freshness(date, settings.REFRESH_TTL_DAYS) if date else None
    if fresh is not None:
        caps["data_freshness"] = fresh
    return caps


def get_version_resource() -> dict[str, Any]:
    return {
        "server": "clinvar-link",
        "server_version": server_version(),
        "mcp_protocol_version": MCP_PROTOCOL_VERSION,
        "clinvar_release_date": get_cached_clinvar_release_date(),
    }


def get_usage_resource() -> str:
    return (
        "# ClinVar Link MCP Usage\n\n"
        "## Resolve a variant\n"
        "Call `get_variant` with any one of these identifier shapes:\n"
        "- VCV accession (e.g. `VCV000012345`)\n"
        "- dbSNP rsID (e.g. `rs28897696`)\n"
        "- HGVS expression (e.g. `NM_000059.3:c.1234A>G`)\n"
        "- ClinVar AlleleID (integer)\n\n"
        "If the identifier does not resolve, call `search_variants` with the gene "
        "symbol plus the change (or free text) to locate the matching record, then "
        "re-call `get_variant` with the returned `vcv_accession`.\n\n"
        "Resolving several variants at once? Call `get_variants(identifiers=[...])` "
        "to batch the lookups into a single round-trip.\n\n"
        "## Summarize a gene\n"
        "Call `get_gene_clinvar_summary(gene_symbol=...)` for the classification "
        "landscape (counts by clinical significance and review-status star rating). "
        "Call `get_variants_by_gene(gene_symbol=...)` for the per-variant rows; "
        "narrow with the supported filters and raise `limit` only as needed.\n\n"
        "## Response modes\n"
        "`minimal | compact | standard | full`. Compact is the default; start there "
        "and widen to `full` only for debugging or full submitter context.\n\n"
        "## Citation contract\n"
        "Every classification you report MUST cite the ClinVar record "
        "(`vcv_accession`) and "
        "the data release echoed in `_meta.clinvar_release` / "
        "`_meta.clinvar_release_date`. Canonical source: "
        "ClinVar (NCBI). https://www.ncbi.nlm.nih.gov/clinvar/.\n\n"
        f"{RESEARCH_USE_NOTICE}"
    )


def get_license_resource() -> dict[str, Any]:
    return {
        "data_source": "NCBI ClinVar",
        "license": "Public domain (US Government work)",
        "summary": (
            "ClinVar data are produced by the US National Center for Biotechnology "
            "Information (NCBI) and are in the public domain within the United States. "
            "There are no usage restrictions on the data itself; NCBI requests "
            "attribution and accurate citation of the data version."
        ),
        "attribution": "National Center for Biotechnology Information (NCBI), ClinVar.",
        "citation": "ClinVar (NCBI). https://www.ncbi.nlm.nih.gov/clinvar/",
        "homepage": "https://www.ncbi.nlm.nih.gov/clinvar/",
        "clinvar_release": get_cached_clinvar_release_date() or CLINVAR_DATA_RELEASE,
        "clinvar_release_date": get_cached_clinvar_release_date(),
        "data_source_note": _DATA_SOURCE_NOTE,
        "research_use_only": True,
        "notice": RESEARCH_USE_NOTICE,
    }
