# Changelog

All notable changes to clinvar-link are documented here.

## [Unreleased]

### Changed

- Re-vendored the behaviour conformance gate from genefoundry-router `56db958`
  (`docs/conformance/behaviour.py` blob `c69801687`) and live-validated this
  backend against the current behaviour gate.

## [0.5.0] - 2026-07-14

Contract hardening (issue #26). **Breaking**: two wire fields change (`error_code` values, and
the gene summary's `total_count` → `variant_count`), and an unrecognized filter value now
ERRORS instead of returning an empty success. Behaviour Conformance v1 (hardened gate): **CONFORMANT**
(82 pass, 0 fail, 0 UNGATED).

### Fixed (code review of PR #27, Codex gpt-5.6-sol)

- **The batch tool `get_variants` had reintroduced the silent-empty, one tool over.** It caught
  every `ToolInputError` in the resolve loop and turned it into `found: false`, so a MALFORMED
  identifier (e.g. a 20-digit rsID that overflows int64) came back as `success: true,
  found_count: 0` — indistinguishable from a valid-but-absent record. A malformed element now
  fails the batch with `invalid_input` naming its position (`field: "identifiers.1"`); a
  well-formed-but-ABSENT identifier is still a truthful miss.
- **Actionable validation only fired at some call sites.** Every `raise ToolInputError(...)` in
  the package now supplies `field` + `public_reason`, so a real call
  (`get_variant(identifier="rs334", id_type="vcv")`, a blank identifier, an auto-detect failure,
  a forbidden-codepoint input) names the parameter instead of the bare "The request was rejected
  as invalid." A source-level partition test walks the AST of every module and fails if any raise
  site omits them — a hand-kept list of examples would be the same bug one level up.
- **Two error sites discarded `structuredContent`.** The output-validation wrapper and the
  unknown-tool backstop built a `CallToolResult` with `isError: true` but no `structuredContent`,
  losing the machine-readable envelope exactly where the contract requires it. Both now populate
  it (shared `_error_result`); the test that had ratified the null was corrected.
- **Gene inference now scans the whole query and handles lowercase symbols.** It stopped at the
  first 8 tokens (a gene at the end of a sentence was missed) and ignored lowercase letter-only
  symbols (`egfr`, `ttn`). Both are promoted now, with a conservative stopword guard so a common
  English word that is also a gene symbol (`set`, `rest`) does not hijack lowercase prose.

### Changed (same review)

- `get_server_capabilities` advertises the FULL six-value closed `error_code` enum, not the
  subset this server emits today, so a client learns the whole taxonomy; the contract test now
  asserts EXACT equality (a subset assertion had let it drift).
- The S4 contract test derives closed vocabularies from the registry (any param that declares an
  `enum` on one tool must declare it on every tool it appears on) instead of a hardcoded list.
- `search_variants` trimmed to 1,148 tokens (was 1,192) for real headroom under the 1,200 B1 cap.

### Fixed

- **The silently-empty filter — 559 pathogenic BRCA1 variants hidden behind a capitalization
  difference.** `get_variants_by_gene(gene_symbol="BRCA1", classification="likely_pathogenic")`
  returns 559 variants; ClinVar's OWN published wording, `"Likely pathogenic"`, returned
  `total_count: 0, success: true` and no error — as did `"BANANA"`. The vocabulary was
  undeclared, case-sensitive and underscore-joined, and the schema typed it as a bare string, so
  a model asking for pathogenic BRCA1 variants in ClinVar's canonical spelling was told,
  confidently, that there are none. Now: the vocabulary is DECLARED as an `enum`, ClinVar's own
  wording is accepted and normalized (`Likely pathogenic` / `Uncertain significance` /
  `Pathogenic/Likely pathogenic`, case-insensitively), and anything else is rejected with
  `invalid_input` naming the parameter and listing the accepted values. Same treatment for
  `assembly` (`hg19`/`hg38` normalize; unknown values error) and for every other closed
  vocabulary. Response-Envelope v1.1: *"silent omission is not compliant."*
- **`search_variants` answered a BRCA1 question with OCRL variants.** Its own documented usage —
  `query="BRCA1 pathogenic frameshift exon 11"` — matched nothing under AND, silently fell back
  to OR, and returned confidently-ranked variants of four unrelated genes (OCRL, CANT1, F8,
  BRCA2) with zero BRCA1 hits. A gene symbol written in the query is now promoted to the gene
  filter, so loose text can only narrow WITHIN the gene; and every degradation (OR fallback, or
  the text being dropped entirely) is declared in `_meta.search`
  (`gene_symbol_inferred` / `gene_symbol_applied` / `fallback` / `notice`) instead of being
  passed off as a ranked answer. An unknown `gene_symbol` filter is `not_found`, not an empty page.
- **Error envelopes carried protocol `isError: false`**, so a client branching on `isError` saw
  every failure — `not_found`, `invalid_input`, `internal` — as a SUCCESSFUL call and could hand
  the error envelope to the model as data. Every error now returns
  `ToolResult(structured_content=envelope, is_error=True)`: the flat envelope is unchanged and
  `structuredContent` is preserved (raising would have discarded it).
- **Validation errors misdirected the caller.** `limit=-5` answered *"The request was rejected as
  invalid"* plus recovery prose pointing at `gene_symbol` — the one argument that was already
  correct. Messages are now built from the tool's OWN advertised schema and name the offending
  parameter and its accepted values (`limit must be between 1 and 100`, `sort must be one of:
  …`), with a matching `field_errors` entry. The caller's rejected VALUE is still never echoed.
- **`response_mode="verbose"` was silently accepted** and served as if valid; `min_stars=5` (stars
  are 0-4) and `limit=100000` were silently clamped to bounds the schema never declared. All are
  now declared and enforced.
- A numeric identifier beyond int64 (`rs99999999999999999999`) overflowed SQLite and escaped as
  `internal_error`; it is now `invalid_input` naming `identifier`.
- The `response_mode` ladder was **non-monotonic**: `standard` returned a LARGER payload than
  `full` (444kB vs 405kB for the same rows) because only `full` dropped always-null trait ids and
  literal `"na"` alleles. `standard` now drops them too.

### Changed

- **BREAKING — `error_code` is the fleet's closed enum** (`invalid_input`, `not_found`,
  `ambiguous_query`, `upstream_unavailable`, `rate_limited`, `internal`). `internal_error` →
  `internal`; `response_too_large` and `output_validation_failed` are mapped onto the canon
  (`invalid_input` — the caller can lower `limit` / use a leaner `response_mode` — and `internal`
  respectively), each keeping its specific, actionable message. `error_codes` in
  `get_server_capabilities` and `clinvar://capabilities` is updated to match.
