"""Annotated parameter types for the ClinVar Link tools (TOOL-SCHEMA-DOCUMENTATION-STANDARD v1).

The `description` on an input property is what the model actually reads to choose an argument;
`outputSchema` is what it does not. Declaring them here — once, next to the vocabulary they come
from — keeps every tool's schema documented (S1), exemplified (S2/S3), enum-bounded (S4) and
numerically bounded, without a per-tool copy that can drift.

Two shapes of closed vocabulary appear below, and the difference is deliberate:

* ``Literal[...]`` — pydantic itself rejects an out-of-enum value before the tool body runs
  (``id_type``, ``response_mode``, ``sort``, ``match_mode``, ``count_mode``). The vocabulary is
  server-facing; the enum in the schema is the whole documentation.
* ``str`` + an ``enum`` in ``json_schema_extra`` (``classification``, ``assembly``) — the schema
  advertises the canonical tokens, and the RUNTIME additionally accepts ClinVar's own published
  wording ("Likely pathogenic", "hg19") and normalises it. The runtime is a superset of the
  advertised enum, never a subset: an unrecognised value is still rejected with ``invalid_input``
  (``services/clinvar_service.py``), never silently matched to nothing.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from clinvar_link.models.enums import (
    ASSEMBLY_VALUES,
    CLASSIFICATION_VALUES,
)

# NB: the accepted tokens are NOT re-listed here — the `enum` in the schema carries them, and
# duplicating them cost ~25 tokens on every request for information the model already has
# (TOOL-SURFACE-BUDGET-STANDARD B1). What the enum CANNOT say is said here: the upstream's own
# wording is accepted, and anything else is an error rather than an empty result.
_CLASSIFICATION_DESC = (
    "Filter by normalized ClinVar classification (accepted tokens are the enum). ClinVar's own "
    "wording is accepted and normalized ('Likely pathogenic' -> likely_pathogenic). Any other "
    "value is REJECTED with invalid_input — an unrecognized classification never silently "
    "returns zero rows. Omit for all."
)

_ASSEMBLY_DESC = (
    "Filter to variants that have coordinates on this reference assembly. "
    "GRCh38 or GRCh37 ('hg38'/'hg19' are accepted and normalized). Omit for either."
)

Identifier = Annotated[
    str,
    Field(
        description=(
            "A single ClinVar variant identifier. Accepts a VCV accession (VCV000007105), a "
            "dbSNP rsID (rs334), an HGVS expression (NM_000059.3:c.1234A>G — the (GENE) "
            "qualifier is optional), a ClinVar AlleleID, or a VariationID. Use id_type to force "
            "one interpretation; the default 'auto' detects the shape."
        ),
        examples=["VCV000007105", "rs334", "NM_007294.4:c.5266dupC"],
        min_length=1,
        max_length=512,
    ),
]

Identifiers = Annotated[
    list[str],
    Field(
        description=(
            "A LIST of ClinVar variant identifiers resolved in one call (the batch form of "
            "get_variant). Shapes may be mixed (VCV / rsID / HGVS / AlleleID / VariationID). "
            "Every input yields one result row echoing its identifier and a `found` flag, so a "
            "miss is explicit and never silently dropped. Capped at 100 per call."
        ),
        examples=[["VCV000007105", "rs334"]],
        min_length=1,
        max_length=100,
    ),
]

IdTypeParam = Annotated[
    Literal["auto", "vcv", "variation_id", "rsid", "hgvs", "allele_id"],
    Field(
        description=(
            "How to interpret `identifier`. 'auto' (default) detects the shape from the value; "
            "the explicit types force one lookup and reject a value of the wrong shape."
        ),
        examples=["auto"],
    ),
]

ResponseModeParam = Annotated[
    Literal["minimal", "compact", "standard", "full"],
    Field(
        description=(
            "Payload verbosity, cheapest first: minimal = ids + classification + stars; compact "
            "(default) adds name, review status, traits; standard adds coordinates, RCVs, "
            "consequence; full adds all fields. Start compact, widen if needed."
        ),
        examples=["compact"],
    ),
]

GeneSymbol = Annotated[
    str,
    Field(
        description=(
            "An HGNC gene symbol (case-insensitive), e.g. BRCA1, TP53, CFTR. A symbol with no "
            "ClinVar variants in the local index returns not_found, never an empty success."
        ),
        examples=["BRCA1"],
        min_length=1,
        max_length=64,
    ),
]

GeneSymbolFilter = Annotated[
    str | None,
    Field(
        description=(
            "Restrict the search to this HGNC gene symbol. An explicit value always wins; when "
            "omitted, a symbol in the query text is applied automatically (reported as "
            "_meta.search.gene_symbol_inferred) so loose text narrows within the gene, not into "
            "unrelated ones. An unknown symbol is not_found, never an empty page."
        ),
        examples=["BRCA1"],
        max_length=64,
    ),
]

Query = Annotated[
    str,
    Field(
        description=(
            "Free text matched against variant names, gene symbols and trait names — typically a "
            "gene symbol plus a change ('BRCA1 c.5266dup'). Terms are ANDed, degrading to OR (and "
            "then to gene-only) when nothing matches all of them; any degradation is declared in "
            "_meta.search, never presented as a confident ranking."
        ),
        examples=["BRCA1 c.5266dup"],
        min_length=1,
        max_length=512,
    ),
]

ClassificationFilter = Annotated[
    str | None,
    Field(
        description=_CLASSIFICATION_DESC,
        examples=["likely_pathogenic"],
        json_schema_extra={"enum": [*CLASSIFICATION_VALUES, None]},
    ),
]

AssemblyFilter = Annotated[
    str | None,
    Field(
        description=_ASSEMBLY_DESC,
        examples=["GRCh38"],
        json_schema_extra={"enum": [*ASSEMBLY_VALUES, None]},
    ),
]

MinStars = Annotated[
    int | None,
    Field(
        description=(
            "Keep only variants with at least this many ClinVar review-status gold stars (0-4): "
            "2 = multiple submitters, no conflicts; 3 = expert panel; 4 = practice guideline."
        ),
        examples=[2],
        ge=0,
        le=4,
    ),
]

SortParam = Annotated[
    Literal["stars_desc", "stars_asc", "name", "variation_id"],
    Field(
        description=(
            "Row order. stars_desc (default) puts the highest review confidence first; "
            "stars_asc reverses it; name sorts by variant name; variation_id by ClinVar ID."
        ),
        examples=["stars_desc"],
    ),
]

MatchModeParam = Annotated[
    Literal["auto", "and", "or"],
    Field(
        description=(
            "Token matching for the query text. auto (default) requires ALL terms and falls back "
            "to ANY only when that matches nothing (the fallback is declared in _meta.search). "
            "and/or force one mode."
        ),
        examples=["auto"],
    ),
]

CountModeParam = Annotated[
    Literal["exact", "none"],
    Field(
        description=(
            "exact (default) returns total_count (bounded by an internal scan cap, which sets "
            "total_count_capped when hit). none skips the count query for lowest latency."
        ),
        examples=["exact"],
    ),
]

Limit = Annotated[
    int,
    Field(
        description="Maximum rows to return in this page (1-100).",
        examples=[20],
        ge=1,
        le=100,
    ),
]

Offset = Annotated[
    int,
    Field(
        description=(
            "Rows to skip before this page; use the response's next_offset to paginate. "
            "Rows beyond total_count return an empty page, never an error."
        ),
        examples=[0],
        ge=0,
    ),
]

RequestId = Annotated[
    str | None,
    Field(
        description=(
            "Opaque id echoed back in _meta.request_id to correlate a response with server "
            "logs. Omit and the server mints one."
        ),
        examples=["req-42"],
        max_length=128,
    ),
]
