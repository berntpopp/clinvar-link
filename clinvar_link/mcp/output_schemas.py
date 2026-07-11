"""JSON Schema fragments declaring the v1.1 ``untrusted_text`` shape.

Hand-written (not pydantic-generated) so tool ``output_schema=`` declarations
stay free of ``$ref``/``$defs`` indirection. The shape mirrors
:class:`clinvar_link.mcp.untrusted_content.UntrustedText` exactly; the fence
unit tests exercise that model directly, and the hostile-vector MCP tests
exercise these schemas end-to-end (a mismatch fails loudly via FastMCP's
output-schema validation, surfaced as ``output_validation_failed``).

Each fenced list surface (``traits[]``, ``top_traits[]``) gets its own item
schema so the ``kind`` literal is declared at the ARRAY-ITEM level, not just
hidden behind a permissive top-level ``additionalProperties: true``.
"""

from __future__ import annotations

from typing import Any

# The typed untrusted_text object itself: kind/text/provenance/raw_sha256.
UNTRUSTED_TEXT_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"const": "untrusted_text"},
        "text": {"type": "string"},
        "provenance": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "record_id": {"type": "string"},
                "retrieved_at": {"type": "string", "format": "date-time"},
            },
            "required": ["source", "record_id", "retrieved_at"],
        },
        "raw_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    },
    "required": ["kind", "text", "provenance", "raw_sha256"],
}

# variant traits[] items: full/standard mode wraps the fenced name alongside
# sibling ontology ids; compact mode emits the bare fenced object directly
# (trait names only, capped at 5). Both are valid per-mode shapes of the same
# field, so the item schema is the union of both.
TRAIT_ITEM_SCHEMA: dict[str, Any] = {
    "oneOf": [
        UNTRUSTED_TEXT_ITEM_SCHEMA,
        {
            "type": "object",
            "properties": {
                "name": UNTRUSTED_TEXT_ITEM_SCHEMA,
                "omim_id": {"type": ["string", "null"]},
                "medgen_id": {"type": ["string", "null"]},
                "mondo_id": {"type": ["string", "null"]},
            },
            "required": ["name"],
            "additionalProperties": True,
        },
    ]
}

# gene summary top_traits[] items: one shape, always {trait, count}.
TOP_TRAIT_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "trait": UNTRUSTED_TEXT_ITEM_SCHEMA,
        "count": {"type": "integer"},
    },
    "required": ["trait"],
}

# get_variant: traits[] is a top-level field.
VARIANT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"traits": {"type": "array", "items": TRAIT_ITEM_SCHEMA}},
    "additionalProperties": True,
}

# get_variants / search_variants / get_variants_by_gene: each row (results[])
# may itself carry traits[].
VARIANT_LIST_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"traits": {"type": "array", "items": TRAIT_ITEM_SCHEMA}},
                "additionalProperties": True,
            },
        }
    },
    "additionalProperties": True,
}

# get_gene_clinvar_summary: top_traits[] is a top-level field.
GENE_SUMMARY_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"top_traits": {"type": "array", "items": TOP_TRAIT_ITEM_SCHEMA}},
    "additionalProperties": True,
}
