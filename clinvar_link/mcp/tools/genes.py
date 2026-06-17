"""Gene-scoped tools: get_gene_clinvar_summary, get_variants_by_gene."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from clinvar_link.mcp.annotations import READ_ONLY_OPEN_WORLD
from clinvar_link.mcp.errors import McpErrorContext, run_mcp_tool
from clinvar_link.services import ClinVarService


def register_gene_tools(mcp: FastMCP, *, service_factory: Callable[[], ClinVarService]) -> None:
    """Register the gene-level ClinVar summary and per-variant listing tools."""

    @mcp.tool(
        name="get_gene_clinvar_summary",
        title="Get Gene ClinVar Summary",
        annotations=READ_ONLY_OPEN_WORLD,
        tags={"gene"},
    )
    async def get_gene_clinvar_summary(
        gene_symbol: str,
        response_mode: str = "compact",
        request_id: str | None = None,
    ) -> dict[str, Any]:
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
    )
    async def get_variants_by_gene(
        gene_symbol: str,
        classification: str | None = None,
        min_stars: int | None = None,
        sort: str = "stars_desc",
        limit: Annotated[int, Field(ge=1)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
        response_mode: str = "compact",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """List the ClinVar variants for a gene as per-variant rows (each with classification and star rating). Use this after get_gene_clinvar_summary to enumerate individual records; narrow with classification / min_stars and paginate with limit / offset (response carries total_count / has_more / next_offset). Default sort is stars_desc (highest review confidence first). In minimal/compact mode the citation is hoisted once to _meta.citation_template instead of repeated per row."""

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
