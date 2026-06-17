# clinvar-link: Harden Beyond 9/10 — Design Spec

**Date:** 2026-06-17
**Status:** Approved (design); pending implementation plan
**Branch:** `improve/clinvar-link-9plus`
**Author:** Bernt Popp (with Claude Code, expert-MCP-tester pass)

## 1. Background & motivation

A black-box MCP test pass (33 tool calls across the live `clinvar-link` server)
scored it ~7/10 and surfaced ~11 issues. A follow-up **white-box** pass against
the current source tree found that the **deployed server is a stale build**:
roughly half the reported issues are *already fixed in `main`* and are not bugs
in the source. This spec targets only what is **genuinely open in the current
source**, plus deliberate enhancements to push an LLM-facing quality score
beyond 9/10.

### 1.1 Already fixed in source (lock with regression tests, do not "re-fix")

| Reported (live server) | Source status | Evidence |
| --- | --- | --- |
| `output_cheatsheet` wrong field names | Fixed | `mcp/resources.py:72` uses `classification`/`star_rating`/`vcv_accession`, adds `raw_clinical_significance_field` |
| `search_variants` lacks `total`/`has_more` | Fixed | both list tools call `_pagination()` → `total_count`/`has_more`/`next_offset` (`services/clinvar_service.py:346`) |
| No batch tool | Exists | `get_variants` registered (`mcp/tools/variants.py:63`); 6-tool surface in `mcp/resources.py:_TOOLS` |
| Per-row citation bloat | Fixed | lean modes hoist `_meta.citation_template`, drop `cdna_change==name` (`services/clinvar_service.py:364`) |
| Bare `transcript:c.` HGVS fails | Likely fixed | `_get_by_hgvs_gene_insensitive()` LIKE-matches `nm_…(%):c…` (`data/repository.py:140`) |

**Out of scope / ops:** redeploying so `deployed == source`. The maintainer
owns deploy. This spec adds a *self-reported freshness signal* (Phase 2) so any
client can detect a stale deployment, but does not perform the deploy.

## 2. Goals & non-goals

**Goals**
- Eliminate the 6 confirmed open correctness/robustness defects.
- Make malformed input fail as `invalid_input` with **truthful** messages
  (never claim an identifier is "well-formed but absent" when it is not).
