"""Guard: pyproject -> installed metadata -> __version__ -> serverInfo are one value.

Regression + drift guard. Two failure modes are locked down here:

1. ``serverInfo.version`` leak: ``create_clinvar_mcp`` built ``FastMCP(...)``
   without a ``version=`` argument, so an MCP ``initialize`` handshake
   advertised the FastMCP framework version (e.g. ``3.4.2``) instead of the
   clinvar-link package version.
2. Version drift: the package version is single-sourced from
   ``pyproject.toml [project].version``; ``clinvar_link.__version__`` derives
   from the installed distribution metadata. These tests assert every surface
   agrees, so a future bump to only one place fails CI.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import version
from pathlib import Path
from typing import cast

from clinvar_link import __version__
from clinvar_link.mcp.facade import create_clinvar_mcp
from clinvar_link.services import ClinVarService

DIST = "clinvar-link"


def _pyproject_version() -> str:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]


def test_pyproject_is_the_single_source() -> None:
    assert version(DIST) == _pyproject_version()


def test_dunder_version_is_metadata_derived() -> None:
    assert __version__ == version(DIST)


def test_mcp_server_info_version_matches_package() -> None:
    mcp = create_clinvar_mcp(service_factory=lambda: cast(ClinVarService, object()))
    assert mcp.version == version(DIST)
