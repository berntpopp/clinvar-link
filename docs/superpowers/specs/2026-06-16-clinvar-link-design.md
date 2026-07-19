# clinvar-link — Design Spec

**Date:** 2026-06-16
**Status:** Approved (via `/goal` directive; design refined from the goal brief)
> Historical record — This design records implementation history; the live MCP registry is authoritative.

**Author:** Bernt Popp / fleet MCP engineering

## 1. Purpose

`clinvar-link` is a production MCP server that grounds variant-pathogenicity and
gene-interpretation questions in **NCBI ClinVar**. It serves answers from a fast
**local SQLite index** built from the ClinVar weekly bulk release — it does **not**
call NCBI eUtils at request time. It is a sibling of `gnomad-link`, `hgnc-link`, and
`genereviews-link` and copies the fleet stack and conventions exactly.

Research use only; not clinical decision support.

## 2. Fleet stack (copied exactly)

- Python 3.12+, **uv**, **FastMCP ≥3.2** + `mcp[cli]`, FastAPI unified HTTP (`/mcp`) +
  stdio (`mcp_server.py`), structlog, pydantic v2, **typer** CLI.
- Package `clinvar_link/`: `cli.py`, `config.py`, `server_manager.py`
  (`UnifiedServerManager`), `mcp/facade.py` (`create_clinvar_mcp`),
  `mcp/tools/*.py` (`register_*`), `mcp/errors.py` (`run_mcp_tool` envelope),
  `services/`, `models/`, `data/` (repository + schema), `ingest/` (data pipeline).
- ruff (line 100) + mypy + pytest (asyncio auto, ≥70 % coverage), pre-commit,
  Makefile (`ci-local`), GitHub Actions, multi-stage Dockerfile + compose,
  README/CLAUDE.md/AGENTS.md/.env.example.
- Three console scripts (hgnc-link pattern):
  - `clinvar-link = clinvar_link.cli:app` — typer `serve/health/version/config`.
  - `clinvar-link-mcp = mcp_server:main` — stdio transport entrypoint at repo root.
  - `clinvar-link-data = clinvar_link.ingest.cli:main` — typer `refresh/build/status`.

## 3. Data source & refresh

- **Primary source:** `https://ftp.ncbi.nlm.nih.gov/pub/clinvar/tab_delimited/variant_summary.txt.gz`
  (~414 MB gz / ~9 GB raw). Parsed **by header name** (the live file carries extra
  columns vs. older fixtures: `Oncogenicity`, `SCVsForAggregateGermlineClassification`,
  `SCVsForAggregateSomaticClinicalImpact`, `SCVsForAggregateOncogenicityClassification`).
- **Optional enrichment (v1: flag-gated, off by default):**
  `submission_summary.txt.gz` for per-submitter conflict detail.
  `clinvar.vcf.gz` is out of scope for v1.
  Rationale: `variant_summary` already provides `ReviewStatus` → star rating and
  "Conflicting classifications of pathogenicity" → conflicting, so v1 ships fully on
  `variant_summary` alone.
- **Refresh:** `clinvar-link-data refresh` does a conditional HTTP GET
  (ETag / Last-Modified) and a **7-day freshness TTL** skip; `build` forces a rebuild;
  `status` prints DB provenance. Download streams to disk; parse streams the gzip
  (never loads the whole file into memory). Build writes to a temp DB then atomically
  renames into place. Designed to be driven by cron / systemd timer.

## 4. Parsing & normalization (ported from kidney-genetics-db)

Port `backend/app/pipeline/sources/annotations/clinvar_utils.py` into
`clinvar_link/ingest/parsing.py` (pure-stdlib, no pandas):

- `map_classification(sig)` → `pathogenic | likely_pathogenic | vus | likely_benign |
  benign | conflicting | not_provided | other`.
- `format_accession(variation_id)` → `VCV{int:09d}`.
- `parse_protein_change`, `parse_protein_position`, `infer_effect_category`,
  `infer_molecular_consequences` (HGVS / protein parsing → SO consequence terms).
- `GeneAccumulator` → per-gene aggregate stats (counts by class, star/consequence
  distributions, top traits, `has_pathogenic`), feeding the `gene_summary` table.
- **Two-pass streaming assembly-priority dedup**: GRCh38 > GRCh37 > na, keyed by
  `VariationID`. Pass 1 builds a compact `{variation_id: winning_priority}` map;
  pass 2 emits only winning rows. Both-assembly coordinates are retained in
  `variant_coordinate`.

### Star rating (divergence from the ported confidence YAML — intentional)

Use the **official ClinVar gold-star convention**, stored in
`clinvar_link/data/review_status_stars.yaml` (configurable):

| ReviewStatus | Stars |
|---|---|
| practice guideline | 4 |
| reviewed by expert panel | 3 |
| criteria provided, multiple submitters, no conflicts | 2 |
| criteria provided, conflicting classifications | 1 |
| criteria provided, single submitter | 1 |
| no assertion criteria provided | 0 |
| no assertion provided / no classification provided / (default) | 0 |

