"""Hand-authored FastMCP facade for ClinVar Link."""

from __future__ import annotations

from collections.abc import Callable

from fastmcp import FastMCP

from clinvar_link import __version__
from clinvar_link.mcp.errors import install_validation_error_handler
from clinvar_link.mcp.log_filters import install_external_error_filter
from clinvar_link.mcp.output_validation import (
    install_output_validation_error_handler,
    install_protocol_error_handler,
)
from clinvar_link.mcp.prompts import register_workflow_prompts
from clinvar_link.mcp.resources import RESEARCH_USE_NOTICE
from clinvar_link.mcp.tools import register_clinvar_tools
from clinvar_link.services import ClinVarService

_INSTRUCTIONS = (
    "ClinVar Link grounds variant-classification work in NCBI ClinVar.\n"
    "- Resolve a variant: get_variant accepts a VCV accession, dbSNP rsID, HGVS "
    "expression, ClinVar AlleleID, or VariationID and returns the normalized "
    "classification plus a 0-4 star rating (review-status confidence). A clean "
    "transcript-qualified HGVS resolves even without the (GENE) qualifier.\n"
    "- Resolve several at once: get_variants(identifiers=[...]) batches the "
    "lookups into a single call instead of looping get_variant.\n"
    "- Locate a record from loose text: search_variants(query=...) with optional "
    "gene_symbol / classification / min_stars filters, then re-call get_variant "
    "with the returned vcv_accession.\n"
    "- Gene landscape: get_gene_clinvar_summary(gene_symbol=...) for counts by "
    "clinical significance and star rating; get_variants_by_gene(gene_symbol=...) "
    "for the per-variant rows.\n"
    "- Response modes: minimal | compact | standard | full; compact is the "
    "default. Start compact and widen only when you need more detail.\n"
    "- Citation contract: every result carries a recommended_citation and the "
    "ClinVar release date in _meta.clinvar_release_date; "
    "cite both when reporting a classification.\n"
    "- Chaining: every response carries _meta.next_commands, a ready-to-call list "
    "of {tool, arguments} next steps (on success and error); execute the first "
    "entry to advance without guessing the next tool.\n"
    "- Discovery: call get_server_capabilities or read clinvar://capabilities for "
    "the tool surface, error taxonomy, and limitations. "
    f"{RESEARCH_USE_NOTICE}"
)


def create_clinvar_mcp(
    *,
    service_factory: Callable[[], ClinVarService],
) -> FastMCP:
    """Build the ClinVar Link MCP server.

    `service_factory` is a lazy callable so HTTP mode can defer to
    `app.state.clinvar_service` and stdio mode can hold a directly
    constructed instance.
    """

    mcp = FastMCP(
        name="clinvar-link",
        version=__version__,
        instructions=_INSTRUCTIONS,
        mask_error_details=True,
        # TOOL-SURFACE-BUDGET-STANDARD v1, Rule 4: the constructor defaults this to True and
        # appends DereferenceRefsMiddleware, which inlines every $defs/$ref at every use site.
        # Free to turn off and safe (no INPUT schema here contains a $ref).
        dereference_schemas=False,
    )
    register_clinvar_tools(mcp, service_factory=service_factory)
    register_workflow_prompts(mcp)
    install_validation_error_handler(mcp)
    install_output_validation_error_handler(mcp)
    # Outermost on CallToolRequest: masks FastMCP core not-found dispatch errors
    # (unknown tool/resource/prompt) that would otherwise echo the caller name/URI.
    install_protocol_error_handler(mcp)
    # Layer 5: scrub the caller-supplied name/URI (and its forbidden code points)
    # out of FastMCP/mcp framework LOG records — the pre-middleware DEBUG source
    # loggers ("Handler called: …", "Tool cache miss for …") and the session-layer
    # ROOT "Failed to validate request: <malformed URI>" record — at ALL levels.
    # Installed after the framework handlers exist so its non-propagating Rich sinks
    # are also covered.
    install_external_error_filter()
    return mcp
