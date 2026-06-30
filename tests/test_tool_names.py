"""Tool-Naming & Normalization Standard v1.1 compliance (CI guard).

Every registered tool name must be unprefixed snake_case starting with a
canonical verb (or be exempt via the ops/meta tag carve-out) so it composes
cleanly behind a namespacing gateway: the router mounts this server under the
``clinvar`` namespace, so leaf ``get_variant`` surfaces as
``clinvar_get_variant`` at the gateway. A leaf-level ``clinvar_`` prefix would
double-prefix there, so it is forbidden here.

The live roster (built from the real facade) must also equal the capabilities
roster (``resources._TOOLS``) so neither can drift from the other.

See ``genefoundry-router/docs/TOOL-NAMING-STANDARD-v1.md`` (rules 1-3, 5, 8).

VERB CANON (ratified Standard v1.1, 2026-06-30)
------------------------------------------------
Tier-1 (universal read/query, all backends):
    get, search, list, resolve, find, compare, compute, map

Tier-2 (sanctioned domain action/compute verbs):
    predict, annotate, recode, liftover, analyze, score,
    submit, export, generate, download

Operational/meta carve-out (by tag, not verb, Standard v1.1 §Q3):
    Tools tagged ``ops`` or ``meta`` skip the verb rule but still must pass
    charset/length/no-self-prefix checks.
"""

from __future__ import annotations

import re
from typing import cast

from clinvar_link.mcp.facade import create_clinvar_mcp
from clinvar_link.mcp.resources import _TOOLS
from clinvar_link.services import ClinVarService

_NAME_RE = re.compile(r"^[a-z0-9_]{1,50}$")

# Ratified Tier-1: universal read/query canon (Standard v1.1, Rule 2).
_CANONICAL_VERBS = frozenset(
    {"get", "search", "list", "resolve", "find", "compare", "compute", "map"}
)

# Ratified Tier-2: sanctioned domain action/compute verbs (Standard v1.1).
_TIER2_VERBS = frozenset(
    {
        "predict",
        "annotate",
        "recode",
        "liftover",
        "analyze",
        "score",
        "submit",
        "export",
        "generate",
        "download",
    }
)

# Combined allowed verb set for domain tools.
_ALL_VERBS = _CANONICAL_VERBS | _TIER2_VERBS

# Tags that grant an ops/meta carve-out (Standard v1.1, §Q3 ratification).
_OPS_CARVEOUT_TAGS = frozenset({"ops", "meta"})

_NAMESPACE = "clinvar"


async def _live_tool_names() -> list[str]:
    """Enumerate the live registered tool surface from the real facade.

    The service factory is lazy and never invoked here, so listing the tools
    needs no SQLite index.
    """
    mcp = create_clinvar_mcp(service_factory=lambda: cast(ClinVarService, object()))
    return sorted(t.name for t in await mcp.list_tools())


async def _live_tools() -> list[object]:
    """Enumerate live tool objects (name + tags) from the real facade."""
    mcp = create_clinvar_mcp(service_factory=lambda: cast(ClinVarService, object()))
    return list(await mcp.list_tools())


async def test_registered_tools_equal_capabilities_roster() -> None:
    live = set(await _live_tool_names())
    assert live == set(_TOOLS)


async def test_tool_names_conform_to_standard_v1_1() -> None:
    tools = await _live_tools()
    assert tools, "no tools registered"
    for tool in tools:
        name = tool.name  # type: ignore[attr-defined]
        tags = set(getattr(tool, "tags", None) or ())
        assert _NAME_RE.match(name), f"{name!r} must match ^[a-z0-9_]{{1,50}}$"
        assert not name.startswith(f"{_NAMESPACE}_"), (
            f"{name!r} must not self-prefix the '{_NAMESPACE}' namespace token"
        )
        # Ops/meta tag carve-out: infrastructure tools are exempt from the verb rule.
        if tags & _OPS_CARVEOUT_TAGS:
            continue
        verb = name.split("_", 1)[0]
        assert verb in _ALL_VERBS, (
            f"{name!r} must start with a Tier-1 or Tier-2 verb; "
            f"Tier-1: {sorted(_CANONICAL_VERBS)}, Tier-2: {sorted(_TIER2_VERBS)}; "
            "or tag the tool ops/meta for the operational carve-out "
            "(Standard v1.1, genefoundry-router/docs/TOOL-NAMING-STANDARD-v1.md)"
        )
