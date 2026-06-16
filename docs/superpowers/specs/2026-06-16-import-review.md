# clinvar-link — Import / Storage / Distribution Review

**Date:** 2026-06-16
**Question asked:** optimize + parallelize + stream the ClinVar import; SQLite vs PostgreSQL;
compare siblings; must not break a 64 GB workstation; ideally **prebuild the DB and publish
to GitHub** like `genereviews-link`.

---

## 1. Measured reality (not assertions)

Built from a bounded **1,000,000-row** slice of the live `variant_summary.txt.gz`
(production gzip read path), instrumented:

| Metric | 1.0 M rows (measured) | Full file ≈ 6.0 M rows (linear extrapolation) |
|---|---|---|
| Variants (after GRCh38>37 dedup) | 508,705 | ~3.0 M |
| Genes | 18,902 | ~40 k |
| Wall time | **31.3 s** | **~3 min** |
| **Peak RSS** | **810 MB** | **~4.9 GB** |
| DB size | 841 MB | **~5 GB** |
| of which `gene_summary` | **257 MB (30 %)** | **~1.5 GB** |
| largest single gene_summary blob | 198 KB | — |

### Verdict on the 64 GB worry
**It will not break a 64 GB workstation.** Peak memory is bounded (streaming gzip +
a compact `dict[vid→priority]` + bounded insert batches); the full build extrapolates to
~5 GB RSS / ~3 min. The file is **never** loaded into RAM. ✅

### But two real defects surfaced
1. **`gene_summary` bloat (30 % of the DB is dead weight).** `GeneAccumulator.finalize()`
   emits `protein_variants` / `genomic_variants` detail lists (capped at 200/gene), JSON-dumped
   into `summary_json` — but `GeneClinVarSummary` has `extra="ignore"`, so the service **never
   reads them**. Largest blob is 198 KB for one gene. Dropping them shrinks `gene_summary`
   ~25× (≈257 MB → ≈10 MB) and the full DB ~5 GB → ~3.5 GB.
2. **Build does avoidable work** (see §3).

---

## 2. SQLite vs PostgreSQL — decision: **stay SQLite (decisively)**

Workload = **read-only, single-consumer** (one MCP client session), keyed lookups + FTS +
precomputed per-gene aggregates. Fleet survey (15 `*-link` repos): **every local-data sibling
uses SQLite + FTS5** (hgnc, gencc, mondo, mgi, panelapp, clingen). The only Postgres user is
`kidney-genetics-db` (multi-service ACID writes + scrapers) and `genereviews-link`
(needs **pgvector** for dense embedding search). Neither driver applies here.

| | SQLite (current) | PostgreSQL |
|---|---|---|
| Ops | none (one file, in-process) | server, migrations, pg_restore |
| Distribution | **ship one file** | dump + restore into a running server |
| Concurrency need | single reader | many writers — N/A here |
| Semantic search | no | pgvector — not needed |
| Fleet convention | ✅ matches 6 siblings | only RAG/multi-service siblings |

PostgreSQL would add a server and `pg_restore` for **zero benefit** on a read-only single-file
lookup service — and it would make the "publish a prebuilt artifact" goal *harder*. SQLite is
the right call, and the prebuild-publish model below makes it even more clearly right.
(DuckDB was considered — great parallel CSV ingest — but no sibling uses it, FTS/point-lookup
story is weaker, and the build is not the bottleneck once we prebuild in CI. Not worth a new engine.)

---

## 3. Build optimizations (P0 — pure wins, low risk)

Current build (`builder.py`) leaves speed/size on the table:

1. **Indexes + FTS are created *before* inserts.** `executescript(schema.sql)` runs all 9
   `CREATE INDEX` + the FTS5 table up front, so every insert maintains them.
   → **Split schema**: create *tables only*, bulk-insert, then `CREATE INDEX …` and
   `INSERT INTO variant_fts SELECT …` once at the end. Biggest single speed win on a 3 M-row build.
2. **No build-time PRAGMAs on the throwaway temp DB.** The temp file is atomically renamed, so
   durability mid-build is irrelevant. Set on the temp connection:
   `PRAGMA synchronous=OFF; PRAGMA journal_mode=OFF; PRAGMA temp_store=MEMORY;
   PRAGMA cache_size=-262144;` (256 MB) `PRAGMA mmap_size=…`.
3. **Trim `GeneAccumulator.finalize()`** to the fields the service actually serves (counts,
   star_distribution, consequence_categories, top_traits, has_pathogenic). Drop
   `protein_variants`/`genomic_variants`. → ~25× smaller `gene_summary`, smaller bundle, less RAM.
4. **File is read 3×** (pass-1 dedup, pass-2 emit, sha256). Compute the SHA-256 **during the
   download stream** (or fold into pass-1) → one fewer 414 MB pass.