- Guarantee provenance from request #1 (no cold-start `clinvar_release:
  "unknown"`).
- Add protocol/observability polish (structured `outputSchema`, `request_id`
  echo, `latency_ms`, freshness signal, a capabilities↔payload drift guard).
- Lock already-fixed behaviors with regression tests.
- Spec the heavyweight data-layer enrichments but keep them off by default.

**Non-goals**
- No new transports, no write paths, no live eUtils calls (the local-index
  contract in `CLAUDE.md` stands).
- No clinical-decision features. Research-use-only safety contract unchanged.
- No redeploy/publish automation in this work.

## 3. Design constraints (from `CLAUDE.md` / `AGENTS.md`)

- Service layer returns plain dicts; the MCP layer (`mcp/errors.py`,
  `mcp/facade.py`) owns the `success`/`_meta` envelope and the error taxonomy.
  **Errors are returned, not raised** to the client (raised internally as typed
  exceptions, converted in `run_mcp_tool`).
- Error taxonomy is exactly `not_found | invalid_input | internal_error` and
  must stay in sync with `mcp/resources.get_capabilities_resource` `error_codes`.
- Every tool declares `annotations=READ_ONLY_OPEN_WORLD` and carries
  `_meta.next_commands`. Six-tool surface kept in lockstep across `mcp/tools/`,
  `mcp/facade.py`, `mcp/resources._TOOLS`.
- TDD; tests are network-free and build a fixture index from
  `tests/fixtures/variant_summary_sample.txt` (`tests/conftest.py`). Coverage
  gate ≥ 70% (target new code ~90%). `make ci-local` must pass.
- `response_mode ∈ minimal | compact | standard | full` (default `compact`);
  projection in `services/clinvar_service.py:_project`.

## 4. Phased plan (each phase independently shippable, `make ci-local` green)

### Phase 1 — Correctness & robustness

Confirmed-open defects, each fixed test-first.

**1.1 Bound pagination (`limit`/`offset`).**
- *Root cause:* `limit = min(limit, MAX_PAGE_SIZE)` (`clinvar_service.py:252`,
  `:315`) caps only the upper bound. `limit=-1` → SQLite `LIMIT -1` = unbounded;
  `limit=0` → empty. `repository.py` does no validation.
- *Design:* Validate at the MCP tool boundary with Pydantic `Field`
  constraints — `limit: int = Field(default=…, ge=1, le=MAX_PAGE_SIZE)`,
  `offset: int = Field(default=0, ge=0)`. A constraint violation surfaces as
  `invalid_input` (FastMCP validation → typed error) with a clear message.
  Service keeps `max(1, min(limit, MAX_PAGE_SIZE))` and `max(0, offset)` as a
  belt-and-braces clamp. Repository gains a defensive clamp + comment (never
  trust raw bounds). `MAX_PAGE_SIZE` stays 100 (`config.py:112`).
- *Tools affected:* `search_variants`, `get_variants_by_gene` (limit/offset);
  `get_variants` (`identifiers` length cap — add `max_length` so a giant batch
  can't blow context).

**1.2 Empty filtered set is success, not `not_found`.**
- *Root cause:* `get_variants_by_gene` raises `DataNotFoundError` when
  `total == 0` after filters (`clinvar_service.py:322`), inconsistent with
  `search_variants` (returns `[]`) and with out-of-range offset (succeeds).
- *Design:* Compute an **unfiltered** gene total first. If the gene has zero
  variants at all → `not_found` ("gene not in ClinVar index"). If the gene
  exists but filters exclude everything → `success` with `results: []`,
  `count: 0`, `total_count: 0`, and `_meta.next_commands` pointing back at
  `get_gene_clinvar_summary` / a relaxed filter. Message text must not imply the
  gene is absent when it isn't.

**1.3 Provenance from request #1 (no cold-start `"unknown"`).**
- *Root cause:* `clinvar_release`/date cache is primed only by the first
  `get_server_capabilities` call (`mcp/tools/metadata.py:84`); any other tool
  called first emits the `CLINVAR_DATA_RELEASE = "unknown"` sentinel
  (`mcp/resources.py:18`, `mcp/errors.py:85`).
- *Design:* Make `clinvar_date_cache.get_cached_clinvar_release_date()`
  **lazy-load from the DB `meta` table on first access** (any tool primes it),
  via the service/repository, with the in-process cache memoizing the result.
  Optionally eager-prime once at server startup in `facade.py`/`server_manager`.
  Sentinel `"unknown"` remains only for a genuinely missing meta row. Also
  populate a distinct `clinvar_release` *identifier* vs `clinvar_release_date`
  (today only the date is meaningful) — if the bulk file carries no separate
  release id, set `clinvar_release` to the date so it is never `"unknown"` on a
  built DB.

**1.4 Identifier shape validation (`get_variant`, `id_type="auto"`).**
- *Root cause:* `_resolve_auto` (`clinvar_service.py:148`) categorizes by regex
  but never rejects; unrecognized input falls through to `_maybe_allele_id`
  (`int(text)` → None) → `DataNotFoundError` with a message claiming the id is
  "well-formed but absent."
- *Design:* If `id_type="auto"` and the input matches **no** known shape
  (rsID `_RSID_RE`, VCV `_VCV_RE`, all-digits, or an HGVS hint `:`/`c.`/`p.`/…),
  raise `ToolInputError` → `invalid_input` with a truthful message
  ("unrecognized identifier shape; expected VCV / rsID / HGVS / AlleleID /
  VariationID, or use search_variants"). Recognized-shape-but-missing keeps
  `not_found`, and its message stays "well-formed but absent" (now accurate).
  For an explicit `id_type` that disagrees with the value (e.g. a VCV string
  with `id_type="rsid"`), return `invalid_input` ("value does not match
  id_type=rsid"). Also validate `id_type ∈ {auto, vcv, rsid, hgvs, allele_id,
  variation_id}`.

**1.5 `sort` allowlist + real implementation.**
- *Root cause:* `repository.variants_by_gene` only honors `sort == "stars_desc"`;
  anything else (incl. `stars_asc`) silently → `ORDER BY v.variation_id`
  (`repository.py:337`). No validation.
- *Design:* Define `SORT_KEYS = {stars_desc, stars_asc, name, variation_id}`
  with explicit, safe `ORDER BY` fragments (no string interpolation of user
  input). Implement ascending and name ordering. Unknown value → `invalid_input`
  listing valid options. Advertise the allowlist in `get_server_capabilities`.

**1.6 Empty-query policy (`search_variants`).**
- *Root cause:* empty `query` → no FTS tokens → `_search_like` with `%%` →
  match-all (`repository.py:233`); a test asserts the filtered variant of this.
- *Design:* Reject a **bare** empty/whitespace query with `invalid_input`
  ("query is required; to list by gene use get_variants_by_gene"). **Allow**
  empty query only when ≥1 filter (`gene_symbol` / `classification` /
  `min_stars`) is present, preserving the existing filter-list behavior and its
  test. The repository keeps the LIKE path for the filtered case.

**1.7 Regression tests for already-fixed behaviors.**
- Lock: bare-transcript HGVS resolves (`NM_033380.3:c.1871G>A` → VariationID
  24455 equivalent in fixture), `total_count`/`has_more`/`next_offset` present on
  both list tools, `get_variants` batch returns `requested`/`found_count`/
  `count`, lean-mode citation hoisting + `cdna_change==name` drop,
  `output_cheatsheet` field names match a real payload (see 2.3).

### Phase 2 — Protocol & observability polish

**2.1 Structured `outputSchema`.** Declare per-tool output schemas (FastMCP
structured output / `output_schema`) for all six tools so clients know shapes
ahead of time. Drive from the existing pydantic `models/` where possible.

**2.2 `request_id` + `latency_ms`.** Generalize the `request_id` param (already
on `get_variants`) to all tools; echo it in `_meta.request_id`. Stamp
`_meta.latency_ms` in `run_mcp_tool` (monotonic clock around the call). Accept a
client-supplied `request_id` (idempotency-friendly correlation).

**2.3 Capabilities↔payload drift guard (test).** A test that, for a real
projected payload, asserts every `output_cheatsheet.*_field` value is a key that
actually appears. This is the automated guard against the exact stale-cheatsheet
class of bug observed on the live server.

**2.4 Data-freshness signal.** Add to `get_server_capabilities` and `_meta`:
`clinvar_release_date`, `age_days` (now − release date), and `past_ttl`
(`age_days > CLINVAR_LINK_REFRESH_TTL_DAYS`). Lets any client detect a stale
deployment without owning the deploy. ("now" injected/cached at request time;
tests pass a fixed clock.)

### Phase 3 — Data-layer depth: sort & freshness (low ingest risk)

**3.1 Multi-key sort end-to-end.** Plumb the Phase-1.5 `SORT_KEYS` through
`search_variants` too (not just `get_variants_by_gene`), with a documented
default. Keep ORDER BY fragments table-driven and injection-safe.

**3.2 `assembly` filter semantics.** The `assembly` filter currently has fuzzy
behavior. Decide explicitly: either (a) document it as "restrict to variants
having coordinates on the named assembly" and implement that against the
coordinates rows, or (b) **drop it** (YAGNI) if no consumer needs it. Default
recommendation: drop unless a use case exists.

### Phase 4 — Heavy enrichments (gated, off by default, last)

Both roughly **double** the DB and touch ingest. Already config-gated; ship
**off by default** and reversible.

**4.1 `submission_summary` / conflict detail** behind
`CLINVAR_LINK_ENABLE_SUBMISSION_SUMMARY`: surface per-submitter classifications
in `full` mode plus a `conflicting` breakdown in `get_gene_clinvar_summary`.

**4.2 `hgvs4variation` multi-transcript** behind
`CLINVAR_LINK_ENABLE_HGVS4VARIATION`: `get_variant` resolves any
transcript-version HGVS and returns alternate HGVS expressions.

**YAGNI ruling:** Phase 4 is **specced but deferred.** Implement only when a
consumer concretely needs per-submitter or multi-transcript data; the DB-weight
cost is not justified speculatively. Phases 1–2 are the real "beyond 9/10" for
an LLM consumer; Phase 3 is a small add-on.

## 5. Error taxonomy impact

No new error codes — `invalid_input` simply becomes *reachable* for the cases it
should always have covered (bad bounds, bad `sort`, bad `id_type`, malformed
identifier, bare empty query). Keep `mcp/resources.error_codes` and the
capabilities doc in sync (they already list all three). All messages must be
**truthful** about why the call failed.

## 6. Testing strategy

- **TDD**: each fix starts with a failing test. New code targeted at ~90% line
  coverage; global gate stays ≥ 70%.
- **Fixture extension**: add rows to `tests/fixtures/variant_summary_sample.txt`
  to exercise: a gene with mixed star ratings (for sort + filter-empty), a
  bare-transcript HGVS case, an all-digits AlleleID vs VariationID collision, a
  4-star variant. Rebuild is automatic via `conftest.built_db`.
- **Per-layer**: `test_repository.py` (bounds clamp, sort SQL, empty-query LIKE),
  `test_service.py` (shape validation, empty-filter success, cold-start release,
  pagination), `test_tools_*.py` (Pydantic constraint → invalid_input, `_meta`
  request_id/latency/freshness, outputSchema presence), plus the
  capabilities↔payload drift guard.
- **No network**; `make ci-local` (ruff check, ruff format --check, mypy,
  pytest+coverage) green per phase.

## 7. Risks & mitigations

| Risk | Mitigation |
| --- | --- |
| Tightening empty-query / bounds breaks existing callers | Preserve filtered empty-query path + its test; bounds violations were already pathological. Document in capabilities. |
| Lazy DB read for release date adds a query to the hot path | Memoize after first read; it is one tiny `meta` lookup, cached process-wide. |
| `outputSchema` drift from payloads | The 2.3 drift-guard test fails CI if capabilities and payloads diverge. |
| Phase 4 DB-size blowup | Off by default, behind existing flags, deferred. |
| `latency_ms`/`age_days` non-determinism in tests | Inject a clock / fixed `now`; assert shape not exact value. |

## 8. Acceptance criteria ("beyond 9/10")

1. Negative/zero `limit`, bad `offset`, unknown `sort`, malformed identifier,
   bad `id_type`, and bare empty `query` all return `invalid_input` with
   truthful messages — none silently dump, miscategorize, or mislead.
2. `get_variants_by_gene` with an over-restrictive filter returns
   `success, results: [], total_count: 0` (not `not_found`); a truly unknown
   gene still returns `not_found`.
3. The **first** tool call of a fresh process emits the real
   `clinvar_release`/`clinvar_release_date` (never `"unknown"` on a built DB).
4. All six tools carry `outputSchema`, `_meta.request_id`, `_meta.latency_ms`,
   and a freshness signal; the drift-guard test passes.
5. Regression tests lock every already-fixed behavior.
6. `make ci-local` green; coverage ≥ 70% (new code ~90%).
7. Phase 4 specced, gated, and deferred (no default DB-size change).
