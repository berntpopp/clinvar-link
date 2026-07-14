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
`assembly` selects `GRCh38` (the canonical row) or `GRCh37` (`hg38`/`hg19` are accepted and
normalized); both assemblies' coordinates are retained where present.

Every filter with a closed vocabulary declares it as an **`enum` in the input schema**
(`classification`, `assembly`, `sort`, `id_type`, `match_mode`, `count_mode`, `response_mode`),
and `classification` additionally accepts ClinVar's own published wording
("Likely pathogenic" → `likely_pathogenic`). No tool publishes an `outputSchema`
(Tool-Surface-Budget Standard v1) — `structuredContent` is unaffected.

## Response contract

**`response_mode` ∈ `minimal | compact | standard | full`** (default `compact`)
controls payload size; `full` returns the unprojected payload. Start `compact`
and widen only when you need more detail.

Errors are returned as a **typed envelope** carrying protocol **`isError: true`** — the flat
`{success:false, error_code, message, retryable, recovery_action, …}` body travels in
`structuredContent`, so a client can branch on either. The taxonomy is the fleet's closed enum:
`not_found`, `invalid_input`, `internal` (advertised under `error_codes` in capabilities).
Malformed input is `invalid_input`, never a false `not_found`, and the `message` names the
offending parameter and its accepted values.

A filter value the server does not understand is **rejected** (`invalid_input`), never answered
with an empty success — that is the silent-empty filter, and it is what made 559 pathogenic BRCA1
variants invisible to `classification="Likely pathogenic"`. A **recognized** filter that
legitimately matches nothing still returns an empty success.

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

- `search_variants` defaults to **AND-mode** (`match_mode=auto` = AND, then OR, then gene-only
  when neither matches). A gene symbol written in the query is applied as a **filter**, so loose
  text narrows *within* the gene instead of returning unrelated genes. Whatever was inferred, and
  any degradation, is declared in `_meta.search`
  (`gene_symbol_inferred` / `fallback` / `notice`) — never passed off as a confident ranking.
- `count_mode` ∈ `{exact, none}` controls whether `total_count` /
  `total_count_capped` is computed (bounded by an internal cap) or skipped for
  lowest latency.
- List responses expose `total_count`, `has_more` and `next_offset`.
  `limit` is clamped to `[1, MAX_PAGE_SIZE]` (default cap 100); `offset >= 0`.
- `get_variants` (batch) echoes each row's `identifier` + `found` flag, plus
  `requested` / `found_count` / `truncated`.
- `get_gene_clinvar_summary` reports `variant_count` (the gene's total — **not** `total_count`,
  which is reserved for a paginated result set) and counts by classification (including an
  `other_count` bucket), the star distribution, consequence categories, top traits, and
  `has_pathogenic`.
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
