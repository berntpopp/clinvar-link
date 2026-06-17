# clinvar-link — "Beyond 9/10" Hardening v2 (Design Spec)

- **Date:** 2026-06-17
- **Status:** Drafted for review → planning
- **Project phase:** alpha — **breaking changes permitted**
- **Predecessor:** `improve/clinvar-link-9plus` (PR #1, merged) — this is the v2 pass

## 1. Context

`clinvar-link` is a local-data-backed MCP server: it answers from a read-only
SQLite index built from the NCBI ClinVar weekly bulk `variant_summary.txt.gz`.
It exposes six tools (`get_server_capabilities`, `get_variant`, `get_variants`
batch, `search_variants`, `get_gene_clinvar_summary`, `get_variants_by_gene`).

A full-surface MCP test on 2026-06-17 scored the server ~8.0/10. A
code-grounded review (Explore pass) confirmed the defects and **reframed the
headline finding**: there is no tool-registration drift in the code — the
running process was simply stale (started before the 6th tool was added and
never restarted), and nothing in the response envelope let a consumer detect
that. This spec defines the work to push the server past 9/10 across
**correctness, discoverability, observability, speed, and token-efficiency**.

### Locked decisions

1. **Scope — two-track.**
   - **Track A** = MCP / read-time changes. Ship now; **no index rebuild**.
   - **Track B** = ingest / index changes. Spec now; the maintainer folds them
     into the next weekly `clinvar-link-data build && publish`.
2. **Compatibility — breaking changes allowed (alpha).** Choose the cleanest
   correct solution; reflect breaks with a SemVer bump (`0.1.0 → 0.2.0`).

## 2. Findings (code-grounded)

| ID | Sev | Finding | Code site |
|----|-----|---------|-----------|
| F1 | High | Stale server served 5 tools while code/capabilities define 6; consumer cannot detect staleness (MCP "silent breakage"). Code is in lockstep — the gap is *detectability* + *structural drift-proofing*. | `mcp/resources.py:30` hardcoded `_TOOLS`; `mcp/errors.py` `_provenance_meta` carries no version |
| F2 | High | Free-text search forces **OR** across terms → `"COL4A5 Gly"` matches 295,797 rows; fights FTS5's implicit-AND default and makes `COUNT(*)` huge/slow (~0.5–1.1 s). | `data/repository.py:172` `_fts_query` (`" OR ".join`); `:253` `count_search` |
| F3 | Med | Gene-less HGVS (`NM_033380.3:c.1871G>A`) misses the equality index → LIKE-scan fallback ~440 ms vs <7 ms for VCV/rsID/VariationID/AlleleID. | `data/repository.py:128` `get_by_hgvs` / `_get_by_hgvs_gene_insensitive` |
| F4 | Med | Error `recovery` prose is generic (cites "VCV/rsID/HGVS/AlleleID" even for gene tools); blank input echoes the blank into `fallback_args`/`next_commands` (dead-end loop); forced `id_type` mismatch returns `not_found` instead of `invalid_input`. | `mcp/errors.py:167` `_recovery_text`; `:105` `_fallback_for`; `services/clinvar_service.py` id_type resolution |
| F5 | Med | Gene-summary significance buckets are an `if/elif` chain with **no catch-all** → risk-factor / drug-response / protective / association classes silently dropped; buckets don't reconcile to total (observed 3518 ≠ 3520). | `ingest/parsing.py:378` `GeneAccumulator.add_variant` |
| F6 | Low | `_meta` carries `clinvar_release` == `clinvar_release_date` (byte-identical); `full` mode emits null trait `omim_id/medgen_id/mondo_id` triples and `"na"` coordinate alleles (token waste). | `mcp/errors.py:73` `_provenance_meta`; `services` `_project` / `models` |

## 3. Success criteria (measurable)

- **SC1 (drift impossible):** capabilities `tools` is derived from the live
  FastMCP registry; a test asserts `set(capabilities.tools) == set(registered)`
  by equality (not `issubset`).
- **SC2 (staleness visible):** every `_meta` (success **and** error) and the
  capabilities doc carry `server_version` (SemVer). Data freshness is already
  conveyed by the existing `clinvar_release_date`; `server_version` is the new
  signal that makes a stale *code / tool surface* detectable. A
  `clinvar://version` resource returns `{server_version, mcp_protocol_version,
  clinvar_release_date}`.
- **SC3 (search precision):** `search_variants("COL4A5 Gly")` returns COL4A5
  Gly variants with `total_count` in the low hundreds and `match_mode:"and"`.
- **SC4 (recall preserved):** a query whose AND form has 0 hits returns results
  with `match_mode:"or_fallback"`.
- **SC5 (cheap count):** a broad query returns quickly with either an exact
  count (≤ threshold) or `total_count_capped:true`; `has_more` is always present
  without a full count.
- **SC6 (error UX):** gene-tool errors contain no "VCV/rsID/HGVS/AlleleID";
  blank input produces no blank in `fallback_args`/`next_commands`;
  `get_variant("VCV000024455", id_type="variation_id")` → `invalid_input`.
- **SC7 (reconciliation):** for every gene, `Σ(significance buckets) +
  other_count == total_count`.
- **SC8 (HGVS speed, Track B):** gene-less HGVS resolves via equality
  (target < 10 ms on the shipped bundle).
- **SC9:** `make ci-local` green; coverage ≥ 70 % (raise on touched modules).

## 4. Track A — MCP / read-time (no rebuild)

### A1. Drift-proof tool surface + visible version/staleness
- Derive the advertised tool list from the live FastMCP registry rather than
  the hardcoded `_TOOLS`; retain `_TOOLS` only as a frozen expectation in tests.
- Add `server_version` (package `__version__`, SemVer) to **capabilities** and
  to **every `_meta`** via `_provenance_meta`. (The existing `clinvar_release_date`
  already conveys *data* freshness; `server_version` is what makes a stale
  *code / tool surface* detectable — do not add a redundant date field.)
- Add a `clinvar://version` resource: `{server_version,
  mcp_protocol_version, clinvar_release_date}`.
- **Bump `server_version` 0.1.0 → 0.2.0** (breaking envelope changes below).
- **Files:** `mcp/resources.py`, `mcp/errors.py`, `mcp/facade.py`,
  `tests/test_tools_metadata.py`, `tests/test_e2e.py`.
- **Basis:** MCP SemVer + server-version resource; "silent breakage"
  anti-pattern.

### A2. Search precision + tiered count
- `_fts_query`: replace `" OR ".join(...)` with FTS5 implicit AND (space-join);
  keep last-token prefix `*` and `ORDER BY rank` (BM25, most-relevant-first).
- **Recall guard:** if the AND query yields 0 rows, auto-retry once with OR;
  echo `match_mode ∈ {and, or_fallback, or}`.
- **Override:** optional `match_mode` request param (default smart
  `and`→or-fallback; allow forcing `and` or `or`).
- **Tiered count (PostgREST pattern):** `count_mode ∈ {exact, estimated,
  none}`. Default = exact **up to a threshold** (e.g. `COUNT_EXACT_MAX = 1000`);
  beyond it return the capped value with `total_count_capped: true`. `has_more`
  always derived from a `limit+1` fetch (never needs a count).
- **Files:** `data/repository.py` (`_fts_query`, search, `count_search`),
  `services/clinvar_service.py`, `mcp/tools/variants.py` (params), tests.
- **Basis:** FTS5 implicit-AND default + BM25; PostgREST count tiers;
  `has_more` via `limit+1`.

### A3. Per-tool error UX
- `_recovery_text(error_code, tool_name, …)`: branch by tool family.
  - Variant tools: keep identifier guidance **+ a worked example**
    (`e.g. VCV000024455 | rs104886142 | NM_033380.3(COL4A5):c.1871G>A`).
  - Gene tools: gene-appropriate prose (`confirm the HGNC gene symbol, e.g.
    COL4A5; use search_variants to discover variants`).
- `_fallback_for`: treat whitespace-only query/identifier as **absent** — do
  not echo the blank; suggest an empty-slot call / capabilities. Removes the
  dead-end loop.
- Forced `id_type` shape validation in the service: when `id_type != auto` and
  the value does not match that type's shape → `invalid_input` (not
  `not_found`).
