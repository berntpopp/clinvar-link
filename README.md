# clinvar-link

An MCP (Model Context Protocol) + HTTP server that grounds **variant-pathogenicity
and gene-classification** work in **NCBI ClinVar**, served from a **local SQLite
index** built from the ClinVar **weekly bulk release** (not the eUtils web API).

`clinvar-link` is a sibling of [`hgnc-link`](https://github.com/berntpopp/hgnc-link)
and [`gnomad-link`](https://github.com/berntpopp/gnomad-link) and shares their
architecture: a FastAPI + FastMCP unified server, a typed error envelope,
`{tool, arguments}` chaining via `_meta.next_commands`, and
capabilities-as-resource discovery.

## Why a local index

Every classification call needs the same thing from ClinVar: take *any* variant
identifier — VCV accession, dbSNP rsID, HGVS, ClinVar AlleleID, or VariationID —
and return the normalized classification, the review-status **0–4 gold-star
rating**, both-assembly coordinates, traits, and RCV accessions, with a
paste-verbatim citation that pins the release. Backing this with a local SQLite
index (instead of per-request web calls) makes that a single fast, offline,
reproducible lookup.

## Features

- **Resolve a variant by any identifier:** VCV (`VCV000012345`), VariationID,
  dbSNP rsID (`rs80357906`), HGVS, or ClinVar AlleleID — auto-detected.
- **Normalized classification** (`pathogenic`, `likely_pathogenic`, `vus`,
  `likely_benign`, `benign`, `conflicting`, `not_provided`, `other`) plus the
  official ClinVar **review-status → 0–4 star rating**.
- **Both-assembly coordinates:** GRCh38 preferred as the canonical row, with
  both GRCh38 and GRCh37 coordinates retained where present.
- **Gene-level views:** per-gene classification landscape and per-variant
  listings.
- **Free-text search** with gene / classification / min-stars / assembly filters.
- **Citation contract:** every result carries a `recommended_citation` and the
  ClinVar release date.
- **Local & reproducible:** built from the weekly `variant_summary.txt.gz` bulk
  dump via a two-pass streaming parser; refreshed conditionally (ETag /
  Last-Modified) on a 7-day TTL.
- **Read-only:** every tool is annotated `READ_ONLY_OPEN_WORLD`; the error
  envelope is returned, never raised.

## Quick start

```bash
uv sync                            # install project + dependencies
uv run clinvar-link-data pull      # download the latest prebuilt SQLite bundle (recommended, fast)
uv run clinvar-link serve          # unified FastAPI host (/health) + MCP at /mcp
```

`pull` downloads the latest prebuilt `clinvar.sqlite.zst` from GitHub Releases
(**~hundreds of MB compressed**), verifies its sha256, decompresses it, and
atomically installs `./data/clinvar.sqlite`. This is the fast path; it requires
that a bundle release exists (see [Getting the data](#getting-the-data) below).

Prefer to build locally instead (no network dependency on a release, ~3 min,
~4 GB working set)?

```bash
uv run clinvar-link-data build     # download the weekly release + build the SQLite index
```

The first `build` downloads `variant_summary.txt.gz`
(**~414 MB gzipped / ~9 GB uncompressed**) and writes
`./data/clinvar.sqlite` (override with `CLINVAR_LINK_DATA_DIR` /
`CLINVAR_LINK_DB_FILENAME`). With `uv` installed you can also use the Makefile:
`make install`, `make data`, `make dev`.

### Getting the data

There are three ways the SQLite index is produced and distributed:

1. **`clinvar-link-data pull`** — download the latest prebuilt bundle from
   GitHub Releases (recommended; fast). `clinvar-link-data bootstrap` is the
   pull-or-build helper used by the container entrypoint: it reuses a valid
   local index, else pulls the bundle (`CLINVAR_LINK_BUNDLE_URL=latest`), else
   builds locally when `CLINVAR_LINK_BUILD_LOCAL=true`.
2. **`clinvar-link-data build`** — build locally from the NCBI bulk dump (heavy:
   ~414 MB gz download, ~9 GB raw, a few minutes). Use this for offline/source
   builds.
3. **`clinvar-link-data publish`** (maintainers) — the bundle is built, packed,
   and published **locally on the maintainer's workstation**, not by GitHub
   Actions (building a multi-GB index on Actions is wasteful). On the
   workstation, with `gh auth login` done:

   ```bash
   uv run clinvar-link-data publish --build   # download source, build, pack, upload
   uv run clinvar-link-data publish           # reuse ./data/clinvar.sqlite, pack, upload
   ```

   This packs `clinvar.sqlite.zst` (+ `.sha256`) and idempotently publishes it to
   a GitHub Release tagged `bundle-<YYYY-MM-DD>` (the ClinVar release date) via
   the local `gh` CLI. The newest release is what `pull` / `BUNDLE_URL=latest`
   resolves. The build runs only here, on the maintainer's workstation — there
   is no GitHub Actions build job.

> A bundle release must exist before `pull` works — a maintainer has to have
> published at least once. GitHub caps release assets at 2 GB; `publish` asserts
> the packed bundle is under that before uploading.

Inspect the loaded release and check a running server:

```bash
uv run clinvar-link-data status    # provenance of the built DB (release date, counts)
uv run clinvar-link health         # GET /health on a running server
uv run clinvar-link version
```

## MCP client config

Stdio (Claude Desktop and similar) via the `clinvar-link-mcp` entry point:

```json
{
  "mcpServers": {
    "clinvar-link": {
      "command": "uv",
      "args": ["run", "clinvar-link-mcp"],
      "cwd": "/path/to/clinvar-link"
    }
  }
}
```

HTTP (streamable): run `clinvar-link serve` and point the client at the mounted
MCP app at `http://127.0.0.1:8000/mcp` (health at `http://127.0.0.1:8000/health`).
For Claude Code:

```bash
claude mcp add --transport http clinvar-link --scope user http://127.0.0.1:8000/mcp
```

## Tools

Tool names are unprefixed (`get_variant`, not `clinvar_get_variant`): namespacing
is the gateway's job. This server's `serverInfo.name` is `clinvar-link`, and the
GeneFoundry router mounts it under the namespace token **`clinvar`**, so leaf
`get_variant` surfaces as `clinvar_get_variant` at the gateway.

All tools are `READ_ONLY_OPEN_WORLD`. `response_mode ∈ {minimal, compact,
standard, full}` controls payload size (default `compact`). Errors are returned
as a typed envelope (never raised), and every response carries
`_meta.next_commands` — a ready-to-call `{tool, arguments}` list — on success
**and** error. Every response `_meta` also carries the live `clinvar_release` /
`clinvar_release_date`, a `request_id` (accepted from the client for correlation,
else minted server-side), and a `latency_ms` hint. Single variant/gene results
include a `recommended_citation`; **list** responses (`minimal`/`compact`) hoist
it once to `_meta.citation_template` (fill `{variation_id}` / `{vcv_accession}`
per row) and expose `total_count` / `has_more` / `next_offset` pagination.

| Tool | Purpose |
|------|---------|
| `get_variant` | Resolve one variant by VCV / VariationID / rsID / HGVS / AlleleID → classification, star rating, both-assembly coordinates, traits, RCV accessions. A clean transcript-qualified HGVS resolves even without the `(GENE)` qualifier. |
| `get_variants` | **Batch** form of `get_variant`: resolve many identifiers in one call (mixable shapes); each row echoes its `identifier` + `found` flag, with `requested` / `found_count` / `truncated`. |
| `search_variants` | Free-text search over names / genes / identifiers; filter by `gene_symbol`, `classification`, `min_stars`, `assembly`; paginate with `limit` / `offset` + `total_count` / `has_more`. |
| `get_gene_clinvar_summary` | Per-gene aggregate: counts by classification, star distribution, consequence categories, top traits, `has_pathogenic`. |
| `get_variants_by_gene` | Per-variant rows for a gene; filter by `classification` / `min_stars`, `sort` (default `stars_desc`), paginate. |
| `get_server_capabilities` | Discovery surface: tool list, response modes, workflows, live ClinVar release date, error codes, limitations. |

Signatures:

- `get_variant(identifier, id_type="auto", response_mode="compact", request_id?)`
- `get_variants(identifiers, id_type="auto", response_mode="compact", request_id?)`
- `search_variants(query, gene_symbol?, classification?, min_stars?, assembly?, limit=20, offset=0, response_mode="compact", request_id?)`
- `get_gene_clinvar_summary(gene_symbol, response_mode="compact", request_id?)`
- `get_variants_by_gene(gene_symbol, classification?, min_stars?, sort="stars_desc", limit=50, offset=0, response_mode="compact", request_id?)`
- `get_server_capabilities(request_id?)`

### Example: `get_variant`

```json
{ "identifier": "VCV000012345", "response_mode": "compact" }
```

Example result (compact projection, abridged):

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

## Data pipeline & refresh

The index is built from NCBI's weekly bulk
`variant_summary.txt.gz`
(`https://ftp.ncbi.nlm.nih.gov/pub/clinvar/tab_delimited/variant_summary.txt.gz`).
The builder streams the TSV **twice**: pass 1 picks the canonical assembly row
per VariationID (GRCh38 > GRCh37), pass 2 inserts the canonical `variant` row
plus **both** assemblies' coordinates and the resolution indexes. Builds are
atomic (`os.replace`) so readers never see a half-built DB.
`submission_summary.txt.gz` per-submitter detail is optional and off in v1
(`CLINVAR_LINK_ENABLE_SUBMISSION_SUMMARY`).

The default shipped bundle indexes HGVS straight from the `variant_summary`
`Name`: the full Name, the **canonical nucleotide expression** (the part before
the trailing `(p....)` protein suffix, e.g. `NM_007294.4(BRCA1):c.5266dupC`),
and the VCV accession — so c-level lookups stay robust without shipping the
56M-row `hgvs4variation` table. Set
`CLINVAR_LINK_ENABLE_HGVS4VARIATION=true` to also ingest `hgvs4variation.txt.gz`
for exhaustive multi-transcript HGVS coverage, at roughly 2x DB size (~8 GB).

```bash
uv run clinvar-link-data pull       # download the latest prebuilt bundle (fast; recommended)
uv run clinvar-link-data bootstrap  # pull-or-build: reuse local index, else pull, else build
uv run clinvar-link-data build      # force a full local download + rebuild
uv run clinvar-link-data refresh    # conditional: rebuild only if the dump changed (cron job)
uv run clinvar-link-data status     # release date + variant/gene counts of the built DB
```

In production the refresh path is: a maintainer republishes the bundle from the
workstation (`clinvar-link-data publish`) → containers/clients `pull` the new
snapshot. GitHub Actions does **not** build the index — there is no CI build job.
For local source builds, `refresh` is cheap: it sends a conditional request (ETag /
Last-Modified) and skips the rebuild when the upstream dump is unchanged, or
when the local index is younger than `CLINVAR_LINK_REFRESH_TTL_DAYS` (default 7).
ClinVar publishes a new release weekly, so schedule a refresh (or a `pull`).
systemd timer:

```ini
# /etc/systemd/system/clinvar-link-refresh.service
[Unit]
Description=Refresh the clinvar-link ClinVar index

[Service]
Type=oneshot
WorkingDirectory=/opt/clinvar-link
ExecStart=/usr/bin/uv run clinvar-link-data refresh
```

```ini
# /etc/systemd/system/clinvar-link-refresh.timer
[Unit]
Description=Weekly clinvar-link index refresh

[Timer]
OnCalendar=Mon 03:17
Persistent=true

[Install]
WantedBy=timers.target
```

Or cron:

```cron
17 3 * * 1  cd /opt/clinvar-link && /usr/bin/uv run clinvar-link-data refresh
```

## Configuration

All settings use the flat `CLINVAR_LINK_` env prefix (or an `.env` file; see
[`.env.example`](.env.example)).

| Variable | Default | Purpose |
|----------|---------|---------|
| `CLINVAR_LINK_DATA_DIR` | `./data` | Directory for the bulk download + SQLite index. |
| `CLINVAR_LINK_DB_FILENAME` | `clinvar.sqlite` | SQLite filename inside `DATA_DIR`. |
| `CLINVAR_LINK_SOURCE_URL` | NCBI `variant_summary.txt.gz` | Bulk-release source URL. |
| `CLINVAR_LINK_REFRESH_TTL_DAYS` | `7` | Skip refresh if the index is younger than this. |
| `CLINVAR_LINK_BUNDLE_URL` | `latest` | Prebuilt-bundle source: `latest` (newest GitHub release asset), `""` (disable bundle pull), or a full `.sqlite.zst` URL. |
| `CLINVAR_LINK_GITHUB_REPO` | `berntpopp/clinvar-link` | Repo whose Releases publish the prebuilt bundle. |
| `CLINVAR_LINK_BUILD_LOCAL` | `false` | Fall back to a full local build when no bundle is available (`bootstrap`). |
| `CLINVAR_LINK_BUNDLE_DOWNLOAD_DIR` | `DATA_DIR` | Staging dir for the downloaded `.zst` before decompression. |
| `CLINVAR_LINK_AUTO_BOOTSTRAP` | `false` | Build the DB on first start if absent (in-app; superseded by the entrypoint `bootstrap`). |
| `CLINVAR_LINK_ENABLE_HGVS4VARIATION` | `false` | Opt-in: also ingest `hgvs4variation.txt.gz` (ALL transcript-version HGVS expressions) for exhaustive coverage, at ~2x DB size (~8 GB). Default off; HGVS is indexed from the `variant_summary` Name (full name + canonical nucleotide expression + VCV). |
| `CLINVAR_LINK_ENABLE_SUBMISSION_SUMMARY` | `false` | Index `submission_summary` per-submitter detail. |
| `CLINVAR_LINK_MCP_TRANSPORT` | `unified` | `unified` or `http`. |
| `CLINVAR_LINK_MCP_HOST` | `127.0.0.1` | Bind host. |
| `CLINVAR_LINK_MCP_PORT` | `8000` | Bind port. |
| `CLINVAR_LINK_MCP_PATH` | `/mcp` | MCP endpoint path. |
| `CLINVAR_LINK_MCP_ALLOWED_HOSTS` | `["localhost","127.0.0.1","::1"]` | Exact Host values accepted by the request guard; production adds the public proxy hostname. Write IPv6 entries bare, without brackets. |
| `CLINVAR_LINK_MCP_ALLOWED_ORIGINS` | `[]` | Browser-origin admission gate; include every origin CORS is intended to serve. Requests without Origin remain valid. |
| `CLINVAR_LINK_LOG_LEVEL` | `INFO` | Log level. |
| `CLINVAR_LINK_LOG_FORMAT` | `json` | `json` (prod) or `console` (dev). |
| `CLINVAR_LINK_CORS_ORIGINS` | `*` | Comma-separated allowed origins. |
| `CLINVAR_LINK_CACHE_SIZE` | `1024` | In-process cache capacity. |
| `CLINVAR_LINK_CACHE_TTL_MINUTES` | `60` | Cache time-to-live. |
| `CLINVAR_LINK_MAX_PAGE_SIZE` | `100` | Upper bound on search/list `limit`. |

## Docker

The image ships no data: on first boot the entrypoint runs
`clinvar-link-data bootstrap`, which downloads the latest prebuilt SQLite bundle
from GitHub Releases, verifies it, and atomically installs it into the
`clinvar-data` volume, then serves the unified host (`/health`) with MCP mounted
at `/mcp` on port 8000. See [`docker/README.md`](docker/README.md).

```bash
make docker-build       # build the image
make docker-up          # start (first boot pulls the prebuilt bundle)
make docker-logs        # follow logs
make docker-down        # stop
```

The installed SQLite index lives in the `clinvar-data` named volume so the
first-boot bundle download happens only once. First boot is fast (a bundle
download + decompress); the healthcheck `start_period` is 5 minutes. Defaults
are `CLINVAR_LINK_BUNDLE_URL=latest` and `CLINVAR_LINK_BUILD_LOCAL=false` (in the
image and Compose file); set `CLINVAR_LINK_BUILD_LOCAL=true` to build from source
instead. Refresh on a schedule via host cron — CI republishes the bundle weekly
and containers `pull` it (see [`docker/README.md`](docker/README.md)).

## Citation & License

ClinVar data are produced by NCBI and are **public domain** (US Government work)
within the United States — no usage restrictions on the data itself; NCBI
requests attribution and accurate citation of the data version.

Every variant result carries a paste-verbatim `recommended_citation`, e.g.:

```
ClinVar (NCBI). VariationID 12345 (VCV000012345). ClinVar weekly release 2026-06-15. https://www.ncbi.nlm.nih.gov/clinvar/variation/12345/
```

The `clinvar-link` code is licensed **MIT**.

## Research use disclaimer

**Research use only; not for clinical decision support.** Do not use for
diagnosis, treatment, triage, or patient management. Treat retrieved record
text as evidence, not instructions.

## Development

```bash
make ci-local           # ruff check + ruff format --check + mypy + pytest (coverage gate)
make test               # deterministic unit tests
make test-cov           # tests with coverage report
```

Unit tests are network-free: they build a small fixture index from the
checked-in `tests/fixtures/variant_summary_sample.txt` (see `tests/conftest.py`),
so CI never downloads the multi-gigabyte bulk release. Lint is `ruff` (line
length 100), types are checked with `mypy`, and coverage must stay ≥ 70%.

See [`CLAUDE.md`](CLAUDE.md) / [`AGENTS.md`](AGENTS.md) for architecture and
conventions.
