# clinvar-link: Harden Beyond 9/10 — Design Spec

**Date:** 2026-06-17
**Status:** Approved (design); implementation plan at `docs/superpowers/plans/2026-06-17-clinvar-link-9plus.md`
> Historical record — This design records implementation history; the live MCP registry is authoritative.

**Branch:** `improve/clinvar-link-9plus`
**Author:** Bernt Popp (with Claude Code, expert-MCP-tester pass)

## 1. Background

A black-box MCP test pass (33 calls) scored the **live** `clinvar-link` server
~7/10 and surfaced ~11 issues. Two follow-up passes against the **current source
tree** (an Explore agent, then a direct read of every relevant module) showed
the **deployed server is a stale build**: the source already fixes most reported
issues. This spec targets only what is genuinely open in the current source,
plus a small set of deliberate enhancements.

### 1.1 Already implemented in source (verified by reading the code)

Do **not** re-build these. Where a regression test is missing, add one; most
already exist.

| Reported / specced | Source status | Evidence |
| --- | --- | --- |
| `output_cheatsheet` wrong field names | Correct | `mcp/resources.py:72-81` |
| `search_variants` lacks `total`/`has_more` | Present | `_pagination()` on both list tools (`services/clinvar_service.py:346`); `test_search_pagination_metadata` |
| No batch tool | Exists | `get_variants` (`mcp/tools/variants.py:63`); `test_get_variants_batch_mixed` |
| Per-row citation bloat | Hoisted in lean modes | `_lean_list()` (`clinvar_service.py:364`); `test_compact_list_drops_cdna_change_duplicate` |
| Bare `transcript:c.` HGVS fails | Resolves first-try | `_get_by_hgvs_gene_insensitive` (`repository.py:140`); `test_get_by_hgvs_resolves_gene_unqualified_nucleotide` |
| `request_id` correlation | Minted + echoed | `run_mcp_tool` (`errors.py:388-405`) |
| `latency_ms` + structured logs | Stamped on success & error | `errors.py:394-441` |
| Cold-start release on **data path** | Primed during call | `_release_date()` (`clinvar_service.py:85-98`) runs before `_meta` is built |
| FastMCP arg-validation → `invalid_input` | Installed | `install_validation_error_handler` (`errors.py:241`) |
| Output-schema drift envelope + ring | Installed | `output_validation.py`; `record_schema_drift` (`errors.py:339`) |
| `assembly` filter | Works | `_search_filters` EXISTS subquery (`repository.py:312`); `test_search_filters` |
| `hgvs4variation` multi-transcript | Implemented (gated by source path) | `repo_with_hgvs`; `test_get_by_hgvs_resolves_hgvs4variation_forms` |

### 1.2 Out of scope

- **Redeploy** so `deployed == source` (maintainer owns deploy). This spec adds a
  self-reported freshness signal so any client can *detect* staleness.
- New transports, write paths, live eUtils. Research-use-only contract unchanged.

## 2. Genuinely-open work (confirmed by line-level read)

| # | Defect | Root cause (file:line) |
| --- | --- | --- |
| O1 | Negative/zero `limit` unbounded/empty | `clinvar_service.py:252,315` clamp upper bound only (`min(limit, MAX)`); tool params have no `Field` constraint; `repository` unguarded → SQLite `LIMIT -1` = all rows |
| O2 | `get_variants_by_gene` raises `not_found` on an over-restrictive **filter** | `clinvar_service.py:322` `if total == 0: raise` — cannot distinguish "unknown gene" from "filter excluded everything"; inconsistent with `search_variants` ([] ) and out-of-range offset (succeeds) |
| O3 | `id_type="auto"` does no shape validation; bad explicit `id_type` silently treated as auto | `_resolve_auto` (`clinvar_service.py:148-164`) falls through to `_maybe_allele_id`; garbage → `not_found` with a **false** "well-formed but absent" message; `_resolve` has no `id_type` allowlist |
| O4 | `sort` unvalidated; only `stars_desc` implemented | `repository.py:337` — `stars_asc`/`name`/garbage all silently → `ORDER BY variation_id` |
| O5 | Bare empty `query` is a match-all | `repository._search_like` `%%` (`repository.py:233`); service does not reject. (Filtered empty-query is intentional and tested — must be preserved.) |
| O6 | Residual cold-start `clinvar_release:"unknown"` | A tool that errors in FastMCP arg-validation (before the service primes the cache) emits the `"unknown"` sentinel (`resources.py:18`, `errors.py:85`). Data path already covered. |

## 3. Goals / non-goals

**Goals**
- Close O1–O5 so every malformed input returns `invalid_input` with a
  **truthful** message; no silent dump, miscategorization, or misleading text.
- Add a freshness signal (`age_days`, `past_ttl`) to capabilities and `_meta`.
- Add a static **capabilities↔model drift-guard test** (complements the runtime
  drift ring).
- Add regression tests only where missing.

**Non-goals**
- `outputSchema` declarations on tools: **deferred (YAGNI).** Tool payloads vary
  by `response_mode` (minimal/compact/standard/full), so a strict output schema
  would reject lean projections; the existing runtime drift ring + the new static
  drift-guard test cover the real risk at lower cost.