- **BREAKING — `get_gene_clinvar_summary` reports `variant_count`, not `total_count`.** Every
  list tool in the fleet uses `total_count` for the size of a PAGINATED result set, and this
  payload also carries a truncated `top_traits` list — one key meaning two things is how a client
  concludes it is reading page 1 of 15,947. The stored index still writes the old key and is read
  through an alias, so no data rebuild is needed.
- **BREAKING — an unrecognized filter value is now an error.** Callers that relied on
  `classification="Likely pathogenic"` returning an empty success will now receive
  `invalid_input`... which is the point: it returned the wrong answer before.
- Tool schemas are fully documented (TOOL-SCHEMA-DOCUMENTATION-STANDARD v1): 31/31 input
  properties carry a `description`, every required and array property carries `examples`, every
  closed vocabulary declares an `enum` (13 across the surface), and every bounded numeric declares
  `minimum`/`maximum`. Survey: **doc% 0 → 100, enums 0 → 13, examples 0 → 31**.
- No tool publishes an `outputSchema` (`output_schema=None`) and the server is constructed with
  `dereference_schemas=False` (TOOL-SURFACE-BUDGET-STANDARD v1). `structuredContent` is
  unaffected. Surface: **3,228t (46% outputSchema) → 3,863t (0% outputSchema)** — the growth is
  entirely parameter documentation, which is what the model actually reads.
- `get_variant` / `get_gene_clinvar_summary` keep their (capped) trait list in
  `response_mode="minimal"`: for a single-record tool the traits ARE the payload, and a mode that
  returns an identifier and nothing else is a silent-empty by another name. The list tools' rows
  stay lean.

### Added

