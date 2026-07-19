# clinvar-link Implementation Plan

> Historical record — This plan records completed implementation work; the live MCP registry is authoritative.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. TDD throughout; commit per task.

**Goal:** Ship a production MCP server that grounds variant-pathogenicity / gene questions in NCBI ClinVar, served from a local SQLite index built from the ClinVar weekly bulk release.

**Architecture:** Sibling of `gnomad-link`/`hgnc-link`. A `clinvar-link-data` ingest CLI streams `variant_summary.txt.gz`, normalizes it (ported kidney-genetics parser), and builds a SQLite index (variant + coordinate + lookup + FTS + gene_summary + meta tables). An async `ClinVarService` over a sync `ClinVarRepository` feeds FastMCP tools exposed via unified FastAPI (`/mcp`) + stdio (`mcp_server.py`).

**Tech Stack:** Python 3.12, uv, FastMCP ≥3.2, mcp[cli], FastAPI, pydantic v2, typer, structlog, sqlite3 (stdlib), httpx, PyYAML. ruff (line 100) + mypy + pytest (asyncio auto, ≥70 % cov).

**Reference siblings (copy conventions verbatim, change names `gnomad`/`hgnc`→`clinvar`):**
- `/home/bernt-popp/development/hgnc-link` — **primary template** (SQLite, 3 console scripts, `mcp_server.py` stdio, ingest CLI, DB test fixtures, Dockerfile/compose/entrypoint, CI).
- `/home/bernt-popp/development/gnomad-link` — facade/errors-envelope/resources/annotations/capabilities/response_mode/next_commands conventions.
- `/home/bernt-popp/development/kidney-genetics-db/backend/app/pipeline/sources/annotations/clinvar_utils.py` — parser to port.

---

## File Structure

```
clinvar-link/
├── pyproject.toml                     # 3 console scripts; deps; ruff/mypy/pytest/coverage
├── Makefile  .pre-commit-config.yaml  .env.example  .env.docker.example
├── mcp_server.py                      # stdio entrypoint -> main()
├── .github/workflows/{ci,docker,security}.yml
├── docker/{Dockerfile,docker-compose.yml,entrypoint.sh,README.md}
├── clinvar_link/
│   ├── __init__.py                    # __version__ = "0.1.0"
│   ├── cli.py                         # typer: serve/health/version/config
│   ├── config.py                      # ServerConfig + Settings (DB path, refresh)
│   ├── logging_config.py  exceptions.py
│   ├── server_manager.py              # UnifiedServerManager (FastAPI /health + /mcp)
│   ├── data/
│   │   ├── schema.sql                 # SQLite DDL
│   │   ├── review_status_stars.yaml   # ReviewStatus -> 0..4
│   │   └── repository.py              # sync ClinVarRepository (read-only)
│   ├── ingest/
│   │   ├── __init__.py  cli.py        # typer: refresh/build/status
│   │   ├── parsing.py                 # ported clinvar_utils
│   │   ├── downloader.py              # conditional streaming download
│   │   └── builder.py                 # two-pass stream build -> sqlite
│   ├── models/
│   │   ├── __init__.py  enums.py  variant_models.py  gene_models.py
│   ├── services/
│   │   ├── __init__.py  clinvar_service.py  citation.py
│   └── mcp/
│       ├── __init__.py  facade.py  annotations.py  errors.py
│       ├── resources.py  clinvar_date_cache.py  prompts.py
│       └── tools/
│           ├── __init__.py            # register_clinvar_tools
│           ├── variants.py            # get_variant, search_variants
│           ├── genes.py               # get_gene_clinvar_summary, get_variants_by_gene
│           └── metadata.py            # get_server_capabilities + resources
├── tests/
│   ├── conftest.py
│   ├── fixtures/variant_summary_sample.txt
│   ├── test_parsing.py  test_builder.py  test_repository.py
│   ├── test_service.py  test_tools_variants.py  test_tools_genes.py
│   ├── test_tools_metadata.py  test_resources.py  test_e2e.py
└── docs/superpowers/{specs,plans}/...
```

---

## WAVE 1 — Foundation (sequential; everything else depends on it)

