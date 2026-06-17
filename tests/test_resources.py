import pytest

from clinvar_link.exceptions import DataNotFoundError
from clinvar_link.mcp.errors import McpErrorContext, run_mcp_tool
from clinvar_link.mcp.resources import (
    get_capabilities_resource,
    get_license_resource,
)
from clinvar_link.models import ClinVarVariant


def test_output_cheatsheet_field_names_match_real_output():
    # The cheatsheet is a discovery aid: every *_field value MUST name a real key
    # in the variant payload (model field) or an envelope path, else it actively
    # misleads callers. Regression guard for the gold_stars / vcv_id drift.
    cheats = get_capabilities_resource()["output_cheatsheet"]
    model_fields = set(ClinVarVariant.model_fields)
    for concept, field_name in cheats.items():
        if field_name.startswith("_meta"):
            continue
        assert field_name in model_fields, f"{concept} -> {field_name!r} is not a real output field"
    # The normalized classification lives in `classification`, the star rating in
    # `star_rating`, and the variant accession in `vcv_accession`.
    assert cheats["classification_field"] == "classification"
    assert cheats["star_rating_field"] == "star_rating"
    assert cheats["variant_accession_field"] == "vcv_accession"
    # The only envelope-path entry must name the real _meta.next_commands path, so
    # a typo there (silently skipped by the model-field loop above) is still caught.
    assert cheats["next_commands_field"] == "_meta.next_commands"


def test_capabilities_advertises_sort_options():
    from clinvar_link.data.repository import ClinVarRepository

    caps = get_capabilities_resource()
    assert caps["sort_options"] == sorted(ClinVarRepository.SORT_ORDERS)


def test_capabilities_lists_core_tools():
    cap = get_capabilities_resource()
    assert cap["research_use_only"] is True
    assert {
        "get_server_capabilities",
        "get_variant",
        "get_variants",
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
