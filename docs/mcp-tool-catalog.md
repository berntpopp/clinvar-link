# MCP tool catalog

The registered tool surface, its parameters, and the response contract every
tool shares. Leaf names are **unprefixed** per Tool-Naming Standard v1 — behind
`genefoundry-router` they surface as `clinvar_<tool>`.

## Signatures

```text
get_variant(identifier, id_type="auto", response_mode="compact", request_id?)
get_variants(identifiers, id_type="auto", response_mode="compact", request_id?)
search_variants(query, gene_symbol?, classification?, min_stars?, assembly?,
                limit=20, offset=0, response_mode="compact", request_id?)
get_gene_clinvar_summary(gene_symbol, response_mode="compact", request_id?)
get_variants_by_gene(gene_symbol, classification?, min_stars?, sort="stars_desc",
                     limit=50, offset=0, response_mode="compact", request_id?)
get_server_capabilities(request_id?)
```

Every tool is annotated **`READ_ONLY_OPEN_WORLD`**.

## Identifiers and domain vocabulary

`identifier` is **auto-detected** (`id_type="auto"`) across:

- **VCV accession** — `VCV000012345`
- **VariationID** — `12345`
- **dbSNP rsID** — `rs80357906`
- **HGVS** — e.g. `NM_007294.4(BRCA1):c.5266dupC`. A clean transcript-qualified
  HGVS resolves **even without** the `(GENE)` qualifier.
- **ClinVar AlleleID**

`classification` is normalized to one of `pathogenic`, `likely_pathogenic`,
`vus`, `likely_benign`, `benign`, `conflicting`, `not_provided`, `other`.
`min_stars` filters on the official ClinVar review-status → **0–4 star rating**.
`assembly` selects `GRCh38` (the canonical row) or `GRCh37`; both assemblies'
coordinates are retained where present.

## Response contract

**`response_mode` ∈ `minimal | compact | standard | full`** (default `compact`)
controls payload size; `full` returns the unprojected payload. Start `compact`
and widen only when you need more detail.

Errors are returned as a **typed envelope, never raised**. The taxonomy is
`not_found`, `invalid_input`, `internal_error` (advertised under `error_codes` in
capabilities). Malformed input is `invalid_input`, not a false `not_found`; an
over-restrictive filter returns an **empty success**, not `not_found`.

Every response — success **and** error — carries `_meta`:

| `_meta` field | Meaning |
|---|---|
| `next_commands` | A ready-to-call `{tool, arguments}` list: the chaining contract. |
| `clinvar_release` / `clinvar_release_date` | The live ClinVar release the answer came from. |
| `data_freshness` | `{age_days, past_ttl}` against `REFRESH_TTL_DAYS`. |
| `request_id` | Accepted from the client for correlation, else minted server-side. |
| `latency_ms` | Timing hint. |
| `server_version` | Stamped on every response. |
| `citation_template` | **List responses only** (`minimal`/`compact`): the citation hoisted once — fill `{variation_id}` / `{vcv_accession}` per row. |

Single variant/gene results carry a paste-verbatim **`recommended_citation`**
instead (see [data → licence & citation](data.md#licence--citation)).

## Search and pagination

- `search_variants` defaults to **AND-mode** (`match_mode=auto` = AND with an
  automatic OR fallback when AND returns nothing).
- `count_mode` ∈ `{exact, none}` controls whether `total_count` /
  `total_count_capped` is computed (bounded by an internal cap) or skipped for
  lowest latency.
- List responses expose `total_count`, `has_more` and `next_offset`.
  `limit` is clamped to `[1, MAX_PAGE_SIZE]` (default cap 100); `offset >= 0`.
- `get_variants` (batch) echoes each row's `identifier` + `found` flag, plus
  `requested` / `found_count` / `truncated`.
- `get_gene_clinvar_summary` reports counts by classification (including an
  `other_count` bucket for classifications outside the named ones), the star
  distribution, consequence categories, top traits, and `has_pathogenic`.
- `get_variants_by_gene` sorts by `stars_desc` by default; `sort` is allow-listed
  and advertised as `sort_options` in capabilities.

## Worked example

`get_variant`:

```json
{ "identifier": "VCV000012345", "response_mode": "compact" }
```

Result (compact projection, abridged):

```json
{
  "success": true,
  "variation_id": 12345,
  "vcv_accession": "VCV000012345",
  "classification": "pathogenic",
  "star_rating": 2,
  "review_status": "criteria provided, multiple submitters, no conflicts",
  "gene_symbol": "BRCA1",
  "canonical_assembly": "GRCh38",
  "traits": ["Hereditary breast and ovarian cancer syndrome"],
  "recommended_citation": "ClinVar (NCBI). VariationID 12345 (VCV000012345). ClinVar weekly release 2026-06-15. https://www.ncbi.nlm.nih.gov/clinvar/variation/12345/",
  "_meta": { "next_commands": [ { "tool": "get_gene_clinvar_summary", "arguments": { "gene_symbol": "BRCA1" } } ] }
}
```

## Discovery

`get_server_capabilities` is the live discovery surface: the tool list, response
modes, workflows, search controls, sort options, the current ClinVar release
date, error codes and limitations. It is driven by the **live tool registry**, so
it cannot drift from what is registered.

Adding a tool means keeping `mcp/tools/` (registered), `mcp/facade.py` and
`mcp/resources._TOOLS` in lockstep — see [`AGENTS.md`](../AGENTS.md).
