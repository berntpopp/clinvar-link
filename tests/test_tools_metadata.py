"""MCP tool tests for the metadata tool and resources (get_server_capabilities)."""

from __future__ import annotations

import pytest

from clinvar_link.mcp.clinvar_date_cache import (
    get_cached_clinvar_release_date,
    reset_clinvar_date_cache,
)
from clinvar_link.mcp.facade import create_clinvar_mcp
from tests._fixture_db import build_service, call_tool

_EXPECTED_TOOLS = {
    "get_variant",
    "search_variants",
    "get_gene_clinvar_summary",
    "get_variants_by_gene",
    "get_server_capabilities",
}


@pytest.fixture
def mcp(tmp_path):
    """A ClinVar Link MCP facade wired to a fixture-backed service.

    Reset the process-level release-date cache so the capabilities priming
    assertion is deterministic regardless of test ordering.
    """
    reset_clinvar_date_cache()
    service = build_service(tmp_path)
    yield create_clinvar_mcp(service_factory=lambda: service)
    service.repo.close()
    reset_clinvar_date_cache()


async def test_capabilities_lists_all_tools(mcp):
    out = await call_tool(mcp, "get_server_capabilities", {})
    assert out["success"] is True
    assert _EXPECTED_TOOLS.issubset(set(out["tools"]))


async def test_capabilities_primes_release_date_cache(mcp):
    assert get_cached_clinvar_release_date() is None
    await call_tool(mcp, "get_server_capabilities", {})
    # The fixture builds with a fixed last_modified, so a release date is present.
    assert get_cached_clinvar_release_date() is not None
