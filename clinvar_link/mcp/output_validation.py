"""Output-schema validation error interceptor for ClinVar Link MCP tools.

Ported from gnomad_link/mcp/output_validation.py. When FastMCP fires an
output-schema validation error, this handler wraps it in the standard
clinvar-link error envelope so LLM callers see a structured actionable response
instead of a raw SDK error string.
"""

from __future__ import annotations

import json
import re
from typing import Any, cast

import mcp.types

from clinvar_link.mcp.errors import (
    _FALLBACK_TOOL,
    _provenance_meta,
    record_mcp_error,
    record_schema_drift,
)

OUTPUT_VALIDATION_PREFIX = "Output validation error:"
_REQUIRED_PROPERTY_RE = re.compile(r"'(?P<field>[^']+)' is a required property")


def actionable_output_validation_error(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    message: str,
) -> dict[str, Any]:
    """Return and record an actionable MCP output-schema validation failure."""
    error_field = _output_validation_field(message)
    suggested_action = (
        f"Tool response failed output schema validation on field '{error_field}'. "
        f"This usually indicates a data/index drift; call {_FALLBACK_TOOL} for context."
    )
    payload: dict[str, Any] = {
        "success": False,
        "error_code": "output_validation_failed",
        "message": "The tool response did not match its declared MCP output schema.",
        "error_field": error_field,
        "suggested_action": suggested_action,
        "_meta": {
            "next_commands": [
                {"tool": _FALLBACK_TOOL, "arguments": {}},
            ],
            **_provenance_meta(),
        },
    }
    record_mcp_error(
        tool_name=tool_name,
        error_code="output_validation_failed",
        exc_type="OutputValidationError",
    )
    # Also surface the event on the dedicated schema-drift ring so an LLM
    # hitting the output_validation_failed envelope can inspect which
    # fields/tools are drifting. Only the parsed schema field is retained; the
    # raw SDK validation string is dropped (it can echo user identifiers).
    record_schema_drift(
        tool_name=tool_name,
        error_field=error_field,
    )
    return payload


def install_output_validation_error_handler(mcp_server: Any) -> None:
    """Wrap the MCP call-tool handler so SDK output validation errors are observable."""
    handler = mcp_server._mcp_server.request_handlers.get(mcp.types.CallToolRequest)
    if handler is None:
        return

    async def wrapped(request: mcp.types.CallToolRequest) -> mcp.types.ServerResult:
        result = cast(mcp.types.ServerResult, await handler(request))
        call_result = getattr(result, "root", None)
        if not isinstance(call_result, mcp.types.CallToolResult):
            return result
        if not call_result.isError or not call_result.content:
            return result
        first_content = call_result.content[0]
        message = getattr(first_content, "text", "")
        if not isinstance(message, str) or not message.startswith(OUTPUT_VALIDATION_PREFIX):
            return result
        payload = actionable_output_validation_error(
            tool_name=request.params.name,
            arguments=request.params.arguments or {},
            message=message,
        )
        return mcp.types.ServerResult(
            mcp.types.CallToolResult(
                content=[
                    mcp.types.TextContent(
                        type="text",
                        text=json.dumps(payload, separators=(",", ":"), sort_keys=True),
                    )
                ],
                isError=True,
            )
        )

    mcp_server._mcp_server.request_handlers[mcp.types.CallToolRequest] = wrapped


def _output_validation_field(message: str) -> str | None:
    match = _REQUIRED_PROPERTY_RE.search(message)
    if match is not None:
        return match.group("field")
    return None
