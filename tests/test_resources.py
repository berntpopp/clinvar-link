import pytest

from clinvar_link.exceptions import DataNotFoundError
from clinvar_link.mcp.errors import McpErrorContext, run_mcp_tool
from clinvar_link.mcp.resources import (
    get_capabilities_resource,
    get_license_resource,
    get_version_resource,
)
from clinvar_link.models import ClinVarVariant
from clinvar_link.models.gene_models import GeneClinVarSummary

# Known envelope-level flags (not model fields but real output keys).
_ENVELOPE_FLAGS = {"total_count_capped"}


def test_output_cheatsheet_field_names_match_real_output():
    # The cheatsheet is a discovery aid: every *_field / *_flag value MUST name a
    # real key in the variant or gene payload (model field), a known envelope flag,
    # or an envelope path — else it actively misleads callers.
    # Regression guard for the gold_stars / vcv_id drift.
    cheats = get_capabilities_resource()["output_cheatsheet"]
    model_fields = set(ClinVarVariant.model_fields) | set(GeneClinVarSummary.model_fields)
    for concept, field_name in cheats.items():
        if field_name.startswith("_meta"):
            continue
        if field_name in _ENVELOPE_FLAGS:
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
    # New fields added in Track A v2 must be present.
    assert cheats["other_count_field"] == "other_count"
    assert cheats["capped_total_flag"] == "total_count_capped"


def test_capabilities_advertises_sort_options():
    from clinvar_link.data.repository import ClinVarRepository

    caps = get_capabilities_resource()
    assert caps["sort_options"] == sorted(ClinVarRepository.SORT_ORDERS)


def test_capabilities_carries_data_freshness_once_primed():
    # AC4: once the release-date cache is primed, capabilities exposes the same
    # freshness signal as the per-response _meta block. Hermetic: prime then reset.
    from clinvar_link.mcp.clinvar_date_cache import (
        reset_clinvar_date_cache,
        set_cached_clinvar_release_date,
    )

    try:
        set_cached_clinvar_release_date("Mon, 15 Jun 2026 08:40:33 GMT")
        caps = get_capabilities_resource()
        assert isinstance(caps["data_freshness"]["age_days"], int)
        assert isinstance(caps["data_freshness"]["past_ttl"], bool)
    finally:
        reset_clinvar_date_cache()
    # With the cache cleared, the optional key is simply absent (no null noise).
    assert "data_freshness" not in get_capabilities_resource()


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


def test_version_resource_shape():
    v = get_version_resource()
    assert v["server"] == "clinvar-link"
    assert isinstance(v["server_version"], str) and v["server_version"]
    assert "mcp_protocol_version" in v
    assert "clinvar_release_date" in v  # may be None until the date cache primes