### Task 1: Scaffold + tooling

**Files:** Create `pyproject.toml`, `clinvar_link/__init__.py`, all package `__init__.py`,
`.gitignore` (exists), `.env.example`, `Makefile`, `.pre-commit-config.yaml`, `README.md` stub.

- [ ] **Step 1:** Copy `gnomad-link/pyproject.toml` → adapt: `name="clinvar-link"`,
  `[tool.hatch.version].path="clinvar_link/__init__.py"`, wheel `packages=["clinvar_link"]`,
  description "MCP server grounding variant-pathogenicity questions in NCBI ClinVar".
  Replace `[project.scripts]` with the **three hgnc-style scripts**:
  ```toml
  [project.scripts]
  clinvar-link = "clinvar_link.cli:app"
  clinvar-link-mcp = "mcp_server:main"
  clinvar-link-data = "clinvar_link.ingest.cli:main"
  ```
  Dependencies: drop `gql[aiohttp]`; keep fastapi, uvicorn[standard], pydantic,
  pydantic-settings, httpx, async-lru, structlog, orjson, rich, typer, mcp[cli], fastmcp,
  gunicorn, asgi-correlation-id, prometheus-client; **add** `pyyaml>=6.0,<7.0`.
  Keep `[dependency-groups].dev` (pytest, pytest-asyncio, pytest-cov, pytest-mock,
  pytest-xdist, respx, ruff, mypy, pre-commit) + add `types-PyYAML`.
  Keep ruff/mypy/pytest/coverage blocks verbatim (line-length 100, asyncio_mode auto,
  fail_under 70). In `[[tool.mypy.overrides]]` modules list drop gql/graphql, add `yaml.*`.
- [ ] **Step 2:** `clinvar_link/__init__.py` → `__version__ = "0.1.0"` + module docstring.
  Create empty `__init__.py` for `clinvar_link/{data,ingest,models,services,mcp,mcp/tools}`.
- [ ] **Step 3:** Copy `gnomad-link/Makefile` + `.pre-commit-config.yaml` + `.env.example`;
  replace `gnomad`→`clinvar`, `GNOMAD`→`CLINVAR`. Ensure Makefile `ci-local` runs
  `ruff check . && ruff format --check . && mypy clinvar_link && pytest --cov`.
- [ ] **Step 4:** `uv sync --all-extras` (or `uv sync`). Expected: venv resolves, no errors.
- [ ] **Step 5:** `uv run python -c "import clinvar_link; print(clinvar_link.__version__)"`
  → prints `0.1.0`.
- [ ] **Step 6:** Commit `chore: scaffold clinvar-link package and tooling`.

### Task 2: config, logging, exceptions

**Files:** Create `clinvar_link/config.py`, `logging_config.py`, `exceptions.py`.

- [ ] **Step 1:** Copy `gnomad-link/gnomad_link/logging_config.py` verbatim (rename logger
  namespace to `clinvar_link`). Copy `exceptions.py`; rename base error to
  `ClinVarServerError`, keep `ConfigurationError`, `MCPIntegrationError`, `StartupError`.
  Add data-layer errors used by the envelope: `DataNotFoundError`, `ClinVarDataError`,
  `ToolInputError(ValueError)` (or place these in `data/` / `mcp/errors.py` per gnomad).
- [ ] **Step 2:** `config.py` — copy gnomad's `ServerConfig` dataclass + `Settings`
  (pydantic-settings, env prefix `CLINVAR_LINK_`). **Replace API fields** with data fields:
  `DATA_DIR: Path` (default `<repo>/data`), `DB_FILENAME: str = "clinvar.sqlite"`,
  computed `db_path`, `SOURCE_URL` (variant_summary.txt.gz), `REFRESH_TTL_DAYS: int = 7`,
  `AUTO_BOOTSTRAP: bool = False`, `ENABLE_SUBMISSION_SUMMARY: bool = False`,
  `CACHE_SIZE`, `CACHE_TTL_MINUTES`, `LOG_FORMAT`, `CORS_ORIGINS`, `cors_origins_list`.
  Keep `ServerConfig.from_env()`.
