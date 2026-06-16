"""Variant tools: get_variant, search_variants."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP

from clinvar_link.mcp.annotations import READ_ONLY_OPEN_WORLD
from clinvar_link.mcp.errors import McpErrorContext, run_mcp_tool
from clinvar_link.services import ClinVarService


def register_variant_tools(mcp: FastMCP, *, service_factory: Callable[[], ClinVarService]) -> None:
    """Register the single-variant resolution and free-text search tools."""

    @mcp.tool(
        name="get_variant",
        title="Get ClinVar Variant",
        annotations=READ_ONLY_OPEN_WORLD,
        tags={"variant"},
    )
    async def get_variant(
        identifier: str,
        id_type: str = "auto",
        response_mode: str = "compact",
    ) -> dict[str, Any]:
        """Resolve a single ClinVar variant by VCV accession, dbSNP rsID, HGVS expression, ClinVar AlleleID, or VariationID. Returns the normalized classification, review status, and 0-4 star rating plus a recommended_citation. Use this when you already have a variant identifier; if it fails to resolve, fall back to search_variants. id_type='auto' (default) detects the shape; response_mode trims payload size (minimal | compact | standard | full)."""

        async def _call() -> dict[str, Any]:
            result = await service_factory().get_variant(
                identifier, id_type=id_type, response_mode=response_mode
            )
            gene_symbol = result.get("gene_symbol")
            next_commands: list[dict[str, Any]] = []
            if gene_symbol:
                next_commands.append(
                    {
                        "tool": "get_gene_clinvar_summary",
                        "arguments": {"gene_symbol": gene_symbol},
                    }
                )
                next_commands.append(
                    {
                        "tool": "get_variants_by_gene",
                        "arguments": {"gene_symbol": gene_symbol},
                    }
                )
            next_commands.append({"tool": "get_server_capabilities", "arguments": {}})
            result.setdefault("_meta", {})["next_commands"] = next_commands
            return result

        return await run_mcp_tool(
            "get_variant",
            _call,
            context=McpErrorContext(tool_name="get_variant", variant_id=identifier),
        )

    @mcp.tool(
        name="search_variants",
        title="Search ClinVar Variants",
        annotations=READ_ONLY_OPEN_WORLD,
        tags={"variant"},
    )
    async def search_variants(
        query: str,
        gene_symbol: str | None = None,
        classification: str | None = None,
        min_stars: int | None = None,
        assembly: str | None = None,
        limit: int = 20,
        offset: int = 0,
        response_mode: str = "compact",
    ) -> dict[str, Any]:
        """Free-text search across ClinVar variant names, genes, and identifiers. Use this to locate a record when you only have a gene symbol plus a change or other loose text, then re-call get_variant with the returned vcv_accession. Optional filters: gene_symbol, classification, min_stars, assembly. Returns a paginated results list with recommended_citation per hit."""

        async def _call() -> dict[str, Any]:
            result = await service_factory().search_variants(
                query,
                gene_symbol=gene_symbol,
                classification=classification,
                min_stars=min_stars,
                assembly=assembly,
                limit=limit,
                offset=offset,
                response_mode=response_mode,
            )
            results = result.get("results") or []
            next_commands: list[dict[str, Any]] = []
            if results:
                first = results[0]
                vcv = first.get("vcv_accession")
                if vcv:
                    next_commands.append({"tool": "get_variant", "arguments": {"identifier": vcv}})
                first_gene = first.get("gene_symbol")
                if first_gene:
                    next_commands.append(
                        {
                            "tool": "get_gene_clinvar_summary",
                            "arguments": {"gene_symbol": first_gene},
                        }
                    )
            next_commands.append({"tool": "get_server_capabilities", "arguments": {}})
            result.setdefault("_meta", {})["next_commands"] = next_commands
            return result

        return await run_mcp_tool(
            "search_variants",
            _call,
            context=McpErrorContext(
                tool_name="search_variants", query=query, gene_symbol=gene_symbol
            ),
        )