- Behaviour Conformance v1 vendored into `tests/conformance/` (`behaviour.py`,
  `test_behaviour_v1.py`, byte-identical to the router's) and run against the container in the
  `mcp-conformance` workflow. It derives every probe from this server's own advertised schema, so
  a tool is gated the day it ships — and a tool it cannot probe is reported UNGATED, which fails.
- `_meta.search` on `search_variants`: what the search inferred and how it degraded.
- `filter_vocabularies` in `get_server_capabilities`: the exact accepted values for
  `classification`, `assembly` and `id_type`.

## [0.4.5] - 2026-07-14

### Changed

- **The NPM deployment pulls the released image instead of building from source.**
  `docker/docker-compose.npm.yml` carried `build:`, so a deploy rebuilt the image on the
  server even though CI had already published an attested, digest-addressable image to
  GHCR — the published image was never consumed. The overlay now requires
  `CLINVAR_LINK_IMAGE` pinned to a digest and fails closed when it is unset. Nothing
  else changed: `container_name`, the Compose project name, the `clinvar-data` named
  volume, the healthcheck (including its long 300s `start_period` for the first-boot
  bundle download) and the single-service bootstrap topology are all preserved.
  Research use only.

## [0.4.4] - 2026-07-13

### Fixed

- Release evidence now states the data contract this repository actually
  declares. The reusable release workflow hardcoded `data-independent` and a
  fixed `data_requirements: {"mode":"none"}`, so every published manifest
  claimed clinvar-link binds to no data at all, while `container-release.json`
  declares `data-bound` with an immutable pinned ClinVar bundle
  (`bundle-2026-07-07`) and its digest. Because `_require_data_binding` returns
  early for a data-independent contract, the binding assertion in the evidence
  chain was silently skipped as well.
- Re-pin both container workflows to the corrected container standard
  (`86b11f7e`), which sources the contract and the exact data identity from
  `container-release.json` and seals them into the capture artifact. This is an
  evidence-only re-release: the v0.4.3 image and attestations were sound.

## [0.4.3] - 2026-07-13

### Added

- Split data materialization out of the server: a one-shot `clinvar-data-init`
  sidecar downloads and verifies the pinned ClinVar bundle into the
  `clinvar-reference` named volume and exits; `clinvar-link` waits for
  `service_completed_successfully` and then mounts the same volume **read-only**.
- Declare the sidecar in `container-release.json` under `service.auxiliary` with
  its `init` role, `approved-networks` egress (the bundle is fetched from GitHub
  Releases), and its exact `writable_targets` (`/data`, `/tmp`), so the central
  fleet compose gate authorizes it **by role, never by name**.

### Fixed

- Hash the expanded bundle as a stream. `_expanded_tree_sha256` read the whole
  installed index into memory (`Path.read_bytes()`), so verifying the ~4.8 GB
  ClinVar SQLite made the data-init container exceed its memory limit and get
  OOM-killed (exit 137) before it could install the bundle. It is now hashed in
  bounded chunks, matching `mavedb-link`'s streaming implementation. The digest is
  unchanged.

### Changed

- Adopt the GeneFoundry container-release caller workflow and code-only
  production image release configuration bound to the published ClinVar
  `bundle-2026-07-07` external data artifact.
- Production now runs `clinvar-link-data pull` in the init sidecar rather than
  `bootstrap`, so advancing the pinned bundle installs exactly that release
  instead of reusing whatever index the volume already holds.
- Move every container mount onto the two writable targets the fleet compose
  policy approves: the reference volume is mounted at `/data` (was
  `/app/reference`) and scratch is a size-capped tmpfs at `/tmp` (was
  `/tmp/clinvar-link`, which the image's `TMPDIR` now also points at).
- Harden both services to the Container & Deployment Hardening Standard:
  digest-pinned untagged image, `read_only` rootfs, `cap_drop: [ALL]`,
  `no-new-privileges`, `deploy.resources.limits` (cpus/memory/pids) instead of
  the service-level `pids_limit`, bounded `json-file` logging, no published
  ports, no `container_name`, and the standard `GF_HEALTHCHECK_HOST` healthcheck.
- Inline the compose service definitions: top-level `x-*` anchors are emitted
  verbatim by `docker compose config` and are rejected as unapproved fields.