- [ ] **Step 3:** Test `tests/test_config.py`: `Settings()` loads defaults; `db_path`
  joins DATA_DIR/DB_FILENAME; env override works (monkeypatch `CLINVAR_LINK_DB_FILENAME`).
  Run `pytest tests/test_config.py -v` → PASS.
- [ ] **Step 4:** Commit `feat: config, logging, exceptions`.

### Task 3: SQLite schema

**Files:** Create `clinvar_link/data/schema.sql`.

- [ ] **Step 1:** Author DDL per spec §5 (tables: `meta`, `variant`, `variant_coordinate`,
  `rsid_lookup`, `allele_id_lookup`, `hgvs_lookup`, `gene_index`, `variant_fts` (FTS5
  `content=''` external-content over name/gene_symbol/traits with `variation_id` as the
  rowid via `content_rowid`), `gene_summary`). Start with
  `PRAGMA journal_mode=WAL; PRAGMA foreign_keys=OFF;`. Add indices:
  `idx_variant_gene` on `variant(gene_symbol)`, `idx_variant_class` on
  `variant(classification)`, `idx_variant_stars` on `variant(star_rating)`,
  `idx_coord_assembly` on `variant_coordinate(assembly, chromosome, start)`,
  `idx_rsid`, `idx_allele_id`, `idx_hgvs_norm`, `idx_gene_index`.
- [ ] **Step 2:** Add `schema.sql` to wheel data via hatch (`[tool.hatch.build.targets.wheel].include`
  or `force-include`) — mirror how hgnc-link ships its `schema.sql` (check
  `hgnc-link/pyproject.toml` + how it loads via `importlib.resources`).
- [ ] **Step 3:** Test `tests/test_schema.py`: open in-memory sqlite, `executescript`
  the schema file, assert `sqlite_master` contains every expected table + the FTS table.
  Run → PASS.
- [ ] **Step 4:** Commit `feat: sqlite schema`.

### Task 4: Port the ClinVar parser (pure, TDD)

**Files:** Create `clinvar_link/ingest/parsing.py`, `clinvar_link/data/review_status_stars.yaml`,
`tests/test_parsing.py`.

Port from `kidney-genetics-db/.../clinvar_utils.py` (pure stdlib + PyYAML for the star map).
Functions: `parse_protein_change`, `parse_protein_position`, `infer_effect_category`,
`infer_molecular_consequences`, `map_classification`, `format_accession`,
`load_star_map()` (reads `review_status_stars.yaml`), `map_star_rating(review_status, star_map)`,
`parse_variant_row(row: dict[str,str], star_map) -> dict`, and class `GeneAccumulator`.

- [ ] **Step 1 (RED):** Write `tests/test_parsing.py` covering, with exact assertions:
  - `map_classification("Pathogenic/Likely pathogenic")=="likely_pathogenic"`,
    `"Conflicting classifications of pathogenicity"→"conflicting"`,
    `"Benign"→"benign"`, `"Likely benign"→"likely_benign"`,
    `"Uncertain significance"→"vus"`, `""→"not_provided"`, `"Pathogenic"→"pathogenic"`,
    `"Pathogenic; Benign"→"conflicting"`.
  - `format_accession("12345")=="VCV000012345"`.
  - `map_star_rating("reviewed by expert panel", star_map)==3`,
    `"practice guideline"→4`,
    `"criteria provided, multiple submitters, no conflicts"→2`,
    `"criteria provided, single submitter"→1`,
    `"no assertion criteria provided"→0`, unknown→0.
  - `parse_protein_change("NM_x:c.80C>T (p.Arg27Trp)")=="p.Arg27Trp"`;
    `parse_protein_position("p.Arg27Trp")==27`.
  - `infer_effect_category` for a frameshift→`"truncating"`, missense→`"missense"`,
    `c.80+1G>A`→`"splice_region"`.
  - `parse_variant_row` on a real header+row dict (use the AP5Z1 row from
    `variant_summary` header) returns dict with `variation_id`, `accession="VCV..."`,
    `classification`, `star_rating`, `protein_change`, `traits` list.
  - `GeneAccumulator`: add 3 variants (1 pathogenic 3-star, 1 vus, 1 benign),
    `finalize()` returns `total_count==3`, `pathogenic_count==1`, `has_pathogenic==True`,
    star distribution present.
  Run `pytest tests/test_parsing.py -v` → FAIL (module missing).
