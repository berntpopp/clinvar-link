# Configuration

Every setting uses the **flat `CLINVAR_LINK_` env prefix** (no nested `__`
delimiters) and can be supplied via the environment or an `.env` file. See
[`.env.example`](../.env.example) for a local starting point and
[`.env.docker.example`](../.env.docker.example) for Compose.

Defaults below are the **application** defaults from `clinvar_link/config.py`.
The Docker images and Compose files carry their own (documented) overrides.

## Local data store

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATA_DIR` | `./data` | Directory for the bulk download + SQLite index. |
| `DB_FILENAME` | `clinvar.sqlite` | SQLite filename inside `DATA_DIR`. |
| `SOURCE_URL` | NCBI `variant_summary.txt.gz` | Bulk-release source URL. |
| `REFRESH_TTL_DAYS` | `7` | Skip a refresh if the index is younger than this. |
| `AUTO_BOOTSTRAP` | `false` | Build the DB in-app on first start if absent. Superseded by the entrypoint's `bootstrap`; cron/CLI owns refresh in production. |
| `ENABLE_HGVS4VARIATION` | `false` | Opt-in: also ingest `hgvs4variation.txt.gz` (**all** transcript-version HGVS expressions) for exhaustive coverage, at ~2x DB size (~8 GB). See [data](data.md#hgvs-indexing-strategy). |
| `HGVS4VARIATION_URL` | NCBI `hgvs4variation.txt.gz` | Source for the opt-in HGVS enrichment. |
| `ENABLE_SUBMISSION_SUMMARY` | `false` | Index `submission_summary` per-submitter detail. Off in v1. |
| `SOURCE_MAX_BYTES` | `1 GiB` | Download guard for the compressed source. |
| `SOURCE_MAX_EXPANDED_BYTES` | `8 GiB` | Download guard for the expanded source. |
| `MAX_DOWNLOAD_SECONDS` | `3600` | Total download time budget. |

## Prebuilt-bundle distribution

How the server obtains a prebuilt `clinvar.sqlite.zst` instead of building one.

| Variable | Default | Purpose |
|----------|---------|---------|
| `ENVIRONMENT` | `development` | `development` or `production`. Production enforces the pinning rules below. |
| `BUNDLE_URL` | `""` (disabled) | `latest` (newest GitHub release asset), `""` (no bundle pull), or a full `.sqlite.zst` URL pinning one snapshot. |
| `DEVELOPMENT_LATEST` | `false` | Must be `true` to accept the floating `BUNDLE_URL=latest`. |
| `BUNDLE_RELEASE_TAG` | unset | Exact `bundle-YYYY-MM-DD` release tag. **Required in production.** |
| `BUNDLE_EXPECTED_SHA256` | unset | Expected sha256 of the **compressed** asset. **Required in production.** |
| `BUNDLE_EXPECTED_EXPANDED_SHA256` | unset | Expected sha256 of the **expanded** DB. **Required in production.** |
| `BUNDLE_EXPECTED_SCHEMA_VERSION` | unset | Must be `1.0.0` in production. |
| `BUNDLE_PATH` | unset | Install from a **local** `.zst` file instead of downloading (air-gapped). |
| `GITHUB_REPO` | `berntpopp/clinvar-link` | Repo whose Releases publish the bundle. |
| `BUNDLE_ASSET_NAME` | `clinvar.sqlite.zst` | Release asset name. |
| `BUILD_LOCAL` | `false` | Fall back to a full local build when no bundle is available (`bootstrap`). |
| `BUNDLE_DOWNLOAD_DIR` | `DATA_DIR` | Staging dir for the downloaded `.zst` before decompression. Shares a filesystem with the index so the swap is atomic. |
| `BUNDLE_REFERENCE_ROOT` | `DATA_DIR` | Root under which sha256-addressed bundle versions are materialized. |
| `BUNDLE_MAX_BYTES` | `2 GiB` | Guard on the compressed asset (GitHub's own release-asset cap). |
| `BUNDLE_MAX_EXPANDED_BYTES` | `8 GiB` | Guard on the expanded DB. |
| `METADATA_MAX_BYTES` | `1 MiB` | Guard on release metadata. |

> [!IMPORTANT]
> **`latest` is a floating pin and startup rejects it unless you opt in.**
> `BUNDLE_URL=latest` requires `DEVELOPMENT_LATEST=true`. With
> `ENVIRONMENT=production`, `DEVELOPMENT_LATEST` must be `false` and the config
> **fails to load** unless: `BUNDLE_RELEASE_TAG` is an exact `bundle-YYYY-MM-DD`
> tag, `BUNDLE_URL` is an `https://` URL containing `/download/<tag>/` (unless
> `BUNDLE_PATH` is set), both sha256 pins are present and well-formed, and
> `BUNDLE_EXPECTED_SCHEMA_VERSION` is `1.0.0`. Production consumes an
> **immutable, digest-pinned** bundle — never a moving one.

## Transport, security, and limits

| Variable | Default | Purpose |
|----------|---------|---------|
| `MCP_TRANSPORT` | `unified` | `unified` (FastAPI `/health` host + MCP at `/mcp`) or `http`. |
| `MCP_HOST` | `127.0.0.1` | Bind host. |
| `MCP_PORT` | `8000` | Bind port. |
| `MCP_PATH` | `/mcp` | MCP endpoint path. |
| `MCP_ALLOWED_HOSTS` | `["localhost","127.0.0.1","::1"]` | **Exact** `Host` values accepted by the request guard. Wildcards are rejected. Production must add the public reverse-proxy hostname. Write IPv6 entries **bare**, without brackets. |
| `MCP_ALLOWED_ORIGINS` | `[]` | Browser-`Origin` admission gate. Requests **without** an `Origin` header remain valid. |
| `CORS_ORIGINS` | `*` | Comma-separated origins for CORS **response headers**. |
| `CACHE_SIZE` | `1024` | In-process cache capacity. |
| `CACHE_TTL_MINUTES` | `60` | Cache time-to-live. |
| `MAX_PAGE_SIZE` | `100` | Upper bound on search/list `limit`. |
| `LOG_LEVEL` | `INFO` | Log level. |
| `LOG_FORMAT` | `json` | `json` (prod) or `console` (dev). |
| `ENABLE_SWAGGER` | `true` | Serve the FastAPI docs UI. |
| `ENABLE_MONITORING` | `true` | Serve Prometheus metrics. |
| `GRACEFUL_SHUTDOWN_TIMEOUT` | `30` | Seconds to drain on shutdown. |

**`MCP_ALLOWED_ORIGINS` and `CORS_ORIGINS` are two different gates.**
`MCP_ALLOWED_ORIGINS` is the *request admission* policy enforced on every HTTP
route; `CORS_ORIGINS` only controls the CORS *response headers* a browser sees.
Include every origin you intend to serve in **both**, or a browser client will
be admitted and then blocked (or vice versa).

## MCP client configuration

**HTTP (Streamable HTTP)** — run `clinvar-link serve` and point the client at
the mounted MCP app:

```bash
claude mcp add --transport http clinvar-link --scope user http://127.0.0.1:8000/mcp
```

**stdio** (Claude Desktop and similar) via the `clinvar-link-mcp` entry point:

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

## Console entry points

- `clinvar-link` — `serve`, `config`, `health`, `version`.
- `clinvar-link-mcp` — the stdio MCP transport.
- `clinvar-link-data` — `build`, `refresh`, `status`, `pull`, `bootstrap`,
  `pack`, `publish` (see [data](data.md)).
