"""Security guard: the diagnostics rings must not retain raw exception or raw
SDK-validation text.

Both ``_RECENT_ERRORS`` (surfaced by ``get_recent_errors``) and
``_RECENT_SCHEMA_DRIFT`` (surfaced by ``get_recent_schema_drift``) are process
diagnostics that an operator/LLM can read back. Raw exception messages and raw
output-validation strings can embed user-supplied identifiers (VCV / rsID /
HGVS / free-text queries) which may be GDPR Art. 9 patient-derived data. Only
low-cardinality, non-PII fields (tool name, error code, exception *type*,
parsed schema field) may be stored. Same finding class as gnomad-link M4.

Research use only; not clinical decision support.
"""

from __future__ import annotations

import json

from clinvar_link.exceptions import DataNotFoundError
from clinvar_link.mcp.errors import (
    McpErrorContext,
    clear_recent_errors,
    clear_recent_schema_drift,
    get_recent_errors,
    get_recent_schema_drift,
    run_mcp_tool,
)
from clinvar_link.mcp.output_validation import actionable_output_validation_error

# A sentinel that would only appear if raw exception / validation text leaked.
_PII_SENTINEL = "NM_033380.3(COL4A5):c.1871G>A_PATIENT_SECRET"


async def test_recent_errors_ring_drops_raw_exception_text() -> None:
    clear_recent_errors()

    async def call() -> dict[str, object]:
        # Exception text embeds a patient-derived identifier.
        raise DataNotFoundError(f"no ClinVar record for {_PII_SENTINEL}")

    await run_mcp_tool(
        "get_variant",
        call,
        context=McpErrorContext(tool_name="get_variant", query=_PII_SENTINEL),
    )

    errors = get_recent_errors()
    assert errors, "expected a recorded diagnostics entry"
    assert _PII_SENTINEL not in json.dumps(errors)
    entry = errors[-1]
    # Only the three non-PII fields are stored.
    assert set(entry) == {"tool_name", "error_code", "exc_type"}
    assert entry["tool_name"] == "get_variant"
    assert entry["error_code"] == "not_found"
    assert entry["exc_type"] == "DataNotFoundError"


async def test_schema_drift_ring_drops_raw_validation_text() -> None:
    clear_recent_schema_drift()
    clear_recent_errors()

    raw = f"Output validation error: 'clinvar_release_date' is a required property; {_PII_SENTINEL}"
    actionable_output_validation_error(
        tool_name="get_variant",
        arguments={},
        message=raw,
    )

    drift = get_recent_schema_drift()
    assert drift, "expected a recorded schema-drift entry"
    assert _PII_SENTINEL not in json.dumps(drift)
    entry = drift[-1]
    # Only the parsed field is kept; the message is fixed, not the raw SDK text.
    assert entry["tool_name"] == "get_variant"
    assert entry["error_field"] == "clinvar_release_date"
    assert isinstance(entry["message"], str) and entry["message"]
    assert _PII_SENTINEL not in entry["message"]

    # The general error ring (also fed by output validation) must stay clean too.
    assert _PII_SENTINEL not in json.dumps(get_recent_errors())
