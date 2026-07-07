"""Structured MCP error envelopes for ClinVar Link tools.

Patterned after gnomad_link/mcp/errors.py. The envelope shape is what LLMs
branch on; codes are deterministic per exception class so prompts can recover
without scraping free text.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from pydantic import ValidationError as PydanticValidationError

from clinvar_link.config import settings
from clinvar_link.exceptions import (
    ClinVarDataError,
    ClinVarServerError,
    DataNotFoundError,
    ToolInputError,
)
from clinvar_link.mcp.clinvar_date_cache import get_cached_clinvar_release_date
from clinvar_link.mcp.freshness import clinvar_freshness
from clinvar_link.mcp.resources import server_version

logger = logging.getLogger(__name__)

RECENT_MCP_ERROR_LIMIT = 50
_RECENT_ERRORS: deque[dict[str, Any]] = deque(maxlen=RECENT_MCP_ERROR_LIMIT)

# Schema-drift events live in a separate, smaller ring so LLM callers can
# distinguish business errors (the general ring) from infrastructure events
# such as a stored row no longer matching our declared output_schema.
RECENT_SCHEMA_DRIFT_LIMIT = 25
_RECENT_SCHEMA_DRIFT: deque[dict[str, Any]] = deque(maxlen=RECENT_SCHEMA_DRIFT_LIMIT)

# Fallback tool used in validation and error envelopes. Points to
# get_server_capabilities for the discovery surface on error recovery.
_FALLBACK_TOOL = "get_server_capabilities"


@dataclass
class McpErrorContext:
    """Per-call context passed to the error builder so envelopes can suggest fallbacks.

    ``request_id`` correlates a response to server-side logs/traces. It is
    accepted from the client for idempotency/correlation and minted server-side
    when absent (see :func:`run_mcp_tool`).
    """

    tool_name: str
    variant_id: str | None = None
    gene_symbol: str | None = None
    query: str | None = None
    request_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class McpToolError(Exception):
    """An exception whose `str(self)` is the JSON-serialised envelope."""

    def __init__(self, payload: dict[str, Any]):
        super().__init__(json.dumps(payload))
        self.payload = payload


def _provenance_meta(context: McpErrorContext | None = None) -> dict[str, Any]:
    """Base ``_meta`` provenance merged into every success and error envelope.

    Always carries the research-use flag and server version. ``clinvar_release_date``
    is set only once the date cache has been primed (first get_server_capabilities or
    lazy service read); omitted while still unknown to avoid null noise.
    ``request_id`` (when set on the context) correlates the response to
    server-side logs/traces.
    """
    clinvar_date = get_cached_clinvar_release_date()
    meta: dict[str, Any] = {
        "unsafe_for_clinical_use": True,
        "server_version": server_version(),
    }
    if clinvar_date is not None:
        meta["clinvar_release_date"] = clinvar_date
        fresh = clinvar_freshness(clinvar_date, settings.REFRESH_TTL_DAYS)
        if fresh is not None:
            meta.update(fresh)
    if context is not None and context.request_id:
        meta["request_id"] = context.request_id
    return meta


def _safe_message(exc: BaseException) -> str:
    text = str(exc) or exc.__class__.__name__
    # ClinVar lookups are user-input shaped; trim long tracebacks/identifiers.
    return text[:240]


_GENE_TOOLS = frozenset({"get_gene_clinvar_summary", "get_variants_by_gene"})


def _fallback_for(context: McpErrorContext) -> tuple[str, dict[str, Any] | None]:
    """Resolve the context-appropriate resolver tool for not_found / invalid_input.

    A failing variant lookup almost always received free text / a gene symbol;
    point it at search_variants. A failing gene tool points back at search; and
    everything else at the discovery entrypoint. fallback_args are populated from
    context so the LLM gets a ready-to-call next step.

    Whitespace-only values for query/variant_id/gene_symbol are treated as absent
    so blank strings are never echoed into fallback_args or next_commands.
    """
    query = (context.query or "").strip() or None
    variant_id = (context.variant_id or "").strip() or None
    if context.tool_name == "get_variant":
        if query:
            return "search_variants", {"query": query}
        if variant_id:
            return "search_variants", {"query": variant_id}
        return "search_variants", None
    if context.gene_symbol and context.gene_symbol.strip():
        return "get_gene_clinvar_summary", {"gene_symbol": context.gene_symbol.strip()}
    if query:
        return "search_variants", {"query": query}
    return "get_server_capabilities", None


def _classify(
    exc: BaseException, context: McpErrorContext
) -> tuple[str, bool, str | None, dict[str, Any] | None]:
    """Return (error_code, retryable, fallback_tool, fallback_args).

    Subclass ordering matters: DataNotFoundError and ClinVarDataError both
    subclass ClinVarServerError, so they MUST be checked before the generic
    ClinVarServerError branch. The load-bearing invariant: retryable=true means
    an identical call may later succeed; false means it never will. Local-index
    failures are never retryable.
    """
    if isinstance(exc, DataNotFoundError):
        tool, args = _fallback_for(context)
        return "not_found", False, tool, args
    if isinstance(exc, ToolInputError):
        tool, args = _fallback_for(context)
        return "invalid_input", False, tool, args
    if isinstance(exc, PydanticValidationError):
        return "invalid_input", False, _FALLBACK_TOOL, {}
    if isinstance(exc, ValueError):
        return "invalid_input", False, _FALLBACK_TOOL, {}
    if isinstance(exc, ClinVarDataError):
        return "internal_error", False, _FALLBACK_TOOL, {}
    if isinstance(exc, ClinVarServerError):
        return "internal_error", False, _FALLBACK_TOOL, {}
    return "internal_error", False, _FALLBACK_TOOL, {}


def _recovery_action(error_code: str, retryable: bool) -> str:
    """Action-typed guidance so the LLM does not infer behavior from a bare bool.

    retry_backoff (wait + retry same call) | reformulate_input (fix the id/fields,
    same tool) | switch_tool (call the fallback_tool, then the original).
    """
    if retryable:
        return "retry_backoff"
    if error_code in {"invalid_input", "validation_failed"}:
        return "reformulate_input"
    return "switch_tool"


def _recovery_text(error_code: str, fallback_tool: str | None, tool_name: str | None = None) -> str:
    is_gene = tool_name in _GENE_TOOLS
    if error_code == "not_found":
        if is_gene:
            return (
                "No ClinVar record for that gene in the local index. Confirm the HGNC "
                "gene symbol (e.g. COL4A5); or call search_variants to discover variants."
            )
        resolver = fallback_tool or "search_variants"
        return (
            "Identifier well-formed but absent in the local ClinVar index. This is a "
            "reformulate, not a retry: confirm the VCV / rsID / HGVS / AlleleID "
            "(e.g. VCV000024455 | rs104886142 | NM_033380.3(COL4A5):c.1871G>A), or call "
            f"{resolver} to locate the matching record, then retry."
        )
    if error_code == "invalid_input":
        if is_gene:
            return (
                "The request was rejected as malformed. Pass a single HGNC gene symbol "
                "(e.g. COL4A5) and a valid sort/filter; do not retry unchanged."
            )
        resolver = fallback_tool or "get_server_capabilities"
        return (
            "The request was rejected as malformed (the identifier or query shape is "
            "wrong for this tool). Do not retry unchanged. Provide a valid id "
            "(e.g. VCV000024455 | rs104886142 | NM_033380.3(COL4A5):c.1871G>A) or call "
            f"{resolver}."
        )
    return (
        f"Unexpected failure. Call {fallback_tool} for a safe entry point."
        if fallback_tool
        else "Unexpected failure."
    )


def _envelope_message(exc: BaseException, error_code: str) -> str:
    """Return a message safe to surface to LLM callers.

    Validation errors use a canned prefix so callers can pattern-match without
    receiving raw user input. Internal errors are fully opaque to avoid leaking
    implementation details or sensitive values.
    """
    if isinstance(exc, ToolInputError):
        # Developer-authored guard string (static or parameter NAMES only, no user
        # values), so it is safe to surface verbatim instead of redacting.
        return _safe_message(exc)
    if error_code == "invalid_input":
        return f"Invalid input: {exc.__class__.__name__}"
    if error_code == "internal_error":
        return f"Internal error: {exc.__class__.__name__}"
    return _safe_message(exc)


def _extract_field_errors(errors: list[Any]) -> list[dict[str, str]]:
    """Flatten Pydantic validation errors into {field, reason} dicts."""
    result: list[dict[str, str]] = []
    for err in errors:
        loc = err.get("loc", ())
        field_name = ".".join(str(x) for x in loc) if loc else "unknown"
        reason = err.get("msg", str(err.get("type", "invalid")))
        result.append({"field": field_name, "reason": reason})
    return result


def mcp_validation_tool_error(
    *,
    tool_name: str,
    exc: PydanticValidationError,
) -> McpToolError:
    """Build a sanitized validation failure raised before tool execution starts."""
    field_errors = _extract_field_errors(list(exc.errors()))
    payload: dict[str, Any] = {
        "success": False,
        "error_code": "invalid_input",
        "message": "Invalid MCP arguments.",
        "retryable": False,
        "recovery_action": "reformulate_input",
        "fallback_tool": _FALLBACK_TOOL,
        "fallback_args": {},
        "field_errors": field_errors,
        "recovery": (
            "Inputs failed validation. Check field_errors for details and call "
            f"{_FALLBACK_TOOL} for the accepted tool surface and identifier shapes."
        ),
        "_meta": {
            "next_commands": [{"tool": _FALLBACK_TOOL, "arguments": {}}],
            **_provenance_meta(),
        },
    }
    return McpToolError(payload)


def install_validation_error_handler(mcp_server: Any) -> None:
    """Wrap registered tools so FastMCP argument validation returns our envelope.

    FastMCP stores tools on ``_local_provider._components`` (modern path) or the
    legacy ``_tool_manager._tools`` mapping. We probe both so the handler keeps
    working across FastMCP minor versions. Tools without a ``run`` method (e.g.
    resources or prompts that happen to share the registry) are skipped.
    """
    candidates: list[Any] = []
    local_provider = getattr(mcp_server, "_local_provider", None)
    components = getattr(local_provider, "_components", None)
    if isinstance(components, dict):
        candidates.extend(components.values())
    tool_manager = getattr(mcp_server, "_tool_manager", None)
    legacy_tools = getattr(tool_manager, "_tools", None)
    if isinstance(legacy_tools, dict):
        candidates.extend(legacy_tools.values())

    for tool in candidates:
        if not hasattr(tool, "run") or getattr(tool, "_clinvar_validation_wrapped", False):
            continue
        original_run = tool.run

        async def wrapped_run(
            arguments: dict[str, Any],
            *,
            _original_run: Callable[[dict[str, Any]], Awaitable[Any]] = original_run,
            _tool: Any = tool,
        ) -> Any:
            try:
                return await _original_run(arguments)
            except PydanticValidationError as exc:
                envelope = mcp_validation_tool_error(
                    tool_name=str(getattr(_tool, "name", "unknown")),
                    exc=exc,
                ).payload
                record_mcp_error(
                    tool_name=str(getattr(_tool, "name", "unknown")),
                    error_code="invalid_input",
                    exc_type=exc.__class__.__name__,
                )
                convert_result = getattr(_tool, "convert_result", None)
                if callable(convert_result):
                    return convert_result(envelope)
                return envelope

        object.__setattr__(tool, "run", wrapped_run)
        object.__setattr__(tool, "_clinvar_validation_wrapped", True)


def mcp_tool_error(exc: BaseException, context: McpErrorContext) -> McpToolError:
    error_code, retryable, fallback_tool, fallback_args = _classify(exc, context)
    # next_commands must agree with the classified fallback: prepend the
    # task-advancing resolver when there is one, keeping the discovery entrypoint
    # as the secondary entry. When fallback_tool is already the discovery
    # entrypoint, the guard collapses to a single entry.
    next_commands: list[dict[str, Any]] = []
    if fallback_tool and fallback_tool != _FALLBACK_TOOL:
        next_commands.append({"tool": fallback_tool, "arguments": fallback_args or {}})
    next_commands.append({"tool": _FALLBACK_TOOL, "arguments": {}})
    payload = {
        "success": False,
        "error_code": error_code,
        "message": _envelope_message(exc, error_code),
        "retryable": retryable,
        "recovery_action": _recovery_action(error_code, retryable),
        "fallback_tool": fallback_tool,
        "fallback_args": fallback_args,
        "recovery": _recovery_text(error_code, fallback_tool, context.tool_name),
        "_meta": {
            "tool": context.tool_name,
            "next_commands": next_commands,
            **_provenance_meta(context),
        },
    }
    return McpToolError(payload)


def record_mcp_error(*, tool_name: str, error_code: str, exc_type: str) -> None:
    """Append a business-error event to the bounded diagnostics ring.

    Stores only low-cardinality, non-PII fields: the tool name, the classified
    ``error_code``, and the exception *type* name. Raw exception text (and the
    derived envelope ``message``) is deliberately NOT retained — it can embed
    user-supplied identifiers (VCV / rsID / HGVS / free-text queries) that may be
    GDPR Art. 9 patient-derived data, and this ring is readable back as
    diagnostics.
    """
    _RECENT_ERRORS.append(
        {
            "tool_name": tool_name,
            "error_code": error_code,
            "exc_type": exc_type,
        }
    )


def get_recent_errors() -> list[dict[str, Any]]:
    return list(_RECENT_ERRORS)


def clear_recent_errors() -> None:
    _RECENT_ERRORS.clear()


_SCHEMA_DRIFT_MESSAGE = "Tool response did not match its declared MCP output schema."


def record_schema_drift(*, tool_name: str, error_field: str | None) -> None:
    """Append an output-schema-drift event to the bounded ring.

    Separate from record_mcp_error so an LLM can distinguish business errors
    (not_found, invalid_input) from infrastructure events (a stored row no
    longer matches our declared output_schema, which usually means we need to
    widen a model).

    Stores only the parsed ``error_field`` (a schema property name) plus a fixed
    message; the raw SDK validation string is NOT retained because it can echo
    user-supplied identifiers into this readable-back diagnostics ring.
    """
    _RECENT_SCHEMA_DRIFT.append(
        {
            "tool_name": tool_name,
            "error_field": error_field,
            "message": _SCHEMA_DRIFT_MESSAGE,
        }
    )


def get_recent_schema_drift() -> list[dict[str, Any]]:
    return list(_RECENT_SCHEMA_DRIFT)


def clear_recent_schema_drift() -> None:
    _RECENT_SCHEMA_DRIFT.clear()


def _augment_meta_observability(
    payload: dict[str, Any], ctx: McpErrorContext, latency_ms: float
) -> None:
    """Stamp request_id (if missing) and latency_ms onto an envelope's ``_meta``."""
    meta = payload.setdefault("_meta", {})
    if ctx.request_id and "request_id" not in meta:
        meta["request_id"] = ctx.request_id
    meta["latency_ms"] = latency_ms