- O6 full fix beyond a best-effort startup prime: **deferred/optional** — the data
  path already covers the common case.
- `submission_summary` per-submitter detail (heavy, ~2× DB): **deferred** behind
  the existing `CLINVAR_LINK_ENABLE_SUBMISSION_SUMMARY` flag until a consumer
  needs it.

## 4. Design decisions

- **O1:** Add `Field(default=…, ge=1)` to `limit` and `Field(default=0, ge=0)` to
  `offset` on `search_variants` and `get_variants_by_gene` → invalid bounds become
  `invalid_input` via the installed validation handler. Keep the service-side
  **upper** clamp `min(limit, MAX_PAGE_SIZE)` (friendly, preserves "limit=1000 →
  100" callers) and add a lower clamp `max(1, …)` / `max(0, offset)` as
  defense-in-depth for direct service callers. Leave `get_variants` batch as-is
  (it already truncates gracefully and sets `truncated`).
- **O2:** When the filtered `total == 0`, run one unfiltered
  `count_variants_by_gene(gene)`. If that is also 0 → `not_found` ("gene not in
  index"). Otherwise return `success, results: [], count: 0, total_count: 0` with
  `next_commands` back to `get_gene_clinvar_summary`.
- **O3:** Add `_ID_TYPES = {auto, vcv, variation_id, rsid, hgvs, allele_id}`;
  `_resolve` raises `ToolInputError` for an unknown `id_type`. In `_resolve_auto`,
  if the text matches no known shape (rsID / VCV / all-digits / HGVS via `:` or a
  `c.`/`p.`/`g.`/`n.` hint), raise `ToolInputError` with a truthful message
  ("unrecognized identifier shape … or call search_variants") instead of
  returning `None`. Recognized-shape-but-absent keeps `not_found` (its message is
  now accurate).
- **O4:** Define `ClinVarRepository.SORT_ORDERS` (table-driven, injection-safe):
  `stars_desc`, `stars_asc`, `name`, `variation_id`. `variants_by_gene` looks up
  the fragment (default `stars_desc`). Service raises `ToolInputError` for a sort
  not in `SORT_ORDERS`. Advertise the options in `get_server_capabilities`.
- **O5:** In `service.search_variants`, reject a blank `query` only when **no**
  filter (`gene_symbol` / `classification` / `min_stars`) is present →
  `invalid_input`. The repository LIKE path and its test are untouched.
- **Freshness:** New pure helper `clinvar_freshness(release_date, now)` →
  `{age_days, past_ttl}` using `REFRESH_TTL_DAYS`. Merged into `_provenance_meta`
  (when a date is cached) and the capabilities payload. `now` is injectable for
  deterministic tests.
- **Drift guard:** A test asserting every `output_cheatsheet.*_field` that names a
  variant field is a real `ClinVarVariant` model field (and `next_commands_field`
  is the known `_meta.next_commands` path).

## 5. Error taxonomy

No new codes. `invalid_input` simply becomes reachable for O1/O3/O4/O5 (bounds,
`id_type`, `sort`, blank query). `mcp/resources.error_codes` already lists all
three; messages must state *why* the call failed.

## 6. Testing

- TDD; each fix starts with a failing test. New code ~90% covered; global gate
  ≥ 70%. `make ci-local` green per task.
- Reuse the fixture DB (`tests/conftest.py`, `tests/_fixture_db.py`,
  `tests/fixtures/variant_summary_sample.txt`; BRCA1/TTN/MLH1/AP5Z1, 20 variants,
  VariationID 100001 = BRCA1 `c.5266dupC`). No new fixture rows are required:
  O2 uses `min_stars=5` (above the 0–4 range) to force an empty match on an
  existing gene; O4/O5 use existing genes.
- Layers: `test_repository.py` (sort SQL), `test_service.py` (bounds clamp,
  id_type/shape validation, empty-filter success, blank-query policy, sort
  validation, freshness helper), `test_tools_*.py` (Field → `invalid_input`
  envelopes, `_meta` freshness keys), `test_resources.py` (drift guard +
  `sort_options`).

## 7. Acceptance criteria

1. `limit ≤ 0`, `offset < 0`, unknown `sort`, unknown `id_type`, malformed
   identifier (auto), and blank `query` (no filter) all return `invalid_input`
   with truthful messages; none dump, miscategorize, or mislead.
2. `get_variants_by_gene` with an over-restrictive filter on an existing gene
   returns `success, results: [], total_count: 0`; a truly unknown gene returns
   `not_found`.
3. `sort=stars_asc` and `sort=name` are honored; `sort_options` is advertised in
   capabilities.
4. Every `_meta` with a known release date carries `age_days` and `past_ttl`;
   capabilities carries the same.
5. The drift-guard test fails if the cheatsheet and the model diverge.
6. `make ci-local` green; coverage ≥ 70%.
7. Deferred (documented, not built): tool `outputSchema`, full O6 fix,
   `submission_summary`.
