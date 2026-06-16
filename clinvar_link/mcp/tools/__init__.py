"""Tool registration entry points for the ClinVar Link MCP facade."""

from __future__ import annotations

from collections.abc import Callable

from fastmcp import FastMCP

from clinvar_link.mcp.tools.genes import register_gene_tools
from clinvar_link.mcp.tools.metadata import register_metadata_tools
from clinvar_link.mcp.tools.variants import register_variant_tools
from clinvar_link.services import ClinVarService


def register_clinvar_tools(
    mcp: FastMCP,
    *,
    service_factory: Callable[[], ClinVarService],
) -> None:
    """Register every ClinVar Link tool group on the FastMCP server."""
    register_variant_tools(mcp, service_factory=service_factory)
    register_gene_tools(mcp, service_factory=service_factory)
    register_metadata_tools(mcp, service_factory=service_factory)