Expected effect: build ~3 min → **~60–90 s**, DB ~5 GB → **~3.5 GB**, peak RSS lower.

### Parallelize? — **No (deliberately).**
SQLite has a single writer; the only parallelizable part is CPU-bound HGVS regex parsing, which
would need a multiprocessing parse → single-writer queue (real complexity). With P0 the build is
~60–90 s and **runs once a week in CI** (§4), so wall-time pressure disappears. Adding parallelism
here is unjustified complexity (YAGNI). Streaming is already correct and is the property that
matters for the 64 GB constraint. (If we ever *did* want it, DuckDB's multi-threaded `read_csv`
would beat hand-rolled multiprocessing — but we won't need it.)

---

## 4. Prebuild + publish to GitHub (P1 — the real answer to "don't melt my workstation")

Adopt the **`genereviews-link` distribution model**, simplified for a single SQLite file
(closer to `clingen-link`, which zstd-compresses its SQLite + a `.sha256`):

**CI (weekly, GitHub Actions on `berntpopp/clinvar-link`):**
1. `clinvar-link-data build` (downloads `variant_summary.txt.gz`, builds the index) — on GitHub's
   runners, not the user's box.
2. zstd-compress `clinvar.sqlite` → `clinvar-<release-date>.sqlite.zst`; write a `.sha256` sibling
   (clingen pattern: `"<digest>  <name>\n"`).
3. `gh release create bundle-<YYYY-MM-DD>` uploading the `.zst` + `.sha256`, tagged by the ClinVar
   release date read from the DB `meta` row.
   **Constraint:** GitHub release assets cap at **2 GB/file** — the compressed, gene_summary-trimmed
   DB must fit (estimate ~0.8–1.3 GB; CI will assert `< 2 GB`).

**Runtime / container (download instead of build):**
- New config: `BUNDLE_URL` (`""` | `"latest"` | full URL), `GITHUB_REPO="berntpopp/clinvar-link"`,
  `BUILD_LOCAL: bool`, `BUNDLE_DOWNLOAD_DIR`.
- Resolver (mirrors `genereviews-link/ingest/github_release.py`): resolve `latest` via the GitHub
  API → pick the `.sqlite.zst` asset → fetch sibling `.sha256` → stream-download with on-the-fly
  SHA-256 verification → zstd-decompress → **atomic `os.replace` into `db_path`**.
- Entrypoint precedence: existing fresh DB → else `BUNDLE_URL` download → else `BUILD_LOCAL`
  full build → else clear error. The Docker default flips from "build the 9 GB thing on first boot"
  to "**pull the ~1 GB prebuilt snapshot**" (with `tmpfs` staging like genereviews' prod overlay).
- Optional `AUTO_PULL_RELEASES` weekly check to hot-swap when a new release appears.

Net effect: a 64 GB workstation (or any client) **never runs the heavy build** — it downloads a
verified, compressed snapshot. The build happens once/week on CI. Local build stays as a fallback.

---

## 5. Additional ClinVar files worth ingesting (P2 — data quality)

From the official `tab_delimited/README` (all weekly, all keyed by VariationID and/or AlleleID):

| File | Value | Why |
|---|---|---|
| **hgvs4variation.txt.gz** | **HIGH** | **All** HGVS expressions per VariationID/AlleleID. We currently index only the single `Name` HGVS, so HGVS lookup misses most real queries. This file makes `get_variant` by HGVS actually robust. → populate `hgvs_lookup`. |
| var_citations.txt.gz | MEDIUM | PMIDs per variant — lets a grounding server cite literature (pairs with `pubtator-link`). |
| submission_summary.txt.gz | MEDIUM | Per-SCV submitter classifications + conflicts (already the flag-gated v1 enhancement). |
| variation_allele.txt.gz | LOW–MED | Complete VariationID↔AlleleID map (README warns variant_summary omits some). Improves AlleleID resolution. |
| gene_specific_summary.txt.gz | LOW | NCBI's own per-gene counts — could validate our `GeneAccumulator`. |
| cross_references.txt.gz | LOW | dbSNP/dbVar IDs (we already get RS# from variant_summary). |

No SO molecular-consequence file is documented there (we infer via regex — acceptable).
Recommendation: **add `hgvs4variation` in P2** (biggest quality lift); the rest optional.

---

## 6. Recommended order

- **P0** build optimizations (trim gene_summary, defer indexes/FTS, PRAGMAs, single sha256 pass) —
  pure win, no API change, do first.
- **P1** prebuild + publish-to-Releases + runtime bundle resolver — the architecturally important
  piece; directly satisfies "prebuild like genereviews" and "don't break the workstation".
- **P2** ingest `hgvs4variation` (and optionally `var_citations` / `submission_summary`).
