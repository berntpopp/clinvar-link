# Docker

```bash
make docker-build       # build the image
make docker-up          # start (unified FastAPI host + mounted MCP HTTP app)
make docker-logs        # follow logs
make docker-down        # stop
```

clinvar-link is a **local-data** MCP server: it answers from a SQLite index
built from the NCBI ClinVar weekly bulk release, not from a remote API at
request time. The image ships no data — the entrypoint downloads
`variant_summary.txt.gz` and builds the index into the `clinvar-data` volume on
first boot, then serves the unified FastAPI host (`/health`) with the MCP
streamable-HTTP app mounted at `/mcp` on a single port (8000).

## First-boot data size

The first boot downloads the ClinVar weekly release
(`variant_summary.txt.gz`, **~414 MB gzipped / ~9 GB uncompressed**) and builds
the SQLite index. This takes a while and needs disk headroom on the
`clinvar-data` volume, so the healthcheck `start_period` is set to 15 minutes.
Subsequent restarts reuse the persisted index and start immediately.

## Refresh

ClinVar publishes a **new weekly release**, so the index should be rebuilt on a
schedule. The in-app scheduler is not used; refresh is owned by host cron / a
sidecar. The entrypoint runs `clinvar-link-data refresh` on start when
`CLINVAR_LINK_AUTO_BOOTSTRAP=true` or the DB file is absent; `refresh` is
conditional and skips the rebuild when the upstream dump is unchanged (cheap
no-op) or the index is younger than `CLINVAR_LINK_REFRESH_TTL_DAYS` (default 7).

Run the one-shot refresh runner (uncomment the `refresh` service in
`docker-compose.yml` first) from host cron:

```cron
17 3 * * 1  docker compose -f /opt/clinvar-link/docker/docker-compose.yml run --rm refresh
```

Or exec into the running container:

```cron
17 3 * * 1  docker compose -f /opt/clinvar-link/docker/docker-compose.yml exec clinvar-link clinvar-link-data refresh
```

## Volume

The built SQLite index (and the cached bulk download) live under
`CLINVAR_LINK_DATA_DIR` (`/app/data`), persisted in the `clinvar-data` named
volume across container restarts so the multi-gigabyte first-boot
download/build happens only once.

## Ports

The host port defaults to `8000`; override with `CLINVAR_LINK_HOST_PORT` (e.g.
in `.env.docker`). MCP endpoint: `http://127.0.0.1:<port>/mcp`. Health:
`http://127.0.0.1:<port>/health`.
