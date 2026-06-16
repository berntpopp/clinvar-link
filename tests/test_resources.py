import pytest

from clinvar_link.exceptions import DataNotFoundError
from clinvar_link.mcp.errors import McpErrorContext, run_mcp_tool
from clinvar_link.mcp.resources import (
    get_capabilities_resource,
    get_license_resource,
)


def test_capabilities_lists_five_tools():
    cap = get_capabilities_resource()
    assert cap["research_use_only"] is True
    assert {
        "get_server_capabilities",
        "get_variant",
        "search_variants",
        "get_gene_clinvar_summary",
        "get_variants_by_gene",
    } <= set(cap["tools"])


def test_license_has_attribution():
    lic = get_license_resource()
    assert "ncbi" in str(lic).lower() or "clinvar" in str(lic).lower()


@pytest.mark.asyncio
async def test_run_mcp_tool_success_injects_meta():
    async def ok():
        return {"value": 1}

    out = await run_mcp_tool("t", ok, context=McpErrorContext(tool_name="t"))
    assert out["success"] is True and out["value"] == 1
    assert out["_meta"]["unsafe_for_clinical_use"] is True


@pytest.mark.asyncio
async def test_run_mcp_tool_not_found_envelope():
    async def boom():
        raise DataNotFoundError("nope")

    out = await run_mcp_tool("t", boom, context=McpErrorContext(tool_name="t"))
    assert out["success"] is False and out["error_code"] == "not_found"