- [ ] **Step 2 (GREEN):** Create `review_status_stars.yaml` per spec §4 table.
  Port the functions/class from `clinvar_utils.py` verbatim (read it in full first), with
  these adaptations: replace `map_review_confidence` with `map_star_rating` driven by the
  YAML; add `load_star_map()` via `importlib.resources.files("clinvar_link.data")`;
  parse `RS# (dbSNP)` and `AlleleID`/`#AlleleID` and `VariationID` columns;
  ensure column access is by header name. Run → PASS.
- [ ] **Step 3:** Commit `feat: port clinvar parser + star map (TDD)`.

---

## WAVE 2 — Data plane (parallel after Wave 1)

### Task 5: Ingest builder + downloader + data CLI + fixture

**Files:** Create `clinvar_link/ingest/downloader.py`, `builder.py`, `cli.py`,
`tests/fixtures/variant_summary_sample.txt`, `tests/test_builder.py`.
**Depends on:** Tasks 3, 4.

- [ ] **Step 1:** Author `tests/fixtures/variant_summary_sample.txt` — the **real 43-column
  header** (from the live file) + ~80 data rows hand-built to cover: ≥4 distinct genes
  (e.g. BRCA1, TTN, AP5Z1, MLH1); each variant present on **both GRCh38 and GRCh37** rows
  (same VariationID) to exercise dedup; one row per classification
  (Pathogenic, Likely pathogenic, Uncertain significance, Likely benign, Benign,
  Conflicting classifications of pathogenicity); one per review-status tier (0–4 stars);
  ≥1 SNV with a real `RS# (dbSNP)`; ≥1 indel; valid `Start/Stop/VariationID/AlleleID`.
- [ ] **Step 2 (RED):** `tests/test_builder.py`:
  - `build_database(config, source_path=FIXTURE, etag="x", last_modified="...")` creates
    `config.db_path`; open it read-only and assert: `variant` row count == number of
    **unique** VariationIDs (dedup worked, GRCh38 kept); a known VariationID has
    `canonical_assembly=="GRCh38"`; `variant_coordinate` has 2 rows for a both-assembly
    variant; `rsid_lookup` resolves a known rs#; `gene_index` has the gene; `gene_summary`
    has a row per gene with valid JSON; `meta` has `clinvar_release_date`, `variant_count`,
    `gene_count`, `source_etag`.
  Run → FAIL.
- [ ] **Step 3 (GREEN):** `builder.py`:
  - `build_database(config, *, source_path, etag, last_modified, release_date=None)`:
    create temp sqlite in DATA_DIR, `executescript(schema.sql)`.
    **Pass 1:** stream `gzip`/plain `csv.DictReader` (tab) → `{variation_id: max_priority}`
    using `_ASSEMBLY_PRIORITY={"GRCh38":3,"GRCh37":2,"na":1}`.
    **Pass 2:** stream again; for each row, if `priority(row.assembly)==winning[vid]` and
    not yet emitted → `parse_variant_row` → insert `variant` + lookups + FTS + gene_index,
    feed `GeneAccumulator[gene]`; always insert every row's coords into
    `variant_coordinate`. Batch inserts (`executemany`, ~2000). After pass 2, write
    `gene_summary` from accumulators, then `meta` (derive `clinvar_release_date` from
    `last_modified` or arg; `build_utc`, `build_duration_s`, counts, sha256 if available).
    Commit; atomically `os.replace(temp, db_path)`.
  - Helpers: `_open_source(path)` yields text lines handling `.gz`; `_priority(assembly)`.
    Run → PASS.
- [ ] **Step 4:** `downloader.py` — copy hgnc-link `downloader.py` shape: conditional GET
  with `If-None-Match`/`If-Modified-Since` via httpx, stream to disk in chunks, persist
  `download_cache.json` (etag, last_modified) beside the DB, return
  `(status: "ok"|"not_modified", path, etag, last_modified)`. Add unit test with `respx`
  mocking 200 then 304.
