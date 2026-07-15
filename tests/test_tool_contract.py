"""The advertised tool surface must be honest, documented, and budgeted (issue #26).

Four fleet contracts, all gated here over EVERY registered tool — the list is derived from the
facade, never hardcoded, so a new tool is gated the day it ships:

  * TOOL-SCHEMA-DOCUMENTATION-STANDARD v1
      S1 every input property carries a non-empty `description`
      S2 every REQUIRED property carries `examples`
      S3 every ARRAY property carries `examples` showing the array form
      S4 every closed vocabulary is declared as an `enum`
  * TOOL-SURFACE-BUDGET-STANDARD v1 — B1 (<=1,200t/tool), B2 (<=10,000t/server), no outputSchema
  * RESPONSE-ENVELOPE-STANDARD v1 — protocol `isError: true` on every error envelope, and
    `error_code` inside the closed six-value enum
"""

from __future__ import annotations

import json

import pytest
from fastmcp import Client

from clinvar_link.mcp.facade import create_clinvar_mcp

# Response-Envelope Standard v1: the closed enum, imported from the ONE source of truth so this
# test pins it exactly rather than carrying a second copy that could drift.
from clinvar_link.models.enums import ERROR_CODES as _ERROR_CODES_TUPLE
from tests._fixture_db import build_service

ERROR_CODES = set(_ERROR_CODES_TUPLE)

BOGUS_ARG = "__gf_conformance_no_such_arg__"


@pytest.fixture
def mcp(tmp_path):
    service = build_service(tmp_path)
    yield create_clinvar_mcp(service_factory=lambda: service)
    service.repo.close()


async def _tools(mcp) -> list:
    async with Client(mcp) as client:
        return await client.list_tools()


def _tokens(tool) -> int:
    """The serialized tools/list entry, in ~4-chars-per-token units (the survey's measure)."""
    blob = json.dumps(
        {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.inputSchema,
            "outputSchema": tool.outputSchema,
            "annotations": tool.annotations.model_dump() if tool.annotations else None,
        },
        default=str,
    )
    return len(blob) // 4


def _props(tool) -> dict:
    return (tool.inputSchema or {}).get("properties") or {}


def _enum_of(prop: dict) -> list | None:
    if isinstance(prop.get("enum"), list):
        return prop["enum"]
    for branch in prop.get("anyOf") or []:
        if isinstance(branch, dict) and isinstance(branch.get("enum"), list):
            return branch["enum"]
    return None


def _is_array(prop: dict) -> bool:
    types = {prop.get("type")}
    types |= {b.get("type") for b in prop.get("anyOf") or [] if isinstance(b, dict)}
    return "array" in types


# --------------------------------------------------------------------------- schema documentation


async def test_s1_every_input_property_has_a_description(mcp):
    missing = [
        f"{tool.name}.{name}"
        for tool in await _tools(mcp)
        for name, prop in _props(tool).items()
        if not (prop.get("description") or "").strip()
    ]
    assert not missing, f"undocumented properties: {missing}"


async def test_s2_every_required_property_has_examples(mcp):
    missing = [
        f"{tool.name}.{name}"
        for tool in await _tools(mcp)
        for name in (tool.inputSchema or {}).get("required") or []
        if not (_props(tool).get(name) or {}).get("examples")
    ]
    assert not missing, (
        f"required properties without `examples` (the gate reports UNGATED): {missing}"
    )


async def test_s3_every_array_property_has_examples_showing_the_array_form(mcp):
    bad = [
        f"{tool.name}.{name}"
        for tool in await _tools(mcp)
        for name, prop in _props(tool).items()
        if _is_array(prop) and not any(isinstance(ex, list) for ex in prop.get("examples") or [])
    ]
    assert not bad, f"array properties without a list-shaped example: {bad}"