(The kidney-genetics `review_confidence` YAML is an internal 0–4 *confidence* scale,
not ClinVar stars; we deliberately diverge to match ClinVar's documented system.)

## 5. SQLite schema (`clinvar_link/data/schema.sql`)

```
meta(schema_version, clinvar_release_date, source_url, source_etag,
     source_last_modified, source_sha256, variant_count, gene_count,
     build_utc, build_duration_s)            -- singleton

variant(
    variation_id INTEGER PRIMARY KEY,
    vcv_accession TEXT, allele_id INTEGER, rsid INTEGER,
    name TEXT, gene_symbol TEXT, gene_id TEXT, hgnc_id TEXT,
    variant_type TEXT,
    clinical_significance TEXT,               -- raw
    classification TEXT,                       -- normalized
    review_status TEXT, star_rating INTEGER,   -- 0..4
    protein_change TEXT, cdna_change TEXT,
    molecular_consequence TEXT,                -- JSON list
    traits TEXT,                               -- JSON list[{name,...}]
    rcv_accessions TEXT,                       -- JSON list
    number_submitters INTEGER, last_evaluated TEXT, origin TEXT,
    canonical_assembly TEXT, chromosome TEXT,  -- from winning assembly row
    cytogenetic TEXT
)
variant_coordinate(variation_id, assembly, chromosome_accession, chromosome,
                   start, stop, reference_allele, alternate_allele, position_vcf,
                   reference_allele_vcf, alternate_allele_vcf)   -- 1..2 rows/variant

rsid_lookup(rsid INTEGER, variation_id INTEGER)         -- idx rsid
allele_id_lookup(allele_id INTEGER, variation_id INTEGER) -- idx allele_id
hgvs_lookup(hgvs_norm TEXT, variation_id INTEGER)       -- idx hgvs_norm
gene_index(gene_symbol_upper TEXT, variation_id INTEGER)-- idx gene_symbol_upper
variant_fts                                              -- FTS5(name, gene_symbol, traits)
gene_summary(gene_symbol_upper TEXT PRIMARY KEY, gene_symbol TEXT, summary_json TEXT)
```

VCV lookups strip `VCV`/leading zeros → int → `variation_id`. WAL mode, read-only
connection at query time (`PRAGMA query_only`).

## 6. Service & repository layer

- `data/repository.py` — sync `ClinVarRepository` over a read-only sqlite3 connection
  (`check_same_thread=False`): `get_by_variation_id`, `get_by_rsid`, `get_by_allele_id`,
  `get_by_hgvs`, `get_by_vcv`, `search` (FTS5 + LIKE fallback), `variants_by_gene`,
  `gene_summary`, `meta`.
- `services/clinvar_service.py` — async `ClinVarService` wrapping the repository (via
  `asyncio.to_thread`), normalizing into pydantic models, attaching
  `recommended_citation` + release date, applying `response_mode` projection.
  `get_clinvar_meta()` feeds the capabilities tool's release date.

## 7. MCP tools (all: `READ_ONLY_OPEN_WORLD`, `response_mode` default `compact`,
typed error envelope returned not raised, `_meta.next_commands`, citation contract)

1. `get_variant(identifier, id_type?="auto", response_mode)` — resolve by
   VCV / VariationID / rsID (`rs123`) / HGVS / AlleleID. Returns one canonical record
   with classification, stars, both-assembly coords, traits, RCVs, `recommended_citation`.
2. `search_variants(query, gene_symbol?, classification?, min_stars?, assembly?,
   limit?, offset?, response_mode)` — FTS over name/gene/traits + filters; paginated list.
3. `get_gene_clinvar_summary(gene_symbol, response_mode)` — precomputed per-gene
   aggregates (counts by classification, star distribution, consequence categories,
   top traits, `has_pathogenic`).
4. `get_variants_by_gene(gene_symbol, classification?, min_stars?, sort?="stars_desc",
   limit?, offset?, response_mode)` — paginated variant list for a gene.
5. `get_server_capabilities()` — tools, datasets, response modes, live release date,
   limitations. Plus resources `clinvar://capabilities|usage|license|research-use`.

**Citation contract:** every variant result carries
`recommended_citation` (e.g. *"ClinVar (NCBI). VariationID 12345 (VCV000012345).
ClinVar weekly release YYYY-MM-DD. https://www.ncbi.nlm.nih.gov/clinvar/variation/12345/"*)
and `_meta` carries `clinvar_release` / `clinvar_release_date` + `unsafe_for_clinical_use: true`.

## 8. Testing (≥70 % coverage)

- Commit a **tiny `variant_summary` fixture** (~80–120 rows: multiple genes, both
  assemblies per variant, every classification + every review-status tier, an rsID,
  an indel, a conflicting record). The full bulk download is **never** run in tests/CI.
- `tests/conftest.py` session fixture builds a real SQLite from the fixture via the
  ingest builder (hgnc-link pattern), then exposes `repo`, `service`, `facade`.
- Unit tests: parser/normalization (`map_classification`, stars, dedup, HGVS),
  repository queries, each tool (success + error envelope + response modes + citation +
  next_commands), capabilities, resources.
- E2E smoke: build DB from fixture → start unified server → `GET /health` + one MCP
  tool call; stdio entrypoint imports and lists tools.

## 9. Packaging / ops

- Multi-stage Dockerfile + docker-compose (named volume for the SQLite index; entrypoint
  runs `clinvar-link-data refresh` then serves; configurable refresh interval).
- Makefile `ci-local` (ruff + mypy + pytest-cov), pre-commit, GitHub Actions
  (ci/docker/security), `.env.example`, README/CLAUDE.md/AGENTS.md.

## 10. Out of scope (v1 / YAGNI)

- `submission_summary.txt.gz` per-submitter detail (flag-gated, off) and `clinvar.vcf.gz`.
- Live eUtils calls. Embeddings / semantic search. Write operations.
```
