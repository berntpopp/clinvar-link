"""Tool-Naming & Normalization Standard v1 compliance (CI guard).

Every registered tool name must be unprefixed snake_case starting with a
canonical verb so it composes cleanly behind a namespacing gateway: the router
mounts this server under the ``clinvar`` namespace, so leaf ``get_variant``
surfaces as ``clinvar_get_variant`` at the gateway. A leaf-level ``clinvar_``
prefix would double-prefix there, so it is forbidden here.

The live roster (built from the real facade) must also equal the capabilities
roster (``resources._TOOLS``) so neither can drift from the other.

See ``genefoundry-router/docs/TOOL-NAMING-STANDARD-v1.md`` (rules 1-3, 5, 8).
"""

from __future__ import annotations

import re
from typing import cast

from clinvar_link.mcp.facade import create_clinvar_mcp
from clinvar_link.mcp.resources import _TOOLS
from clinvar_link.services import ClinVarService

_NAME_RE = re.compile(r"^[a-z0-9_]{1,50}$")
_CANONICAL_VERBS = frozenset({"get", "search", "list", "resolve", "find", "compare", "compute"})
_NAMESPACE = "clinvar"


async def _live_tool_names() -> list[str]:
    """Enumerate the live registered tool surface from the real facade.

    The service factory is lazy and never invoked here, so listing the tools
    needs no SQLite index.
    """
    mcp = create_clinvar_mcp(service_factory=lambda: cast(ClinVarService, object()))
    return sorted(t.name for t in await mcp.list_tools())


async def test_registered_tools_equal_capabilities_roster() -> None:
    live = set(await _live_tool_names())
    assert live == set(_TOOLS)


async def test_tool_names_conform_to_standard_v1() -> None:
    names = await _live_tool_names()
    assert names, "no tools registered"
    for name in names:
        assert _NAME_RE.match(name), f"{name!r} must match ^[a-z0-9_]{{1,50}}$"
        assert name.split("_", 1)[0] in _CANONICAL_VERBS, (
            f"{name!r} must start with a canonical verb {sorted(_CANONICAL_VERBS)}"
        )
        assert not name.startswith(f"{_NAMESPACE}_"), (
            f"{name!r} must not self-prefix the '{_NAMESPACE}' namespace token"
        )