async def test_s4_a_vocabulary_declared_closed_on_one_tool_is_closed_on_every_tool(mcp):
    """A parameter that declares an `enum` on ANY tool MUST declare it on EVERY tool it appears on.

    Derived from the registry, not a hardcoded list (a hand-kept list is the same bug one level
    up: case N+1 is gated only if someone remembers to add it). If `classification` is a closed
    enum on get_variants_by_gene, then `classification` on search_variants is the same closed
    vocabulary, and an undeclared one there is the silent-empty filter waiting to happen. This
    catches that inconsistency the moment a new tool reuses a known vocabulary — no list to edit.
    """
    tools = await _tools(mcp)
    closed_names = {name for tool in tools for name, prop in _props(tool).items() if _enum_of(prop)}
    assert closed_names, "no enum-bearing parameter found at all — S4 has nothing to verify"
    undeclared = [
        f"{tool.name}.{name}"
        for tool in tools
        for name, prop in _props(tool).items()
        if name in closed_names and not _enum_of(prop)
    ]
    assert not undeclared, f"closed vocabularies with no enum on some tool: {undeclared}"


async def test_bounded_numerics_declare_their_bounds(mcp):
    """limit=100000 was silently clamped to a maximum the schema never declared."""
    for tool in await _tools(mcp):
        for name, prop in _props(tool).items():
            if name not in {"limit", "offset", "min_stars"}:
                continue
            bounds = {**prop, **{k: v for b in prop.get("anyOf") or [] for k, v in b.items()}}
            assert "minimum" in bounds, f"{tool.name}.{name} declares no minimum"
            if name != "offset":
                assert "maximum" in bounds, f"{tool.name}.{name} declares no maximum"


# --------------------------------------------------------------------------- surface budget


async def test_b1_no_tool_definition_exceeds_1200_tokens(mcp):
    over = {t.name: _tokens(t) for t in await _tools(mcp) if _tokens(t) > 1200}
    assert not over, f"tools over the 1,200t budget: {over}"


async def test_b2_the_server_surface_stays_under_10000_tokens(mcp):
    total = sum(_tokens(t) for t in await _tools(mcp))
    assert total <= 10_000, f"surface is {total}t"


async def test_output_schemas_are_suppressed(mcp):
    """46% of the surface was outputSchema — a field the model never reads."""
    declared = [t.name for t in await _tools(mcp) if t.outputSchema]
    assert not declared, f"tools still publishing an outputSchema: {declared}"


# --------------------------------------------------------------------------- the error frame


async def test_every_tool_returns_protocol_is_error_on_an_error_envelope(mcp):
    """A client that branches on isError saw every failure as a SUCCESSFUL call."""
    async with Client(mcp) as client:
        for tool in await client.list_tools():
            res = await client.call_tool(tool.name, {BOGUS_ARG: "x"}, raise_on_error=False)
            body = res.structured_content or {}
            assert body.get("success") is False, f"{tool.name} ACCEPTED an unknown argument"
            assert res.is_error is True, f"{tool.name}: error envelope carries isError=False"
            assert body.get("error_code") in ERROR_CODES, f"{tool.name}: {body.get('error_code')!r}"


async def test_a_not_found_envelope_also_carries_is_error(mcp):
    async with Client(mcp) as client:
        res = await client.call_tool(
            "get_variant", {"identifier": "VCV999999999"}, raise_on_error=False
        )
    assert res.is_error is True
    assert (res.structured_content or {})["error_code"] == "not_found"


async def test_advertised_error_codes_are_exactly_the_closed_enum(mcp):
    """Capabilities must advertise the WHOLE closed enum, not a subset (a subset test let it drift).

    EXACT equality, not subset: it advertised only {not_found, invalid_input, internal} while a
    `<= ERROR_CODES` assertion passed anyway, so a client never learned the full taxonomy it must
    branch on. The full enum is now advertised and pinned here to the ONE source of truth.
    """
    from clinvar_link.mcp.resources import get_capabilities_resource

    advertised = set(get_capabilities_resource()["error_codes"])
    assert advertised == ERROR_CODES, (
        f"advertised {sorted(advertised)} != closed enum {sorted(ERROR_CODES)}"
    )


async def test_the_unknown_argument_error_names_the_parameters(mcp):
    """'The request was rejected as invalid.' names nothing — the model has nothing to act on."""
    async with Client(mcp) as client:
        res = await client.call_tool("get_variants_by_gene", {BOGUS_ARG: "x"}, raise_on_error=False)
    body = res.structured_content or {}
    message = f"{body.get('message')} {body.get('recovery_action')}"
    assert any(param in message for param in ("gene_symbol", "classification", "sort")), message