- [ ] **Step 5:** `ingest/cli.py` — typer app `main`:
  - `build` (force download+rebuild), `refresh` (skip if DB meta younger than
    `REFRESH_TTL_DAYS` AND remote 304; else rebuild), `status` (print meta as rich table).
    Wire via `build_database` + `downloader`. Reuse a cross-process lock (copy
    hgnc-link `ingest/lock.py`) so concurrent refresh is safe.
- [ ] **Step 6:** Commit `feat: ingest pipeline (download, build, data CLI) + fixture`.

### Task 6: Repository (sync SQLite queries)

**Files:** Create `clinvar_link/data/repository.py`, `tests/test_repository.py`.
**Depends on:** Tasks 3, 5 (uses the fixture-built DB from conftest).

- [ ] **Step 1 (RED):** `tests/test_repository.py` against the built fixture DB
  (session fixture — see Task 11/conftest, or build inline here). Assert:
  `get_by_variation_id(vid)` returns dict with `vcv_accession`, `classification`,
  `star_rating`, `coordinates` (list with both assemblies); `get_by_vcv("VCV000012345")`
  resolves; `get_by_rsid(397704705)` resolves; `get_by_allele_id(...)`; `get_by_hgvs(...)`;
  `search("BRCA1", limit=10)` returns hits; `variants_by_gene("BRCA1", min_stars=2)`
  filters; `gene_summary("BRCA1")` returns parsed JSON; `meta()` returns release date.
  Run → FAIL.
- [ ] **Step 2 (GREEN):** `ClinVarRepository(db_path)` opens
  `sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)`,
  `row_factory=sqlite3.Row`, `PRAGMA query_only=ON`. Implement methods above; `search`
  uses FTS5 `MATCH` with a sanitized query (copy hgnc-link `_fts_query` + LIKE fallback);
  `variants_by_gene` queries `gene_index`→`variant` with `min_stars`/`classification`
  filters and `ORDER BY star_rating DESC`; coordinates loaded via a join on
  `variant_coordinate`. Run → PASS.
- [ ] **Step 3:** Commit `feat: ClinVarRepository`.

### Task 7: Pydantic models

**Files:** Create `clinvar_link/models/{enums.py,variant_models.py,gene_models.py,__init__.py}`.
**Depends on:** Task 1 only (parallelizable).

- [ ] **Step 1:** `enums.py` — `Classification(str, Enum)` (pathogenic, likely_pathogenic,
  vus, likely_benign, benign, conflicting, not_provided, other), `Assembly(GRCh38, GRCh37)`,
  `ResponseMode(minimal, compact, standard, full)`, `IdType(auto, vcv, variation_id, rsid,
  hgvs, allele_id)`.
- [ ] **Step 2:** `variant_models.py` — `Coordinate`, `Trait`, `ClinVarVariant`
  (pydantic v2 `BaseModel`, `ConfigDict(extra="forbid")`, modern typing). `gene_models.py`
  — `GeneClinVarSummary` (counts, star_distribution, consequence_categories, top_traits,
  has_pathogenic). Add `model_config` examples.
- [ ] **Step 3:** Test `tests/test_models.py`: construct from a dict, round-trip
  `model_dump()`. Run → PASS.
- [ ] **Step 4:** Commit `feat: pydantic models`.

---

## WAVE 3 — Service + MCP plumbing (parallel after Wave 2)

### Task 8: ClinVarService + citation

**Files:** Create `clinvar_link/services/clinvar_service.py`, `citation.py`, `__init__.py`,
`tests/test_service.py`. **Depends on:** Tasks 6, 7.

- [ ] **Step 1:** `citation.py` — `recommended_citation(variation_id, release_date)` →
  `"ClinVar (NCBI). VariationID {vid} ({vcv}). ClinVar weekly release {date}. https://www.ncbi.nlm.nih.gov/clinvar/variation/{vid}/"`.
