"""Somatic tool: search_somatic_classifications (ClinVar clinical-impact tier + oncogenicity)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from fastmcp.tools import ToolResult

from clinvar_link.mcp.annotations import READ_ONLY_OPEN_WORLD
from clinvar_link.mcp.errors import McpErrorContext, run_mcp_tool
from clinvar_link.mcp.params import (
    Limit,
    Offset,
    OncogenicityFilter,
    RequestId,
    SomaticGeneFilter,
    SomaticIdentifier,
    SomaticResponseModeParam,
    TierFilter,
    TumourTypeFilter,
)
from clinvar_link.services import ClinVarService
from clinvar_link.services.somatic import ONCOGENICITY, TIERS, somatic_token

type ToolReturn = dict[str, Any] | ToolResult


def register_somatic_tools(mcp: FastMCP, *, service_factory: Callable[[], ClinVarService]) -> None:
    """Register search_somatic_classifications."""

    @mcp.tool(
        name="search_somatic_classifications",
        title="Search ClinVar Somatic Classifications",
        annotations=READ_ONLY_OPEN_WORLD,
        tags={"variant", "somatic"},
        output_schema=None,
    )
    async def search_somatic_classifications(
        gene_symbol: SomaticGeneFilter = None,
        tier: TierFilter = None,
        oncogenicity: OncogenicityFilter = None,
        tumour_type: TumourTypeFilter = None,
        identifier: SomaticIdentifier = None,
        limit: Limit = 20,
        offset: Offset = 0,
        response_mode: SomaticResponseModeParam = "compact",
        request_id: RequestId = None,
    ) -> ToolReturn:
        """List ClinVar variants that carry a SOMATIC classification: the AMP/ASCO/CAP clinical-impact tier (somatic_clinical_impact) and the ClinGen/CGC/VICC oncogenicity call (oncogenicity), each with review status and date, beside the separate germline_classification (never merged). Tier I first, paginated with total_count / has_more / next_offset. standard/full add per-submission tumour type, therapy and assertion type. Use this for oncology questions (actionable tier, oncogenic call); coverage is ClinVar's somatic submissions only, so an empty page is not evidence a variant lacks clinical impact."""

        async def _call() -> dict[str, Any]:
            service = service_factory()
            gene, resolved_from = await service.resolve_gene_argument(gene_symbol)
            result = await service.search_somatic_classifications(
                gene_symbol=gene,
                tier=tier,
                oncogenicity=oncogenicity,
                tumour_type=tumour_type,
                identifier=identifier,
                limit=limit,
                offset=offset,
                response_mode=response_mode,
            )
            if resolved_from:
                result["resolved_from"] = resolved_from
            results = result.get("results") or []
            next_commands: list[dict[str, Any]] = []
            if result.get("has_more"):
                page: dict[str, Any] = {"gene_symbol": gene_symbol}
                page |= {"tier": somatic_token(tier, TIERS)}
                page |= {"oncogenicity": somatic_token(oncogenicity, ONCOGENICITY)}
                page |= {"tumour_type": tumour_type, "limit": limit}
                page |= {"offset": result["next_offset"], "response_mode": response_mode}
                args = {k: v for k, v in page.items() if v is not None}
                next_commands.append({"tool": "search_somatic_classifications", "arguments": args})
            if results and identifier is None:
                next_commands.append(
                    {
                        "tool": "search_somatic_classifications",
                        "arguments": {
                            "identifier": results[0]["vcv_accession"],
                            "response_mode": "standard",
                        },
                    }
                )
            next_commands.append({"tool": "get_server_capabilities", "arguments": {}})
            result.setdefault("_meta", {})["next_commands"] = next_commands
            return result

        return await run_mcp_tool(
            "search_somatic_classifications",
            _call,
            # No gene_symbol: its fallback is the germline gene summary, wrong for a bad tier.
            context=McpErrorContext(
                tool_name="search_somatic_classifications", request_id=request_id
            ),
        )
