"""Shared local helpers for the MCP tool tests.

Task 11 will introduce a real conftest; until then each tool-test module builds
its own SQLite index from the checked-in fixture via these helpers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clinvar_link.config import Settings
from clinvar_link.data.repository import ClinVarRepository
from clinvar_link.ingest.builder import build_database
from clinvar_link.services import ClinVarService

FIXTURE = Path(__file__).parent / "fixtures" / "variant_summary_sample.txt"


def build_service(tmp_path: Path) -> ClinVarService:
    """Build a real SQLite index from the fixture and return a service over it."""
    cfg = Settings(DATA_DIR=tmp_path, DB_FILENAME="t.sqlite")
    build_database(cfg, source_path=FIXTURE, last_modified="Mon, 01 Jan 2026 00:00:00 GMT")
    repo = ClinVarRepository(cfg.db_path)
    return ClinVarService(repo)


async def call_tool(mcp: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Call an MCP tool through the in-memory FastMCP client and return its dict.

    FastMCP returns a CallToolResult with a raw ``.structured_content`` dict
    (always present, exactly as the server emitted it) and an ergonomic
    ``.data`` attribute. For tools that declare an ``output_schema`` (v1.1
    fenced tools: get_variant / get_variants / search_variants /
    get_variants_by_gene / get_gene_clinvar_summary), FastMCP's client
    reconstructs ``.data`` into a *generated pydantic model instance* from
    that schema rather than a plain dict, so tests must prefer the guaranteed
    -dict ``.structured_content`` and only fall back to ``.data`` when it is
    itself already a dict (tools with no output_schema).
    """
    from fastmcp import Client

    async with Client(mcp) as client:
        res = await client.call_tool(name, args)
    structured = getattr(res, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    data = getattr(res, "data", None)
    if isinstance(data, dict):
        return data
    return res.structured_content
