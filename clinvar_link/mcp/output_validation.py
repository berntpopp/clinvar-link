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
    sanitize_envelope,
)
from clinvar_link.mcp.untrusted_content import sanitize_message

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
    # Defensive backstop: the raw SDK message is never surfaced (only the parsed,
    # sanitized schema field), but strip code points from every leaf regardless.
    payload = sanitize_envelope(payload)
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
        # Code-point-strip the parsed schema property before it reaches the
        # caller payload, the suggested_action, and the schema-drift ring.
        return sanitize_message(match.group("field"))
    return None


# ---------------------------------------------------------------------------
# Protocol-level not-found interceptor (tool / resource / prompt)
# ---------------------------------------------------------------------------
# FastMCP's CORE dispatch reflects the caller-controlled component name/URI
# verbatim when it is unknown ("Unknown tool: '<name>'",
# "Provided resource URI is invalid: '<uri>'"), which bypasses the structured
# tool-envelope path entirely. These are FIXED, input-free public messages that
# never contain the requested name/URI (nor a `_meta.tool` echo of it).
_PROTOCOL_ERROR_MESSAGES: dict[str, str] = {
    "tool": "The requested tool is not available.",
    "resource": "The requested resource is not available.",
    "prompt": "The requested prompt is not available.",
}


class ProtocolError(Exception):
    """A dispatch-level failure re-raised with a FIXED, input-free message."""


def _is_structured_envelope(call_result: mcp.types.CallToolResult) -> bool:
    """True if an isError result carries one of OUR JSON envelopes (has error_code).

    Distinguishes a structured clinvar-link error (already input-free, e.g. the
    output-validation envelope) from a RAW FastMCP dispatch error whose plain-text
    message echoes the caller-supplied tool name ("Unknown tool: '<name>'").
    """
    if not call_result.content:
        return False
    text = getattr(call_result.content[0], "text", None)
    if not isinstance(text, str):
        return False
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return False
    return isinstance(obj, dict) and "error_code" in obj


def _fixed_tool_not_found_result() -> mcp.types.ServerResult:
    """A fixed, input-free CallToolResult for an unknown/failed tool dispatch."""
    msg = _PROTOCOL_ERROR_MESSAGES["tool"]
    payload: dict[str, Any] = {
        "success": False,
        "error_code": "not_found",
        "message": msg,
        "retryable": False,
        "recovery_action": "switch_tool",
        "fallback_tool": _FALLBACK_TOOL,
        "fallback_args": {},
        "recovery": f"{msg} Call {_FALLBACK_TOOL} for the supported tool surface.",
        # NOTE: no `_meta.tool` — the requested name is deliberately NOT echoed.
        "_meta": {
            "next_commands": [{"tool": _FALLBACK_TOOL, "arguments": {}}],
            **_provenance_meta(),
        },
    }
    payload = sanitize_envelope(payload)
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


def install_protocol_error_handler(mcp_server: Any) -> None:
    """Wrap the tool/resource/prompt request handlers so a FastMCP core not-found
    (or read) error can never reflect the caller-supplied name/URI.

    Must be installed AFTER :func:`install_output_validation_error_handler` so it
    is the OUTERMOST wrapper on the CallToolRequest handler (it catches the
    ``Unknown tool`` error the inner handler raises).
    """
    handlers = mcp_server._mcp_server.request_handlers

    call_tool = handlers.get(mcp.types.CallToolRequest)
    if call_tool is not None:

        async def wrapped_call_tool(
            request: mcp.types.CallToolRequest,
            *,
            _orig: Any = call_tool,
        ) -> mcp.types.ServerResult:
            try:
                result = cast(mcp.types.ServerResult, await _orig(request))
            except Exception:
                # A registered tool never raises here (run_mcp_tool returns an
                # envelope); any exception is a dispatch-level failure whose
                # message would echo the caller name — mask it.
                return _fixed_tool_not_found_result()
            # FastMCP returns an isError CallToolResult with a raw plain-text
            # message ("Unknown tool: '<name>'") for an unknown tool; replace any
            # isError result that is NOT one of our structured envelopes.
            root = getattr(result, "root", None)
            if (
                isinstance(root, mcp.types.CallToolResult)
                and root.isError
                and not _is_structured_envelope(root)
            ):
                return _fixed_tool_not_found_result()
            return result

        handlers[mcp.types.CallToolRequest] = wrapped_call_tool

    for request_type, kind in (
        (mcp.types.ReadResourceRequest, "resource"),
        (mcp.types.GetPromptRequest, "prompt"),
    ):
        orig = handlers.get(request_type)
        if orig is None:
            continue

        async def wrapped(
            request: Any,
            *,
            _orig: Any = orig,
            _kind: str = kind,
        ) -> Any:
            try:
                return await _orig(request)
            except Exception:
                # Re-raise with a FIXED, input-free message so no requested
                # name/URI (or its code points) reaches the JSON-RPC error frame.
                raise ProtocolError(_PROTOCOL_ERROR_MESSAGES[_kind]) from None

        handlers[request_type] = wrapped
