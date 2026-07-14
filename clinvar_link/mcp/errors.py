"""Structured MCP error envelopes for ClinVar Link tools.

Patterned after gnomad_link/mcp/errors.py. The envelope shape is what LLMs
branch on; codes are deterministic per exception class so prompts can recover
without scraping free text.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import uuid4

from fastmcp.exceptions import ValidationError as FastMCPValidationError
from fastmcp.tools.tool import ToolResult
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
from clinvar_link.mcp.untrusted_content import (
    FORBIDDEN_CODEPOINTS,
    UntrustedTextLimitError,
    sanitize_message,
)

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


# Response-Envelope Standard v1: `error_code` is a CLOSED enum, harmonized across the fleet.
# Anything outside this set — however sensible it reads — is a violation, and a client that
# branches on the code cannot act on it. `internal_error` / `response_too_large` /
# `output_validation_failed` (this server's former codes) are mapped onto the canon.
ERROR_CODES: frozenset[str] = frozenset(
    {
        "invalid_input",
        "not_found",
        "ambiguous_query",
        "upstream_unavailable",
        "rate_limited",
        "internal",
    }
)

# Fixed, error-code-specific PUBLIC messages. A classified exception's own
# str() is built from caller input (identifiers, queries) or internal detail,
# which can carry injection prose that survives code-point stripping — so the
# caller-visible message NEVER interpolates that text. Actionable, server-authored
# guidance travels in the fixed `recovery` field and `next_commands`; the raw
# detail stays only in the (server-side) exception chain, and is never logged.
_PUBLIC_MESSAGES: dict[str, str] = {
    "not_found": "No matching ClinVar record was found for the request.",
    "invalid_input": "The request was rejected as invalid.",
    "internal": "An internal error occurred while handling the request.",
}

# The response-shaping ceiling keeps its own actionable public message while reporting the
# canonical `invalid_input` code: the caller CAN fix it (lower `limit`, leaner `response_mode`).
_RESPONSE_TOO_LARGE_MESSAGE = (
    "The response exceeded the allowed size limit; narrow the request "
    "(lower the limit parameter or use a leaner response_mode)."
)


def _strip_forbidden(text: str) -> str:
    """Code-point-strip a string WITHOUT the length cap.

    Used by the recursive whole-envelope pass, where a server-authored
    ``recovery`` string may legitimately exceed the message cap; only the
    forbidden control/zero-width/bidi/NUL code points are removed.
    """
    return "".join(char for char in text if ord(char) not in FORBIDDEN_CODEPOINTS)


def _has_forbidden_codepoints(text: str) -> bool:
    return any(ord(char) in FORBIDDEN_CODEPOINTS for char in text)


def sanitize_envelope(payload: Any) -> Any:
    """Recursively code-point-strip every string leaf of an error payload.

    The final backstop over the WHOLE envelope — message, recovery, field_errors,
    fallback_args, request_id, and every ``_meta.next_commands[*].arguments``
    value — so no forbidden control/zero-width/bidi/NUL code point survives in
    either ``structured_content`` or the ``TextContent`` JSON mirror, whatever
    path built the field. Applied ON TOP OF the fixed-message + input-rejection
    discipline (it strips code points, it does not neutralize prose); dict keys
    are server-defined and preserved as-is, only values are stripped.
    """
    if isinstance(payload, str):
        return _strip_forbidden(payload)
    if isinstance(payload, dict):
        return {key: sanitize_envelope(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [sanitize_envelope(item) for item in payload]
    return payload


_GENE_TOOLS = frozenset({"get_gene_clinvar_summary", "get_variants_by_gene"})


def _clean_context_value(value: str | None) -> str | None:
    """Normalize a context echo value for ``fallback_args`` / ``next_commands``.

    Returns the stripped value, or ``None`` when it is blank OR carries any fenced
    forbidden code point. A recovery-argument field is a ready-to-execute
    suggestion, so a code-point-bearing identifier is OMITTED rather than echoed
    as a sanitized copy (per the fence's "prefer omitting over sanitizing for
    hint/recovery/argument fields" rule).
    """
    if not value:
        return None
    stripped = value.strip()
    if not stripped or _has_forbidden_codepoints(stripped):
        return None
    return stripped


def _fallback_for(context: McpErrorContext) -> tuple[str, dict[str, Any] | None]:
    """Resolve the context-appropriate resolver tool for not_found / invalid_input.

    A failing variant lookup almost always received free text / a gene symbol;
    point it at search_variants. A failing gene tool points back at search; and
    everything else at the discovery entrypoint. fallback_args are populated from
    context so the LLM gets a ready-to-call next step.

    Whitespace-only values for query/variant_id/gene_symbol are treated as absent
    so blank strings are never echoed into fallback_args or next_commands; values
    carrying forbidden code points are likewise omitted (see _clean_context_value).
    """
    query = _clean_context_value(context.query)
    variant_id = _clean_context_value(context.variant_id)
    gene_symbol = _clean_context_value(context.gene_symbol)
    if context.tool_name == "get_variant":
        if query:
            return "search_variants", {"query": query}
        if variant_id:
            return "search_variants", {"query": variant_id}
        return "search_variants", None
    if gene_symbol:
        return "get_gene_clinvar_summary", {"gene_symbol": gene_symbol}
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
    if isinstance(exc, UntrustedTextLimitError):
        # A v1.1 fenced-text ceiling (object count / per-object / total bytes) was exceeded.
        # The caller CAN fix it (lower `limit`, leaner `response_mode`), so it reports the
        # canonical `invalid_input` code with its own actionable message rather than an
        # off-enum code or an unactionable `internal`.
        return "invalid_input", False, _FALLBACK_TOOL, {}
    if isinstance(exc, ValueError):
        return "invalid_input", False, _FALLBACK_TOOL, {}
    if isinstance(exc, ClinVarDataError):
        return "internal", False, _FALLBACK_TOOL, {}
    if isinstance(exc, ClinVarServerError):
        return "internal", False, _FALLBACK_TOOL, {}
    return "internal", False, _FALLBACK_TOOL, {}


def _recovery_action(error_code: str, retryable: bool) -> str:
    """Action-typed guidance so the LLM does not infer behavior from a bare bool.

    retry_backoff (wait + retry same call) | reformulate_input (fix the id/fields,
    same tool) | switch_tool (call the fallback_tool, then the original).
    """
    if retryable:
        return "retry_backoff"
    if error_code == "invalid_input":
        return "reformulate_input"
    return "switch_tool"


def _public_detail(exc: BaseException) -> str | None:
    """A FIXED, server-authored public message for exceptions that carry one.

    A rejected argument is only actionable if the message names the PARAMETER and its accepted
    values ("The request was rejected as invalid." names nothing, and the old gene-tool recovery
    text pointed at `gene_symbol` — the one argument that was already correct). Both halves here
    are server-authored constants: the parameter name is a declared identifier and the reason is
    built from this server's own vocabulary. The caller's rejected VALUE is never echoed.
    """
    if isinstance(exc, UntrustedTextLimitError):
        return _RESPONSE_TOO_LARGE_MESSAGE
    if isinstance(exc, ToolInputError) and exc.field and exc.public_reason:
        field = _safe_field_name((exc.field,))
        return f"Invalid value for parameter '{field}': {exc.public_reason}"
    return None


def _field_errors_for(exc: BaseException) -> list[dict[str, str]] | None:
    """The structured {field, reason} form of a self-describing input error."""
    if isinstance(exc, ToolInputError) and exc.field and exc.public_reason:
        return [{"field": _safe_field_name((exc.field,)), "reason": exc.public_reason}]
    return None


def _recovery_text(
    error_code: str,
    fallback_tool: str | None,
    tool_name: str | None = None,
    exc: BaseException | None = None,
) -> str:
    is_gene = tool_name in _GENE_TOOLS
    if error_code == "invalid_input" and isinstance(exc, ToolInputError) and exc.field:
        field = _safe_field_name((exc.field,))
        return (
            f"Fix the '{field}' argument and call the same tool again; every other argument was "
            "accepted. Do not retry unchanged. field_errors names the parameter and its accepted "
            "values."
        )
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
        if isinstance(exc, UntrustedTextLimitError):
            return (
                "The response this call would produce is too large. Re-issue it with a lower "
                "limit (e.g. 10) or response_mode='minimal'; do not retry unchanged."
            )
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


def _envelope_message(error_code: str, exc: BaseException | None = None) -> str:
    """Return a FIXED public message: the exception's server-authored detail, else the code's.

    NEVER interpolates caller input or exception text: a classified exception's
    own str() is built from the caller's identifier/query (or internal detail)
    and can carry injection prose that code-point stripping does not remove, so
    the surfaced message is either a fixed server-authored string keyed by the
    classified error code, or the exception's own ``public_reason`` — which is a
    server-authored constant naming the parameter and its accepted values (see
    :func:`_public_detail`), never the rejected value.
    """
    detail = _public_detail(exc) if exc is not None else None
    if detail:
        return detail
    return _PUBLIC_MESSAGES.get(error_code, "The request could not be completed.")


# Map a pydantic error `type` to a FIXED reason. The pydantic `msg` can echo the
# rejected input value and the `loc` (for an unexpected keyword argument) is a
# caller-controlled name, so neither is surfaced verbatim.
_PYDANTIC_REASONS: dict[str, str] = {
    "missing": "required field is missing",
    "missing_argument": "required argument is missing",
    "int_parsing": "expected an integer",
    "int_type": "expected an integer",
    "float_parsing": "expected a number",
    "float_type": "expected a number",
    "string_type": "expected a string",
    "bool_parsing": "expected a boolean",
    "bool_type": "expected a boolean",
    "list_type": "expected a list",
    "dict_type": "expected an object",
    "value_error": "value was rejected as invalid",
    "greater_than": "value is out of range",
    "greater_than_equal": "value is out of range",
    "less_than": "value is out of range",
    "less_than_equal": "value is out of range",
    "unexpected_keyword_argument": "unexpected argument",
    "extra_forbidden": "unexpected argument",
}
# For these error types the ``loc`` is a CALLER-INVENTED argument name (not a
# declared parameter), so it is redacted wholesale rather than echoed.
_UNKNOWN_ARG_TYPES = frozenset({"unexpected_keyword_argument", "extra_forbidden"})
_SAFE_FIELD_NAME_RE = re.compile(r"^[A-Za-z0-9_.]{1,64}$")


def _safe_field_name(loc: Any) -> str:
    """Return a code-point-free, identifier-validated declared field name.

    The ``loc`` of a normal field error names a DECLARED parameter (safe to echo);
    it is still code-point-stripped and shape-validated, collapsing to ``"unknown"``
    if it is not identifier-shaped.
    """
    name = sanitize_message(".".join(str(part) for part in loc) if loc else "")
    return name if _SAFE_FIELD_NAME_RE.match(name) else "unknown"


def _extract_field_errors(errors: list[Any]) -> list[dict[str, str]]:
    """Flatten pydantic validation errors into {field, reason} dicts.

    Both members are fixed/redacted: an unexpected-argument name (caller-invented)
    is redacted to ``"unknown"``; a declared field name is code-point-stripped and
    identifier-validated; and the reason is a FIXED string keyed by the pydantic
    error ``type`` — never the pydantic ``msg``, which can echo the rejected input.
    """
    result: list[dict[str, str]] = []
    for err in errors:
        etype = str(err.get("type", "invalid"))
        field_name = (
            "unknown" if etype in _UNKNOWN_ARG_TYPES else _safe_field_name(err.get("loc", ()))
        )
        reason = _PYDANTIC_REASONS.get(etype, "value was rejected as invalid")
        result.append({"field": field_name, "reason": reason})
    return result


def _constraint_of(prop: dict[str, Any]) -> str | None:
    """Describe a DECLARED constraint (enum / numeric bound) from the tool's own schema.

    Everything here is server-authored — the property names, the enum members and the bounds all
    come from the schema this server advertises — so it is safe to surface verbatim. This is what
    lets the message be actionable ("sort must be one of: stars_desc, …") without ever echoing
    the caller's rejected value, and it cannot drift: it IS the advertised schema.
    """
    branches = [prop, *(b for b in prop.get("anyOf") or [] if isinstance(b, dict))]
    for branch in branches:
        values = branch.get("enum")
        if isinstance(values, list) and values:
            allowed = ", ".join(str(v) for v in values if v is not None)
            return f"must be one of: {allowed}"
    for branch in branches:
        low, high = branch.get("minimum"), branch.get("maximum")
        if low is not None and high is not None:
            return f"must be between {low} and {high}"
        if low is not None:
            return f"must be at least {low}"
        if high is not None:
            return f"must be at most {high}"
    return None


def _validation_message(field_errors: list[dict[str, str]], schema: dict[str, Any] | None) -> str:
    """A message the model can act on: it names the parameters and their accepted values.

    The old message — "The request was rejected as invalid." — named nothing, so a model had
    nothing to self-correct from (MCP: "Tool Execution Errors contain actionable feedback that
    language models can use to self-correct").
    """
    properties: dict[str, Any] = (schema or {}).get("properties") or {}
    parts: list[str] = []
    for err in field_errors:
        name, reason = err["field"], err["reason"]
        if name == "unknown":
            parts.append("The request included an argument this tool does not accept.")
            continue
        constraint = _constraint_of(properties.get(name) or {})
        parts.append(f"{name} {constraint}." if constraint else f"{name}: {reason}.")
    if properties:
        parts.append("Accepted parameters: " + ", ".join(properties) + ".")
    return " ".join(parts) if parts else _PUBLIC_MESSAGES["invalid_input"]


def _validation_error_payload(
    field_errors: list[dict[str, str]],
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the fixed arg-validation envelope (recursively code-point-stripped).

    The public ``message`` is built ONLY from the tool's own advertised schema plus the
    fixed/redacted ``field_errors`` (see :func:`_extract_field_errors`); the final
    :func:`sanitize_envelope` pass is a defensive backstop over every leaf. The caller's
    rejected value is never part of it.
    """
    payload: dict[str, Any] = {
        "success": False,
        "error_code": "invalid_input",
        "message": _validation_message(field_errors, schema),
        "retryable": False,
        "recovery_action": "reformulate_input",
        "fallback_tool": _FALLBACK_TOOL,
        "fallback_args": {},
        "field_errors": field_errors,
        "recovery": (
            "Inputs failed validation. Check field_errors for the field + reason, fix those "
            f"arguments and call the same tool again; call {_FALLBACK_TOOL} for the accepted "
            "tool surface and identifier shapes."
        ),
        "_meta": {
            "next_commands": [{"tool": _FALLBACK_TOOL, "arguments": {}}],
            **_provenance_meta(),
        },
    }
    return cast(dict[str, Any], sanitize_envelope(payload))


def mcp_validation_tool_error(
    *,
    tool_name: str,
    exc: PydanticValidationError,
    schema: dict[str, Any] | None = None,
) -> McpToolError:
    """Build a sanitized validation failure raised before tool execution starts."""
    return McpToolError(
        _validation_error_payload(_extract_field_errors(list(exc.errors())), schema)
    )


class _ValidationLogFilter(logging.Filter):
    """Drop FastMCP's arg-validation WARNING record.

    FastMCP logs ``"Invalid arguments for tool %r: %s"`` with the pydantic error
    detail, which embeds the raw caller-supplied argument value/name (including
    forbidden code points), inside its own tool-call handler. Suppress that
    specific record so caller input never lands in a server log; the caller still
    receives a fixed, sanitized envelope from :func:`install_validation_error_handler`.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.msg if isinstance(record.msg, str) else ""
        return not msg.startswith("Invalid arguments for tool")


_VALIDATION_LOG_FILTER = _ValidationLogFilter()


def _install_validation_log_filter() -> None:
    """Idempotently attach the arg-validation log filter to FastMCP's logger."""
    fastmcp_logger = logging.getLogger("fastmcp.server.server")
    if not any(isinstance(f, _ValidationLogFilter) for f in fastmcp_logger.filters):
        fastmcp_logger.addFilter(_VALIDATION_LOG_FILTER)


def _pydantic_cause(exc: BaseException) -> PydanticValidationError | None:
    """Return the pydantic ValidationError in an exception's cause chain, if any."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, PydanticValidationError):
            return cur
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return None


def install_validation_error_handler(mcp_server: Any) -> None:
    """Wrap registered tools so FastMCP argument validation returns our envelope.

    FastMCP stores tools on ``_local_provider._components`` (modern path) or the
    legacy ``_tool_manager._tools`` mapping. We probe both so the handler keeps
    working across FastMCP minor versions. Tools without a ``run`` method (e.g.
    resources or prompts that happen to share the registry) are skipped.

    FastMCP 3.x re-raises pydantic argument-validation failures as its OWN
    ``fastmcp.exceptions.ValidationError`` (with the pydantic error in
    ``__cause__``), NOT a bare pydantic error — so we catch both. Otherwise the
    FastMCP error surfaces the raw offending argument value/name (with code
    points) verbatim to the caller.
    """
    _install_validation_log_filter()
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
            except (PydanticValidationError, FastMCPValidationError) as exc:
                pyd = exc if isinstance(exc, PydanticValidationError) else _pydantic_cause(exc)
                field_errors = _extract_field_errors(list(pyd.errors())) if pyd is not None else []
                schema = getattr(_tool, "parameters", None)
                envelope = _validation_error_payload(
                    field_errors, schema if isinstance(schema, dict) else None
                )
                record_mcp_error(
                    tool_name=str(getattr(_tool, "name", "unknown")),
                    error_code="invalid_input",
                    exc_type=exc.__class__.__name__,
                )
                # Response-Envelope v1: "isError: true is REQUIRED so clients surface the error
                # to the model for self-correction." A returned dict never sets it, and raising
                # would throw the structured envelope away (_make_error_result emits
                # structuredContent=null) — ToolResult is the only shape that carries both.
                return error_tool_result(envelope)

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
    payload: dict[str, Any] = {
        "success": False,
        "error_code": error_code,
        "message": _envelope_message(error_code, exc),
        "retryable": retryable,
        "recovery_action": _recovery_action(error_code, retryable),
        "fallback_tool": fallback_tool,
        "fallback_args": fallback_args,
        "recovery": _recovery_text(error_code, fallback_tool, context.tool_name, exc),
        "_meta": {
            "tool": context.tool_name,
            "next_commands": next_commands,
            **_provenance_meta(context),
        },
    }
    field_errors = _field_errors_for(exc)
    if field_errors is not None:
        payload["field_errors"] = field_errors
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


def error_tool_result(envelope: dict[str, Any]) -> ToolResult:
    """Wrap an error envelope so it carries BOTH the MCP ``isError`` flag and the structure.

    Response-Envelope Standard v1: "``isError: true`` is REQUIRED so clients surface the error to
    the model for self-correction." A tool that returns a plain dict never sets it, so a client
    branching on ``isError`` saw every failure — not_found, invalid_input, internal — as a
    SUCCESSFUL call and handed the error envelope to the model as if it were data.

    Raising is not the alternative: FastMCP's raise path sets ``isError`` but emits
    ``structuredContent: null``, throwing the machine-readable envelope away. ``ToolResult`` is
    the only shape that carries both, and the envelope contents are unchanged.
    """
    return ToolResult(structured_content=envelope, is_error=True)


async def run_mcp_tool(
    tool_name: str,
    call: Callable[[], Awaitable[dict[str, Any]]],
    *,
    context: McpErrorContext | None = None,
) -> dict[str, Any] | ToolResult:
    """Execute an MCP tool body, converting any exception to an error envelope.

    A SUCCESS returns the plain dict (FastMCP serializes it to ``structuredContent``). A FAILURE
    returns a :class:`ToolResult` carrying the same flat envelope plus protocol ``isError: true``
    — the LLM still sees a structured, actionable failure, and a client branching on ``isError``
    now sees a failure too. Every response — success or error — carries an observability ``_meta``
    block (``request_id``, ``latency_ms``) and a structured server-side log line keyed by ``tool``
    + ``request_id``.
    """
    ctx = context or McpErrorContext(tool_name=tool_name)
    if ctx.request_id is None:
        ctx.request_id = uuid4().hex
    # request_id is caller-supplied for correlation; strip forbidden code points
    # so it cannot inject controls into a log line or the echoed _meta.request_id.
    ctx.request_id = _strip_forbidden(ctx.request_id)
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
            error_code=exc.payload.get("error_code", "internal"),
            exc_type=exc.__class__.__name__,
        )
        # Final recursive backstop: no forbidden code point survives on any leaf.
        return error_tool_result(cast(dict[str, Any], sanitize_envelope(exc.payload)))
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
        # Final recursive backstop: no forbidden code point survives on any leaf.
        return error_tool_result(cast(dict[str, Any], sanitize_envelope(wrapped.payload)))
