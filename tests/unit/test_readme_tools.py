"""README Standard v1: the ``## Tools`` table must match the registered surface.

The README's tool table is the server's advertised MCP surface. Hand-maintained,
it drifts the moment a tool is added, renamed, or removed — so it is machine-
verified here instead.

The live roster is enumerated the same way ``tests/test_tool_names.py`` does it:
from the **real facade**, with a lazy service factory that is never invoked (so
listing the tools needs no SQLite index).

See ``genefoundry-router/docs/README-STANDARD-v1.md`` (rules 6 and 9).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

from clinvar_link.mcp.facade import create_clinvar_mcp
from clinvar_link.services import ClinVarService

README = Path(__file__).resolve().parents[2] / "README.md"

# A table row whose first cell is a `backticked_tool_name`.
_ROW = re.compile(r"^\|\s*`([a-z0-9_]+)`\s*\|")


def _readme_tool_names() -> set[str]:
    """Parse the tool names out of the README's ``## Tools`` table."""
    text = README.read_text(encoding="utf-8")
    section = re.search(r"^## Tools\s*$(.*?)(?=^## )", text, re.M | re.S)
    assert section is not None, "README.md has no '## Tools' section"
    return {m.group(1) for line in section.group(1).splitlines() if (m := _ROW.match(line))}


async def _registered_tool_names() -> set[str]:
    """Enumerate the live registered tool surface from the real facade."""
    mcp = create_clinvar_mcp(service_factory=lambda: cast(ClinVarService, object()))
    return {t.name for t in await mcp.list_tools()}


async def test_readme_tools_table_matches_registered_tools() -> None:
    documented = _readme_tool_names()
    registered = await _registered_tool_names()

    assert documented, "the README '## Tools' table lists no tools"
    assert documented == registered, (
        "the README '## Tools' table has drifted from the registered tool surface.\n"
        f"  missing from README: {sorted(registered - documented)}\n"
        f"  not registered:      {sorted(documented - registered)}\n"
        "Every registered tool needs exactly one row (README Standard v1, rule 6)."
    )