- [ ] **Step 2 (RED):** `tests/test_service.py`: `ClinVarService(repo)` async methods:
  `get_variant("VCV000012345")` returns dict with `recommended_citation` + `classification`
  + `coordinates`; `search_variants("BRCA1")`; `gene_summary("BRCA1")`;
  `variants_by_gene("BRCA1", min_stars=2)`; `get_clinvar_meta()` returns `{release_date:...}`;
  `response_mode` projection: `minimal` returns only core ids+classification, `full` returns
  everything. Run → FAIL.
- [ ] **Step 3 (GREEN):** Implement `ClinVarService` wrapping repo calls in
  `asyncio.to_thread`, mapping rows → models → dicts, attaching citation + applying a
  `_project(payload, mode)` helper. `DataNotFoundError` raised when missing (envelope
  converts it). Run → PASS.
- [ ] **Step 4:** Commit `feat: ClinVarService + citation contract`.

### Task 9: MCP envelope, annotations, resources, date cache, prompts

**Files:** Create `clinvar_link/mcp/{errors.py,annotations.py,resources.py,clinvar_date_cache.py,prompts.py}`,
`tests/test_resources.py`. **Depends on:** Tasks 2, 8.

- [ ] **Step 1:** Copy `gnomad-link/gnomad_link/mcp/annotations.py` verbatim
  (`READ_ONLY_OPEN_WORLD`). Copy `clinvar_date_cache.py` (process-lifetime cache of the
  release date — already ClinVar-named in gnomad).
- [ ] **Step 2:** `resources.py` — copy gnomad's; set `RESEARCH_USE_NOTICE`,
  `get_capabilities_resource()` (tools list = the 5 tools, response modes, resources,
  `clinvar_release_date` from cache), `get_usage_resource()` (compact usage notes),
  `get_license_resource()` (ClinVar is public domain / NCBI attribution), and the
  research-use payload. Tools list must match the registered tools exactly.
- [ ] **Step 3:** `errors.py` — copy gnomad's `run_mcp_tool` + `McpErrorContext` +
  `McpToolError` + `mcp_tool_error` + `record_mcp_error` + `_provenance_meta`
  (`unsafe_for_clinical_use:true`, `clinvar_release`/`clinvar_release_date`). Map our
  exceptions: `DataNotFoundError`→`not_found`, `ToolInputError`→`invalid_input`,
  `ClinVarDataError`→`internal_error`. `install_validation_error_handler` +
  `install_output_validation_error_handler` (copy from gnomad; if gnomad has
  `output_validation.py`, copy it too).
- [ ] **Step 4:** `prompts.py` — copy gnomad's `register_workflow_prompts`, rewrite the
  prompt text for ClinVar workflows (resolve variant → classification; gene → summary).
- [ ] **Step 5:** `tests/test_resources.py`: capabilities dict has the 5 tool names +
  `research_use_only:true`; `run_mcp_tool` returns `{success:False, error_code:"not_found"}`
  envelope (never raises) when the inner coro raises `DataNotFoundError`, and injects
  `_meta.clinvar_release`. Run → PASS.
- [ ] **Step 6:** Commit `feat: MCP envelope, resources, annotations, prompts`.

---

## WAVE 4 — Tools + transports (after Wave 3)

### Task 10: MCP tools + facade

**Files:** Create `clinvar_link/mcp/tools/{variants.py,genes.py,metadata.py,__init__.py}`,
`clinvar_link/mcp/facade.py`, `tests/test_tools_variants.py`, `test_tools_genes.py`,
`test_tools_metadata.py`. **Depends on:** Tasks 8, 9.

Pattern for every tool (copy gnomad tool file shape): `@mcp.tool(name=..., title=...,
annotations=READ_ONLY_OPEN_WORLD, tags={...})`, async body returns
`await run_mcp_tool(name, lambda: _coro(...), context=McpErrorContext(...))`. Each result
dict carries `_meta.next_commands` (list of `{tool, arguments}`) + `recommended_citation`.

