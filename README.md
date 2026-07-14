# clinvar-link

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![CI](https://github.com/berntpopp/clinvar-link/actions/workflows/ci.yml/badge.svg)](https://github.com/berntpopp/clinvar-link/actions/workflows/ci.yml)
[![Conformance](https://github.com/berntpopp/clinvar-link/actions/workflows/conformance.yml/badge.svg)](https://github.com/berntpopp/clinvar-link/actions/workflows/conformance.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

An **MCP** (Model Context Protocol) server that grounds variant-pathogenicity and
gene-classification questions in **NCBI ClinVar**, served from a local SQLite
index built from the ClinVar **weekly bulk release** — not from the eUtils web API.

> [!IMPORTANT]
> Research use only. Not clinical decision support. Do not use for diagnosis,
> treatment, triage, or patient management.

## Why

Every classification question asks ClinVar for the same thing: take *any* variant
identifier and return the normalized classification with its review-status
confidence. ClinVar's public surface makes that harder than it sounds.

- **The identifier space is fragmented.** A VCV accession, a VariationID, a dbSNP
  rsID, an HGVS expression and an AlleleID are five different lookups. This
  server auto-detects all five and resolves them to one record.
- **The answers arrive raw.** ClinVar returns free-text `ClinicalSignificance`
  and `ReviewStatus` strings that every consumer re-normalizes by hand. This
  server maps them once, to a fixed classification enum and the official
  **0–4 gold-star rating**, and keeps **both** assemblies' coordinates.
- **Per-request web calls are the wrong shape for an agent.** They are slow,
  rate-limited, and not reproducible — the answer can move under you mid-session.

The weekly bulk release has all of it, but it is a ~414 MB gzipped TSV nobody
wants to parse per question. Indexing it once into local SQLite makes each lookup
fast, offline and **reproducible**, and lets every result carry a
paste-verbatim citation that pins the exact ClinVar release it came from.

## Quick start

The server is hosted — no install, no data build:

```bash
claude mcp add --transport http clinvar https://clinvar-link.genefoundry.org/mcp
```

To run it yourself, **the data step is mandatory** — the server is useless
without an index (Python 3.12+, [uv](https://github.com/astral-sh/uv)):

```bash
uv sync
uv run clinvar-link-data pull      # REQUIRED: install the prebuilt SQLite index
uv run clinvar-link serve          # unified FastAPI host (/health) + MCP at /mcp
claude mcp add --transport http clinvar-link http://127.0.0.1:8000/mcp
```

`pull` downloads the prebuilt `clinvar.sqlite.zst` bundle from GitHub Releases
(~hundreds of MB compressed), verifies its sha256 and installs it atomically. It
needs a published bundle to exist. To build from the NCBI dump instead
(~3 minutes; a ~414 MB gzipped download, ~4 GB working set):

```bash
uv run clinvar-link-data build     # download the weekly release + build the index
```

Both paths and the container's first-boot `bootstrap` are described in
[docs/data.md](docs/data.md).

## Tools

| Tool | Purpose |
|------|---------|
| `get_variant` | Resolve one variant by VCV / VariationID / rsID / HGVS / AlleleID → classification, star rating, both-assembly coordinates, traits, RCV accessions. |
| `get_variants` | Batch form of `get_variant`: many identifiers (mixable shapes) in one call, each row echoing its `identifier` and `found` flag. |
| `search_variants` | Free-text search over names / genes / identifiers, filtered by gene, classification, minimum stars or assembly. |
| `get_gene_clinvar_summary` | Per-gene aggregate: counts by classification, star distribution, consequence categories, top traits, `has_pathogenic`. |
| `get_variants_by_gene` | Per-variant rows for a gene, filterable and sortable (default `stars_desc`), paginated. |
| `get_server_capabilities` | Discovery surface: tool list, response modes, workflows, live ClinVar release date, error codes, limitations. |

Leaf names are **unprefixed** per Tool-Naming Standard v1 — namespacing is the
gateway's job. This server's `serverInfo.name` is `clinvar-link`, and
`genefoundry-router` mounts it under the namespace token **`clinvar`**, so
`get_variant` surfaces as `clinvar_get_variant` at the gateway.

Parameters, `response_mode`, the `_meta` / `next_commands` envelope and the error
taxonomy: [docs/mcp-tool-catalog.md](docs/mcp-tool-catalog.md).

## Data & provenance

| | |
|---|---|
| **Source** | NCBI ClinVar [`variant_summary.txt.gz`](https://ftp.ncbi.nlm.nih.gov/pub/clinvar/tab_delimited/variant_summary.txt.gz), the **weekly** bulk release |
| **Shape** | A local read-only SQLite index; no per-request eUtils or web calls |
| **Refresh** | Weekly. Consumers `pull` a newly published bundle; source builds run `refresh`, which is a cheap conditional (ETag / Last-Modified) no-op when the dump is unchanged |
| **Data licence** | **Public domain** (a US Government work). No usage restrictions on the data itself; NCBI *requests* attribution and accurate citation of the data version |

Every result carries the ClinVar release it came from, and a paste-verbatim
`recommended_citation` (lists hoist it once to `_meta.citation_template`):

```
ClinVar (NCBI). VariationID 12345 (VCV000012345). ClinVar weekly release 2026-06-15. https://www.ncbi.nlm.nih.gov/clinvar/variation/12345/
```

Paste it verbatim; never paraphrase or fabricate it. Full account of the build,
the HGVS indexing strategy and the bundle-publishing model:
[docs/data.md](docs/data.md).

## Documentation

- [Data: source, build, distribution, refresh](docs/data.md) — the bulk pipeline, the prebuilt bundle, the refresh model, the citation contract.
- [MCP tool catalog](docs/mcp-tool-catalog.md) — signatures, identifiers, response modes, the `_meta` envelope, error taxonomy.
- [Configuration](docs/configuration.md) — every `CLINVAR_LINK_*` variable, the Host/Origin guards, the production bundle-pinning rules, MCP client config.
- [Deployment](docs/deployment.md) — Docker, compose overlays, reverse proxy, refresh scheduling. Image details: [docker/README.md](docker/README.md).
- [AGENTS.md](AGENTS.md) — engineering conventions and architecture invariants.

## Contributing

See [AGENTS.md](AGENTS.md) for engineering conventions. `make ci-local` is the
definition-of-done gate: lint, format, README standard, mypy, and the tests
(which are network-free — they build a fixture index, never downloading the bulk
release).

## License

Code: [MIT](LICENSE) © Bernt Popp. Data: NCBI ClinVar is **public domain** (a US
Government work) within the United States, with attribution requested — see
[Data & provenance](#data--provenance).