- **Files:** `mcp/errors.py`, `services/clinvar_service.py`, `mcp/tools/*`,
  tests.
- **Basis:** Anthropic "actionable errors with format examples."

### A4. Read-time reconciliation + envelope/token cleanup (breaking)
- Gene summary: add `other_count` derived at read time =
  `total_count − Σ(known buckets)`; guarantee `Σ buckets + other == total`.
  (Track B moves this to source; read-time stays as cross-check.)
- **Breaking cleanups:** remove the duplicate `clinvar_release` from `_meta`
  (keep single `clinvar_release_date`); in `full` mode omit null trait
  `omim_id/medgen_id/mondo_id` and `"na"` coordinate alleles.
- **Files:** `data/repository.py` or `services/clinvar_service.py` (gene
  summary), `models/`, `mcp/resources.py`, `services` `_project`, tests.

## 5. Track B — ingest / index (next rebuild)

### B1. Index the gene-stripped canonical HGVS key
- In builder pass-2 HGVS indexing, additionally insert the nucleotide
  expression with the `(GENE)` qualifier removed (e.g. `NM_033380.3:c.1871G>A`)
  into `hgvs_lookup`, **only when unambiguous within the build**.
- **Ambiguity rule:** if the gene-less key maps to > 1 `variation_id`, do not
  index it (leave to the LIKE fallback) and bump a build-time counter.
