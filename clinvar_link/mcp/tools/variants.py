"""Variant tools: get_variant, search_variants."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

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
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a single ClinVar variant by VCV accession, dbSNP rsID, HGVS expression, ClinVar AlleleID, or VariationID. A clean transcript-qualified HGVS resolves even without the (GENE) qualifier (e.g. NM_033380.3:c.1871G>A). Returns the normalized classification, review status, and 0-4 star rating plus a recommended_citation. Use this when you already have a variant identifier; if it fails to resolve, fall back to search_variants. id_type='auto' (default) detects the shape; response_mode trims payload size (minimal | compact | standard | full). Pass request_id to correlate the response with server logs."""

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
            context=McpErrorContext(
                tool_name="get_variant", variant_id=identifier, request_id=request_id
            ),
        )

    @mcp.tool(
        name="get_variants",
        title="Get ClinVar Variants (batch)",
        annotations=READ_ONLY_OPEN_WORLD,
        tags={"variant"},
    )
    async def get_variants(
        identifiers: list[str],
        id_type: str = "auto",
        response_mode: str = "compact",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve MANY ClinVar variants in ONE call — the batch form of get_variant. Pass a list of identifiers (VCV / rsID / HGVS / AlleleID / VariationID, mixable); prefer this over looping get_variant when you have several. Each result row echoes its identifier and a found flag (misses are explicit, never dropped); requested / found_count / truncated summarize the batch. response_mode trims payload size and, in minimal/compact, hoists the citation to _meta.citation_template."""

        async def _call() -> dict[str, Any]:
            result = await service_factory().get_variants(
                identifiers, id_type=id_type, response_mode=response_mode
            )
            results = result.get("results") or []
            next_commands: list[dict[str, Any]] = []
            first_found = next((r for r in results if r.get("found")), None)
            if first_found and first_found.get("gene_symbol"):
                next_commands.append(
                    {
                        "tool": "get_gene_clinvar_summary",
                        "arguments": {"gene_symbol": first_found["gene_symbol"]},
                    }
                )
            next_commands.append({"tool": "get_server_capabilities", "arguments": {}})
            result.setdefault("_meta", {})["next_commands"] = next_commands
            return result

        return await run_mcp_tool(
            "get_variants",
            _call,
            context=McpErrorContext(tool_name="get_variants", request_id=request_id),
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
        match_mode: str = "auto",
        count_mode: str = "exact",
        limit: Annotated[int, Field(ge=1)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
        response_mode: str = "compact",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Free-text search across ClinVar variant names, genes, and identifiers. Use this to locate a record when you only have a gene symbol plus a change or other loose text, then re-call get_variant with the returned vcv_accession. Optional filters: gene_symbol, classification, min_stars, assembly. match_mode controls token matching: auto (default) tries AND first then falls back to OR; and/or force explicit mode. count_mode=none skips the total count query for faster responses. Returns a paginated results list with total_count / has_more / next_offset; in minimal/compact mode the citation is hoisted once to _meta.citation_template (fill {variation_id}/{vcv_accession} per row) instead of repeated per hit."""

        async def _call() -> dict[str, Any]:
            result = await service_factory().search_variants(
                query,
                gene_symbol=gene_symbol,
                classification=classification,
                min_stars=min_stars,
                assembly=assembly,
                match_mode=match_mode,
                count_mode=count_mode,
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
                tool_name="search_variants",
                query=query,
                gene_symbol=gene_symbol,
                request_id=request_id,
            ),
        )
