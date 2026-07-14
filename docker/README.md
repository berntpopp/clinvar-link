# Docker

```bash
make docker-build       # build the image
make docker-up          # start (unified FastAPI host + mounted MCP HTTP app)
make docker-logs        # follow logs
make docker-down        # stop
```

clinvar-link is a **local-data** MCP server: it answers from a SQLite index
built from the NCBI ClinVar weekly bulk release, not from a remote API at
request time. The image ships no data — on first boot the entrypoint runs
`clinvar-link-data bootstrap`, which **downloads the latest prebuilt SQLite
bundle** from GitHub Releases (`clinvar.sqlite.zst`), verifies its sha256,
decompresses it, and atomically installs it into the `clinvar-data` volume. It
then serves the unified FastAPI host (`/health`) with the MCP streamable-HTTP
app mounted at `/mcp` on a single port (8000).

## How the data gets there

The heavy build runs **once on the maintainer's workstation**, not in CI and
not in your container:

1. A maintainer builds + packs + publishes from their workstation:
   `clinvar-link-data publish --build` builds the SQLite index from the NCBI
   weekly dump, packs it into a zstd-compressed `clinvar.sqlite.zst` (+ a
   `.sha256` sidecar), and publishes it to a GitHub Release tagged
   `bundle-<YYYY-MM-DD>` via the local `gh` CLI. (There is no GitHub Actions
   build job — building a multi-GB index on Actions is wasteful.)
2. On first boot your container runs `clinvar-link-data bootstrap`, which pulls
   that prebuilt bundle (`CLINVAR_LINK_BUNDLE_URL=latest` of
   `CLINVAR_LINK_GITHUB_REPO`), verifies the sha256, decompresses, and
   atomically swaps it into place.
3. `bootstrap` is a no-op when a valid index is already present (restarts are
   instant), and falls back to a full local build only when
   `CLINVAR_LINK_BUILD_LOCAL=true`.

> A bundle release must already exist for the pull to succeed — a maintainer
> has to have published at least once. For fully offline or air-gapped builds,
> set `CLINVAR_LINK_BUILD_LOCAL=true` to build from source.

GitHub caps release assets at **2 GB**; the published `clinvar.sqlite.zst` is
well under that limit (the `publish` command asserts it and fails otherwise).

A bootstrap failure is **fatal**: the entrypoint exits non-zero rather than
serving an empty database, so the container fails loudly instead of silently
returning no results.

## First-boot data size

The first boot downloads the prebuilt bundle (**~hundreds of MB compressed**)
and decompresses it into the `clinvar-data` volume — far faster than a local
build, so the healthcheck `start_period` is set to **5 minutes**. Subsequent
restarts reuse the persisted index and start immediately.

If you set `CLINVAR_LINK_BUILD_LOCAL=true` instead, the first boot downloads the
ClinVar weekly release (`variant_summary.txt.gz`, **~414 MB gzipped / ~9 GB
uncompressed**) and builds the index locally — slower and disk-hungry; raise the
healthcheck `start_period` accordingly.

## Refresh

CI republishes the bundle on a weekly schedule
([`data-bundle.yml`](../.github/workflows/data-bundle.yml)), so production
refresh is just a **pull** of the newest snapshot. The in-app scheduler is not
used; refresh is owned by host cron / a sidecar. See
[`docs/deployment.md`](../docs/deployment.md#refresh-scheduling).

Run the one-shot refresh runner (uncomment the `refresh` service in
`docker-compose.yml` first) from host cron:

```cron
17 3 * * 1  docker compose -f /opt/clinvar-link/docker/docker-compose.yml run --rm refresh
```

Or exec into the running container:

```cron
17 3 * * 1  docker compose -f /opt/clinvar-link/docker/docker-compose.yml exec clinvar-link clinvar-link-data pull
```

## Volume

The `clinvar-data-init` sidecar materializes the verified bundle into the
`clinvar-reference` named volume, mounted at `/data`, and exits; `clinvar-link`
then mounts the same volume **read-only** and serves from it. The index lives at
`CLINVAR_LINK_DATA_DIR` (`/data/current`), a symlink to the sha256-addressed
directory of the installed bundle, so the volume can hold several versions and
the swap is atomic. The downloaded `.zst` is staged in
`CLINVAR_LINK_BUNDLE_DOWNLOAD_DIR` (`/data`) — the same filesystem as the index.

`/data` and `/tmp` are the only writable mount targets the fleet compose policy
approves; the container rootfs is read-only and `/tmp` is a size-capped tmpfs.

## Ports

The host port defaults to `8000`; override with `CLINVAR_LINK_HOST_PORT` (e.g.
in `.env.docker`). MCP endpoint: `http://127.0.0.1:<port>/mcp`. Health:
`http://127.0.0.1:<port>/health`.
