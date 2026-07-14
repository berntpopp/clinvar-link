"""Gene-scoped tools: get_gene_clinvar_summary, get_variants_by_gene.

`get_variants_by_gene.classification` is the parameter that hid 559 pathogenic BRCA1 variants
behind a capitalization difference. Its vocabulary is now DECLARED as an enum in the schema
(:mod:`clinvar_link.mcp.params`) and enforced at runtime (:mod:`clinvar_link.services`), so a
model can neither guess wrong nor be told, confidently, that there are none.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult

from clinvar_link.mcp.annotations import READ_ONLY_OPEN_WORLD
from clinvar_link.mcp.errors import McpErrorContext, run_mcp_tool
from clinvar_link.mcp.params import (
    ClassificationFilter,
    GeneSymbol,
    Limit,
    MinStars,
    Offset,
    RequestId,
    ResponseModeParam,
    SortParam,
)
from clinvar_link.services import ClinVarService

# A SUCCESS returns the envelope dict; a FAILURE returns a ToolResult carrying the same
# envelope plus protocol isError:true (Response-Envelope Standard v1).
type ToolReturn = dict[str, Any] | ToolResult


def register_gene_tools(mcp: FastMCP, *, service_factory: Callable[[], ClinVarService]) -> None:
    """Register the gene-level ClinVar summary and per-variant listing tools."""

    @mcp.tool(
        name="get_gene_clinvar_summary",
        title="Get Gene ClinVar Summary",
        annotations=READ_ONLY_OPEN_WORLD,
        tags={"gene"},
        output_schema=None,
    )
    async def get_gene_clinvar_summary(
        gene_symbol: GeneSymbol,
        response_mode: ResponseModeParam = "compact",
        request_id: RequestId = None,
    ) -> ToolReturn:
        """Summarize a gene's ClinVar variant landscape: counts by clinical significance (pathogenic, likely pathogenic, VUS, benign, conflicting) and by review-status star rating, plus top associated traits. Use this for a gene-level overview before drilling into individual variants with get_variants_by_gene. Returns a recommended_citation."""

        async def _call() -> dict[str, Any]:
            result = await service_factory().get_gene_clinvar_summary(
                gene_symbol, response_mode=response_mode
            )
            symbol = result.get("gene_symbol", gene_symbol)
            result.setdefault("_meta", {})["next_commands"] = [
                {
                    "tool": "get_variants_by_gene",
                    "arguments": {"gene_symbol": symbol},
                },
                {"tool": "get_server_capabilities", "arguments": {}},
            ]
            return result

        return await run_mcp_tool(
            "get_gene_clinvar_summary",
            _call,
            context=McpErrorContext(
                tool_name="get_gene_clinvar_summary",
                gene_symbol=gene_symbol,
                request_id=request_id,
            ),
        )

    @mcp.tool(
        name="get_variants_by_gene",
        title="Get ClinVar Variants by Gene",
        annotations=READ_ONLY_OPEN_WORLD,
        tags={"gene"},
        output_schema=None,
    )
    async def get_variants_by_gene(
        gene_symbol: GeneSymbol,
        classification: ClassificationFilter = None,
        min_stars: MinStars = None,
        sort: SortParam = "stars_desc",
        limit: Limit = 50,
        offset: Offset = 0,
        response_mode: ResponseModeParam = "compact",
        request_id: RequestId = None,
    ) -> ToolReturn:
        """List the ClinVar variants for a gene as per-variant rows (each with classification and star rating). Use this after get_gene_clinvar_summary to enumerate individual records; narrow with classification / min_stars and paginate with limit / offset (response carries total_count / has_more / next_offset). classification takes the normalized tokens in its enum — ClinVar's own wording ("Likely pathogenic") is accepted and normalized, and any unrecognized value is REJECTED rather than silently returning zero rows. Default sort is stars_desc (highest review confidence first). In minimal/compact mode the citation is hoisted once to _meta.citation_template instead of repeated per row."""

        async def _call() -> dict[str, Any]:
            result = await service_factory().get_variants_by_gene(
                gene_symbol,
                classification=classification,
                min_stars=min_stars,
                sort=sort,
                limit=limit,
                offset=offset,
                response_mode=response_mode,
            )
            symbol = result.get("gene_symbol", gene_symbol)
            results = result.get("results") or []
            next_commands: list[dict[str, Any]] = []
            if results:
                vcv = results[0].get("vcv_accession")
                if vcv:
                    next_commands.append({"tool": "get_variant", "arguments": {"identifier": vcv}})
            next_commands.append(
                {
                    "tool": "get_gene_clinvar_summary",
                    "arguments": {"gene_symbol": symbol},
                }
            )
            next_commands.append({"tool": "get_server_capabilities", "arguments": {}})
            result.setdefault("_meta", {})["next_commands"] = next_commands
            return result

        return await run_mcp_tool(
            "get_variants_by_gene",
            _call,
            context=McpErrorContext(
                tool_name="get_variants_by_gene",
                gene_symbol=gene_symbol,
                request_id=request_id,
            ),
        )
