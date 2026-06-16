"""Capabilities tool plus resource handlers for ClinVar Link."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from mcp.types import Annotations

from clinvar_link.mcp.annotations import READ_ONLY_OPEN_WORLD
from clinvar_link.mcp.clinvar_date_cache import (
    has_cached_clinvar_release_date,
    set_cached_clinvar_release_date,
)
from clinvar_link.mcp.errors import run_mcp_tool
from clinvar_link.mcp.resources import (
    RESEARCH_USE_NOTICE,
    get_capabilities_resource,
    get_license_resource,
    get_usage_resource,
)
from clinvar_link.services import ClinVarService

logger = logging.getLogger(__name__)

_RESOURCE_ANNOTATIONS = Annotations(audience=["assistant"], priority=1.0)


def register_metadata_tools(mcp: FastMCP, *, service_factory: Callable[[], ClinVarService]) -> None:
    """Register the capabilities discovery tool and the static MCP resources."""

    @mcp.tool(
        name="get_server_capabilities",
        title="Get ClinVar Link Capabilities",
        annotations=READ_ONLY_OPEN_WORLD,
        tags={"metadata"},
    )
    async def get_server_capabilities() -> dict[str, Any]:
        """Use this for orientation in a new session: the supported tool surface, response modes, recommended workflows, the live ClinVar release date, error codes, and current limitations. Returns ~3kB."""

        return await run_mcp_tool(
            "get_server_capabilities",
            lambda: _coro_capabilities(service_factory),
        )

    @mcp.resource(
        "clinvar://capabilities",
        annotations=_RESOURCE_ANNOTATIONS,
        mime_type="application/json",
    )
    def capabilities_resource() -> dict[str, Any]:
        return get_capabilities_resource()

    @mcp.resource("clinvar://usage", annotations=_RESOURCE_ANNOTATIONS)
    def usage_resource() -> str:
        return get_usage_resource()

    @mcp.resource(
        "clinvar://license",
        annotations=_RESOURCE_ANNOTATIONS,
        mime_type="application/json",
    )
    def license_resource() -> dict[str, Any]:
        return get_license_resource()

    @mcp.resource(
        "clinvar://research-use",
        annotations=_RESOURCE_ANNOTATIONS,
        mime_type="application/json",
    )
    def research_use_resource() -> dict[str, Any]:
        return {"notice": RESEARCH_USE_NOTICE}


async def _coro_capabilities(
    service_factory: Callable[[], ClinVarService],
) -> dict[str, Any]:
    # Best-effort prime the release-date cache before returning so the
    # capabilities payload (and every subsequent envelope) can echo the live
    # ClinVar release date. A missing date is never a blocker.
    if not has_cached_clinvar_release_date():
        try:
            meta = await service_factory().get_clinvar_meta()
            set_cached_clinvar_release_date(meta.get("release_date"))
        except Exception:
            # Do not cache the failure; the next call retries.
            logger.debug("clinvar release-date priming failed", exc_info=True)
    return get_capabilities_resource()