- [ ] **Step 1 (RED):** Write tool tests against a `facade`/`service` built on the fixture DB:
  - `get_variant`: by VCV, by `rs<num>`, by HGVS, by AlleleID, by VariationID → success
    envelope with `classification`, `star_rating`, `coordinates`, `recommended_citation`,
    `_meta.next_commands` containing `get_gene_clinvar_summary`. Unknown id → `not_found`
    envelope (no raise). `response_mode="minimal"` trims payload.
  - `search_variants("BRCA1")` → list + pagination meta + next_commands.
  - `get_gene_clinvar_summary("BRCA1")` → counts + has_pathogenic + citation.
  - `get_variants_by_gene("BRCA1", min_stars=2)` → filtered list sorted by stars.
  - `get_server_capabilities()` → 5 tools + release date.
  Run → FAIL.
- [ ] **Step 2 (GREEN):** `variants.py` `register_variant_tools(mcp, service_factory)`
  (`get_variant`, `search_variants`); `genes.py` `register_gene_tools`
  (`get_gene_clinvar_summary`, `get_variants_by_gene`); `metadata.py`
  `register_metadata_tools` (`get_server_capabilities` + the `clinvar://*` resources —
  copy gnomad's metadata.py). `tools/__init__.py` `register_clinvar_tools(mcp,*,service_factory)`
  calls all three. `id_type="auto"` detection: `rs\d+`→rsid, `VCV…`→vcv, `\d+`→variation_id,
  contains `:`/`c.`/`p.`→hgvs, else try allele_id. Run → PASS.
- [ ] **Step 3:** `facade.py` — copy gnomad's `create_clinvar_mcp(*, service_factory)`:
  `FastMCP(name="clinvar-link", instructions=_INSTRUCTIONS, mask_error_details=True)`,
  call `register_clinvar_tools`, `register_workflow_prompts`, install both error handlers.
  Rewrite `_INSTRUCTIONS` for ClinVar.
- [ ] **Step 4:** Commit `feat: MCP tools + facade`.

### Task 11: Transports — server_manager, cli, mcp_server (stdio), conftest

**Files:** Create `clinvar_link/server_manager.py`, `clinvar_link/cli.py`, `mcp_server.py`,
`tests/conftest.py`. **Depends on:** Task 10.

- [ ] **Step 1:** `tests/conftest.py` — session fixture `built_db(tmp_path_factory)` runs
  `build_database` from the committed fixture; `data_config`, `repo`, `service`,
  `facade` fixtures (hgnc-link conftest pattern). All tests needing data use these.
- [ ] **Step 2:** `server_manager.py` — copy gnomad's `UnifiedServerManager`; replace the
  service factory with one that builds `ClinVarService(ClinVarRepository(settings.db_path))`;
  FastAPI `/health` returns `{status, transport, clinvar_release_date}`; mount MCP at `/mcp`;
  keep lifespan composition + signal handlers. If `AUTO_BOOTSTRAP` and DB missing, log a
  clear error pointing to `clinvar-link-data build`.
- [ ] **Step 3:** `cli.py` — copy gnomad's typer app: `serve` (unified), `config`,
  `health`, plus a `version` command printing `__version__`. Remove cache subcommands if
  not applicable (or keep a no-op `cache stats`). Adapt `config` table to data settings.
- [ ] **Step 4:** `mcp_server.py` (repo root) — copy hgnc-link `mcp_server.py`: builds
  `ClinVarService` directly, `create_clinvar_mcp(service_factory=...)`, `def main(): mcp.run()`
  (stdio). Guard `if __name__ == "__main__": main()`.
- [ ] **Step 5:** Commit `feat: unified server, cli, stdio entrypoint`.

---

## WAVE 5 — Integration, ops, docs (parallel after Wave 4)

### Task 12: E2E smoke tests

**Files:** Create `tests/test_e2e.py`. **Depends on:** Task 11.

- [ ] **Step 1:** Test: build the FastAPI app via `UnifiedServerManager` pointed at the
  fixture DB; use `httpx.ASGITransport`/`TestClient` to `GET /health` → 200 + release date.
- [ ] **Step 2:** Test: import `mcp_server`, build the facade, `await mcp.list_tools()`
  (or FastMCP equivalent) returns the 5 tool names. Test: an in-process MCP tool call to
  `get_variant` returns a success envelope.
- [ ] **Step 3:** Run full suite `uv run pytest --cov=clinvar_link` → ≥70 %. Commit
  `test: e2e smoke (http + stdio)`.

### Task 13: Docker + CI + pre-commit

**Files:** Create `docker/{Dockerfile,docker-compose.yml,entrypoint.sh,README.md}`,
`.github/workflows/{ci.yml,docker.yml,security.yml}`, `.env.docker.example`.
**Depends on:** Task 11 (parallel with 12/14).

- [ ] **Step 1:** Copy hgnc-link `docker/Dockerfile` (multi-stage uv builder→runtime) +
  `entrypoint.sh` (run `clinvar-link-data refresh` if enabled, then
  `clinvar-link serve --host 0.0.0.0 --port 8000`) + `docker-compose.yml` (named volume
  `clinvar-data` → `/app/data`, env `CLINVAR_LINK_DATA_DIR=/app/data`, healthcheck on
  `/health`). Adjust package/script names.
- [ ] **Step 2:** Copy hgnc-link `.github/workflows/{ci,docker,security}.yml`; adjust
  names; CI runs `make ci-local` on 3.12/3.13. Ensure CI does **not** download bulk data
  (tests use the fixture).
- [ ] **Step 3:** `docker build -f docker/Dockerfile -t clinvar-link:dev .` → succeeds.
  Commit `chore: docker, compose, CI, security workflows`.

### Task 14: Docs

**Files:** Create/expand `README.md`, `CLAUDE.md`, `AGENTS.md`. **Depends on:** Task 11.

- [ ] **Step 1:** `README.md` — Quick Start (`uv sync`, `clinvar-link-data build`,
  `clinvar-link serve`), MCP client config (stdio `clinvar-link-mcp` + HTTP `/mcp`),
  tool reference (the 5 tools w/ examples), data refresh + cron/systemd, citation +
  research-use notice, env vars table.
- [ ] **Step 2:** `CLAUDE.md` + `AGENTS.md` — mirror hgnc-link: architecture, conventions,
  how to run tests/ci-local, data pipeline notes, "research use only".
- [ ] **Step 3:** Commit `docs: README, CLAUDE.md, AGENTS.md`.

---

## WAVE 6 — Verification (verification-before-completion)

### Task 15: Full verification + smoke on real data shape

- [ ] **Step 1:** `make ci-local` → ruff clean, ruff format clean, mypy clean,
  pytest ≥70 % cov. Paste real output.
- [ ] **Step 2:** **Real-data shape smoke (bounded):** stream the first ~20 000 lines of
  the live `variant_summary.txt.gz` to a temp file, run `build_database` on it, then
  `clinvar-link-data status` + one repository lookup → proves the parser/builder handle
  the real 43-column format end to end. (Do NOT download the full 9 GB.)
- [ ] **Step 3:** Start `clinvar-link serve` against the smoke DB; `curl /health`;
  run `mcp_server.py` stdio handshake (list tools). Confirm a `get_variant` call returns a
  real classified record with citation + release date.
- [ ] **Step 4:** `docker build` succeeds. Final commit + summary of what shipped.

---

## Self-Review

**Spec coverage:** §2 stack→T1/T2/T11; §3 refresh→T5; §4 parser+stars→T4; §5 schema→T3;
§6 repo/service→T6/T8; §7 tools+citation→T10/T8; §8 testing→fixtures T5 + tests across
tasks + T12; §9 docker/CI/docs→T13/T14; capabilities/resources→T9/T10. All covered.

**Placeholder scan:** No TBD/"handle edge cases"; each task names exact files, the sibling
to copy, signatures, and concrete test assertions.

**Type/name consistency:** `build_database`, `ClinVarRepository`, `ClinVarService`,
`create_clinvar_mcp`, `register_clinvar_tools`, `run_mcp_tool`, `map_classification`,
`map_star_rating`, `format_accession`, `GeneAccumulator` — used consistently throughout.

**Divergences from goal (intentional, per spec):** official ClinVar star convention (not
kidney confidence scale); `submission_summary` flag-gated/off in v1.
