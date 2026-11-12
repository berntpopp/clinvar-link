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

[`var_citations.txt`](https://ftp.ncbi.nlm.nih.gov/pub/clinvar/tab_delimited/var_citations.txt)
(~250 MB, `CLINVAR_LINK_VAR_CITATIONS_URL`) is **always** ingested into `variant_citation`
(VariationID → PubMed / PubMedCentral / NCBIBookShelf id). It backs `get_variant`'s
`citations` and `get_variants_by_pmid`; a failed download fails the build, so a bundle never
silently lacks it. An index built before v0.6.0 has no such table: `get_variant` then omits
`citations` and `get_variants_by_pmid` reports an error instead of `not_found`.

**Submissions (OMIM) from the monthly VCV XML (#122).** `submission_summary.txt` has
`Description = -` for every OMIM SCV, so the per-family text is only in the monthly
[`ClinVarVCVRelease_00-latest.xml.gz`](https://ftp.ncbi.nlm.nih.gov/pub/clinvar/xml/)
(~6.1 GB gz, `CLINVAR_LINK_VCV_XML_URL`). The build streams it once straight from NCBI —
gzip and `lxml.etree.iterparse` over the HTTP body, nothing written to disk, each
`VariationArchive` cleared after use (peak RSS ~75 MB) — and keeps only the SCVs of the
submitters in `models.enums.SUBMITTERS` (OMIM) in `submission` / `submission_gene`. The
compressed bytes are verified against NCBI's published `.md5` at the end of the stream; the
parsed OMIM count is checked against an independent count from `submission_summary.txt.gz`
(`CLINVAR_LINK_SUBMISSION_SUMMARY_URL`) and the build fails above 5% drift. HGNC's
`hgnc_complete_set.txt` (`CLINVAR_LINK_HGNC_URL`) fills `gene_alias`, so a renamed gene finds
submissions filed under its previous symbol. All three pins (URL, Last-Modified, MD5 /
SHA-256, record count) are in the `data_source` table and in `bundle-metadata.json` `sources`.
Measured 2026-11-12: 4,708,791 records → 37,918 OMIM SCVs in 8.4 min (local) against 37,948 in
`submission_summary`. A failure in any of these fails the build. An index built before v0.7.0
lacks the tables: `get_variant` omits `submissions` and the `submitter` listing errors.

A bundle-format change with an UNCHANGED upstream (new tables) cannot reuse the immutable
`bundle-<date>` tag: dispatch the Data bundle workflow with `revision=2` to publish
`bundle-<date>-r2`.

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

## Runtime data identity

A deployment does not just need *an* index; it needs to be able to prove **which** data
release it is serving. When `pull` is given the complete exact identity
(`BUNDLE_RELEASE_TAG` + `BUNDLE_EXPECTED_SHA256` + `BUNDLE_EXPECTED_EXPANDED_SHA256` +
`BUNDLE_EXPECTED_SCHEMA_VERSION`) it materializes into an immutable version directory
`<BUNDLE_REFERENCE_ROOT>/<compressed sha256>/` and seals it:

```
/data/463a73…/clinvar.sqlite               # 0444, the index
/data/463a73…/data-identity.json           # release tag + compressed/expanded digests
/data/463a73…/data-identity-manifest.json  # 0444, canonical runtime-v1 manifest
/data/current -> 463a73…                   # selector, repointed last
```

`data-identity-manifest.json` is the GeneFoundry **`runtime-v1`** contract
(`clinvar_link/runtime_data_identity.py`): `{schema_version, release_tag, inputs}`, where
every input carries its relative path, byte size and SHA-256, sorted by path. The
identity **digest** is the SHA-256 of that manifest serialized as canonical JSON, so it
changes if any authoritative byte, any file name, or the release tag changes.

`clinvar-link-data pull` prints that digest as `data_identity_digest`. It is the value
that belongs in `container-release.json` `.data.digest` and in
`CLINVAR_LINK_DATA_IDENTITY_DIGEST`; the server rehashes the volume when it opens the
store and refuses to report healthy unless the two agree. See
[deployment](deployment.md#runtime-data-identity-runtime-v1) for the `/health` payload and
the read-only `python -m clinvar_link.data_probe` observation.

The manifest is written **last** and the `current` symlink is repointed **after** it, so a
reader that follows the selector always lands on a directory whose identity has already
been proven — including a candidate volume the fleet controller has not switched to yet.

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
that snapshot (production and NPM pin `bundle-2026-11-09-r2` by exact URL,
release tag, compressed SHA-256
`b92a826a74dbbe759d00f47cb5e8e738340681ad31a0513ed0c884a4c3f00027`, and
expanded-tree SHA-256
`edb5d43ad72b05f1121e23dbd24277b256c77213affc8c41b312f78ba5d6c773`; see
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