- Result: gene-less HGVS resolves by equality (< 10 ms); LIKE fallback becomes
  a rare last resort.
- **Files:** `ingest/builder.py`, `ingest/parsing.py` (key derivation),
  `data/repository.py` (relegate LIKE fallback), tests/fixtures.

### B2. Source `other` bucket in `GeneAccumulator`
- Add `other_count` to `GeneAccumulator` (final `else` of the classification
  chain), to `finalize()` output, and to the stored gene-summary schema;
  repository reads it. Read-time derivation (A4) remains the back-compat path
  for pre-B databases.
- **Graceful degradation:** repository falls back to read-time derivation when
  the column is absent (old bundle).
- **Files:** `ingest/parsing.py`, `ingest/builder.py` (schema),
  `data/repository.py`, `models/`, tests/fixtures.

## 6. Cross-cutting

- **Versioning:** SemVer; breaking → `0.2.0`; `clinvar://version` resource;
  never change `(name, version)` behavior silently.
- **Docs lockstep (project-mandated):** update `CLAUDE.md`, `AGENTS.md`,
  capabilities (`error_codes`, `sort_options`, new params `match_mode` /
  `count_mode`, `other_count`, `server_version`, `total_count_capped`), usage /
  output-cheatsheet, prompts. Keep the six-tool surface + field names in
  lockstep across `mcp/tools/`, `mcp/facade.py`, `mcp/resources`. (After A1,
  `resources` derives the advertised list from the live registry; `_TOOLS`
  survives only as the test oracle, so runtime drift is structurally
  impossible.)
- **TDD:** failing tests first; network-free; extend
  `tests/fixtures/variant_summary_sample.txt` with (a) a gene-less HGVS row,
  (b) an "other"-class classification (e.g. drug response / risk factor),
  (c) a multi-term search case exercising AND vs OR fallback.
- **Rollout:** Track A merges independently (no rebuild). Track B requires the
  maintainer to run `clinvar-link-data build && publish`; read-path guards
  tolerate the old schema.
- **Memory:** correct the standing rubric note (F1 root cause = stale server,
  not code drift) and log the v2 program.

## 7. Out of scope (YAGNI)

- `submission_summary` per-submitter detail (remains v1-off).
- `hgvs4variation` exhaustive multi-transcript indexing (separate opt-in flag
  already exists).
- Cursor-based pagination rewrite — tiered count + `has_more` is sufficient now.
- Trait → ontology (OMIM / MedGen / Mondo) enrichment.

## 8. Risks & mitigations

| Risk | Mitigation |
|------|-----------|
| AND default lowers recall on messy queries | OR auto-fallback + `match_mode` override; labelled in response |
| Removing `clinvar_release` / null fields breaks naive parsers | Acceptable (alpha); signalled by `0.2.0` bump + docs |
| Gene-less HGVS key ambiguous across genes | "don't index ambiguous" rule; LIKE fallback retained |
| `estimated` count misleads on small sets | Exact up to threshold (small sets always exact); cap only large sets |

## 9. Work breakdown (for planning)

1. A1 — drift-proof tools + version stamp + `clinvar://version` + strict test.
2. A2 — FTS implicit-AND + OR fallback + `match_mode` + tiered `count_mode`.
3. A3 — per-tool recovery prose + blank-input hygiene + forced-id_type validation.
4. A4 — read-time `other_count` + remove duplicate `_meta` field + full-mode null trim.
5. Docs/capabilities/cheatsheet lockstep + memory note.
6. B1 — gene-stripped HGVS key indexing (ingest) + fixture + relegate LIKE.
7. B2 — source `other` bucket (ingest) + schema + graceful read fallback.

Each item: tests-first, atomic commit, `make ci-local` green before the next.
