"""Stdio entrypoint for clinvar-link (clinvar-link-mcp).

For HTTP transport use ``clinvar-link serve`` (``--transport unified`` or
``--transport http``). This module runs the MCP server on the stdio transport
(the FastMCP default) for Claude Desktop and similar clients, backed by the
local read-only SQLite index built by ``clinvar-link-data build``.
"""

from __future__ import annotations

from clinvar_link.config import settings
from clinvar_link.data.repository import ClinVarRepository
from clinvar_link.logging_config import configure_logging
from clinvar_link.mcp.facade import create_clinvar_mcp
from clinvar_link.services.clinvar_service import ClinVarService


def _build_mcp():
    """Configure logging, open the local index, and build the MCP server."""
    configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)
    repo = ClinVarRepository(settings.db_path)  # read-only; raises if missing
    service = ClinVarService(repo)
    return create_clinvar_mcp(service_factory=lambda: service)


def main() -> None:
    """Run the clinvar-link MCP server on the stdio transport."""
    _build_mcp().run()  # stdio transport (FastMCP default)


if __name__ == "__main__":
    main()