async def run_mcp_tool(
    tool_name: str,
    call: Callable[[], Awaitable[dict[str, Any]]],
    *,
    context: McpErrorContext | None = None,
) -> dict[str, Any]:
    """Execute an MCP tool body, converting any exception to an envelope dict.

    Returning the envelope (rather than raising) means the LLM sees a structured
    failure instead of an `isError: true` MCP response with an opaque message.
    Every response — success or error — carries an observability ``_meta`` block
    (``request_id``, ``latency_ms``) and a structured server-side log line keyed
    by ``tool`` + ``request_id``.
    """
    ctx = context or McpErrorContext(tool_name=tool_name)
    if ctx.request_id is None:
        ctx.request_id = uuid4().hex
    start = time.perf_counter()
    try:
        result = await call()
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        # Inject research-use meta into every successful dict response unless
        # the tool already provides _meta. A symmetric success:true flag lets
        # callers branch on `success` instead of special-casing `is False`.
        if isinstance(result, dict):
            result.setdefault("success", True)
            existing_meta: dict[str, Any] = result.get("_meta") or {}
            result["_meta"] = {
                **existing_meta,
                **_provenance_meta(ctx),
                "latency_ms": latency_ms,
            }
        logger.info(
            "mcp_tool_ok tool=%s request_id=%s latency_ms=%s",
            tool_name,
            ctx.request_id,
            latency_ms,
        )
        return result
    except McpToolError as exc:
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        _augment_meta_observability(exc.payload, ctx, latency_ms)
        record_mcp_error(
            tool_name=tool_name,
            error_code=exc.payload.get("error_code", "internal_error"),
            exc_type=exc.__class__.__name__,
        )
        return exc.payload
    except Exception as exc:  # broad catch is the error-boundary contract
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        wrapped = mcp_tool_error(exc, ctx)
        _augment_meta_observability(wrapped.payload, ctx, latency_ms)
        logger.warning(
            "mcp_tool_error tool=%s code=%s request_id=%s latency_ms=%s exc=%s",
            tool_name,
            wrapped.payload["error_code"],
            ctx.request_id,
            latency_ms,
            exc.__class__.__name__,
        )
        record_mcp_error(
            tool_name=tool_name,
            error_code=wrapped.payload["error_code"],
            exc_type=exc.__class__.__name__,
        )
        return wrapped.payload
