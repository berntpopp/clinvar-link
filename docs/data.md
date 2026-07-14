# Data: source, build, distribution, refresh

`clinvar-link` answers from a **local read-only SQLite index**, never from a
per-request eUtils or web call. This document is the full account of where that
index comes from, how it is distributed, and how it stays fresh.

## Upstream source

| | |
|---|---|
| File | [`variant_summary.txt.gz`](https://ftp.ncbi.nlm.nih.gov/pub/clinvar/tab_delimited/variant_summary.txt.gz) (`CLINVAR_LINK_SOURCE_URL`) |
| Publisher | NCBI ClinVar, **weekly** bulk release |
| Size | **~414 MB gzipped / ~9 GB uncompressed** |
| Local build cost | **~3 minutes**, ~4 GB working set |
| Prebuilt bundle | `clinvar.sqlite.zst`, **~hundreds of MB compressed** |

Download guards (all overridable, see [configuration](configuration.md)):
`SOURCE_MAX_BYTES` (1 GiB), `SOURCE_MAX_EXPANDED_BYTES` (8 GiB),
`MAX_DOWNLOAD_SECONDS` (3600).

## The build pipeline

`clinvar_link/ingest/builder.py` streams the TSV **twice**:

1. **Pass 1** picks the canonical assembly row per VariationID (**GRCh38 >
   GRCh37**).
2. **Pass 2** inserts the canonical `variant` row plus **both** assemblies'
   coordinates, the rsID / AlleleID / HGVS / gene resolution indexes, and the
   FTS5 row.

Builds are **atomic** (`os.replace`) under a build lock, so readers never see a
half-built database. `builder.SCHEMA_VERSION` is bumped on incompatible schema
changes. ReviewStatus maps to the **0–4 star rating** via
`clinvar_link/data/review_status_stars.yaml`; ClinicalSignificance is normalized
to `pathogenic | likely_pathogenic | vus | likely_benign | benign | conflicting |
not_provided | other`.

### HGVS indexing strategy

The shipped bundle indexes HGVS straight from the `variant_summary` **`Name`**
field — the full Name, the **canonical nucleotide expression** (the part before
the trailing `(p....)` protein suffix, e.g. `NM_007294.4(BRCA1):c.5266dupC`), and
the VCV accession. This keeps c-level lookups robust *without* shipping the
56M-row `hgvs4variation` table. Ambiguous bare short forms (`c.` / `p.`) are
deliberately **not** indexed.

Set `CLINVAR_LINK_ENABLE_HGVS4VARIATION=true` to also ingest
[`hgvs4variation.txt.gz`](https://ftp.ncbi.nlm.nih.gov/pub/clinvar/tab_delimited/hgvs4variation.txt.gz)
for exhaustive multi-transcript HGVS coverage (~12 keys/variant), at roughly
**2x DB size (~8 GB)**. The secondary download never fails the main build (it is
logged as a warning).

`submission_summary.txt.gz` per-submitter detail is optional and **off in v1**
(`CLINVAR_LINK_ENABLE_SUBMISSION_SUMMARY`).

## Distribution: three ways to get an index

```bash
uv run clinvar-link-data pull       # download the latest prebuilt bundle (fast; recommended)
uv run clinvar-link-data bootstrap  # pull-or-build: reuse local index, else pull, else build
uv run clinvar-link-data build      # force a full local download + rebuild
uv run clinvar-link-data refresh    # conditional: rebuild only if the dump changed (cron)
uv run clinvar-link-data status     # release date + variant/gene counts of the built DB
uv run clinvar-link-data pack       # pack ./data/clinvar.sqlite -> clinvar.sqlite.zst + .sha256
uv run clinvar-link-data publish    # (maintainers) pack + publish a GitHub Release
```

1. **`pull`** — downloads the prebuilt `clinvar.sqlite.zst` from GitHub
   Releases, **verifies its sha256**, decompresses it, and atomically installs
   `./data/clinvar.sqlite`. The fast path; it requires that a bundle release
   already exists.
2. **`bootstrap`** — the pull-or-build helper used by the container entrypoint:
   reuse a valid local index → else `pull` the bundle → else build locally only
   when `CLINVAR_LINK_BUILD_LOCAL=true` → else error and exit 1. **A bootstrap
   failure is fatal**: the entrypoint never serves an empty database.
3. **`build`** — a full local build from the NCBI bulk dump (heavy, see the
   sizes above). Use this for offline / air-gapped / source builds.

### Who publishes the bundle

Two publishers exist, and both produce the same artifact shape — a
`bundle-<YYYY-MM-DD>` GitHub Release (the ClinVar release date) carrying
`clinvar.sqlite.zst`:

- **CI** — [`.github/workflows/data-bundle.yml`](../.github/workflows/data-bundle.yml)
  runs on a weekly `schedule` (and `workflow_dispatch`): it builds, packs,
  writes `bundle-metadata.json` + `SHA256SUMS`, attests build provenance, and
  publishes the release immutably (an identical rerun is a no-op; a collision
  with an already-published tag fails loudly).
- **A maintainer's workstation** — `clinvar-link-data publish [--build]` packs
  `clinvar.sqlite.zst` (+ `.sha256`) and idempotently publishes it via the local
  `gh` CLI (`gh auth login` required). `--build` rebuilds from source first;
  the default reuses the existing `./data` index.

> [!IMPORTANT]
> A bundle release must **already exist** before `pull` (or `BUNDLE_URL=latest`)
> can resolve one — someone has to have published at least once. GitHub caps
> release assets at **2 GB**; `publish` asserts the packed bundle is under that
> before uploading, and `BUNDLE_MAX_BYTES` (2 GiB) enforces the same ceiling on
> the consuming side.

## Refresh

ClinVar publishes a new release **weekly**, so schedule a `refresh` (for source
builds) or a `pull` (for bundle consumers) — see
[deployment](deployment.md#refresh-scheduling) for the systemd timer and cron
lines.

`refresh` is deliberately cheap: it issues a **conditional** request
(ETag / Last-Modified) and skips the rebuild when the upstream dump is unchanged,
or when the local index is younger than `CLINVAR_LINK_REFRESH_TTL_DAYS`
(default **7**). Refresh is **CLI/cron-driven**; the in-app scheduler is off by
default (`CLINVAR_LINK_AUTO_BOOTSTRAP=false`).

In production the refresh path is: a new bundle is published → containers `pull`
that snapshot (production pins it by exact URL + tag + sha256; see
[configuration](configuration.md#prebuilt-bundle-distribution)).

## Licence & citation

ClinVar data are produced by NCBI and are **public domain** (a US Government
work) within the United States — there are **no usage restrictions on the data
itself**. NCBI *requests* attribution and accurate citation of the data version.

Every single-variant and gene result carries a paste-verbatim
`recommended_citation`; **list** responses hoist it once to
`_meta.citation_template`. Paste it verbatim; never paraphrase or fabricate it.

```
ClinVar (NCBI). VariationID 12345 (VCV000012345). ClinVar weekly release 2026-06-15. https://www.ncbi.nlm.nih.gov/clinvar/variation/12345/
```

Every response `_meta` also carries the live `clinvar_release` /
`clinvar_release_date` so a claim can always be pinned to a release.
