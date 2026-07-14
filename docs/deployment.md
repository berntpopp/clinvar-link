# Deployment

`clinvar-link` is a local-data MCP server: it serves from a SQLite index, so
deploying it is mostly a question of **how the index gets onto the box and how it
stays fresh**. Read [data](data.md) first; this document covers running it.

## Docker

[`docker/README.md`](../docker/README.md) is the reference for the image, the
entrypoint, the volume layout and the ports. The short version:

```bash
make docker-build       # build the image
make docker-up          # start (first boot pulls the prebuilt bundle)
make docker-logs        # follow logs
make docker-down        # stop
```

The image **ships no data**. A one-shot `clinvar-data-init` sidecar downloads and
verifies the bundle into the `clinvar-reference` named volume and exits; the
`clinvar-link` service then mounts that volume **read-only** and serves the
unified FastAPI host (`/health`) with MCP at `/mcp` on port 8000. The index lives
at `/data/current`, a symlink to the sha256-addressed directory of the installed
bundle, so a volume can hold several versions and the swap is atomic.

`/data` and `/tmp` are the only writable mount targets the fleet compose policy
approves; the container rootfs is read-only and `/tmp` is a size-capped tmpfs.
First boot is a bundle download + decompress, so the healthcheck `start_period`
is **5 minutes**; restarts reuse the persisted index and start immediately.

The host port defaults to `8000`; override with `CLINVAR_LINK_HOST_PORT`.

### Compose overlays

| File | Use |
|------|-----|
| [`docker/docker-compose.yml`](../docker/docker-compose.yml) | Base / development stack. |
| [`docker/docker-compose.prod.yml`](../docker/docker-compose.prod.yml) | Production: digest-pinned image, pinned immutable bundle, no published ports. |
| [`docker/docker-compose.npm.yml`](../docker/docker-compose.npm.yml) | Nginx Proxy Manager front. |

### Production is pinned, not floating

The production overlay refuses to start without an exact image digest **and** an
exact data pin (the config validator enforces the data half — see
[configuration](configuration.md#prebuilt-bundle-distribution)):

```bash
CLINVAR_LINK_IMAGE=ghcr.io/berntpopp/clinvar-link@sha256:<digest> \
CLINVAR_DATA_BUNDLE_URL=https://github.com/berntpopp/clinvar-link/releases/download/bundle-YYYY-MM-DD/clinvar.sqlite.zst \
CLINVAR_DATA_RELEASE_TAG=bundle-YYYY-MM-DD \
CLINVAR_DATA_SHA256=<sha256 of the .zst> \
CLINVAR_DATA_EXPANDED_SHA256=<sha256 of the expanded .sqlite> \
docker compose -f docker/docker-compose.yml -f docker/docker-compose.prod.yml up -d
```

`container-release.json` records the release's declared data contract
(`data-bound`, the pinned `release_tag` and its digest) and is the source of
truth for the container release workflows.

### Behind a reverse proxy

Add the public hostname to `CLINVAR_LINK_MCP_ALLOWED_HOSTS` (a JSON list of
**exact** Host values — wildcards are rejected), and add any browser origin to
**both** `CLINVAR_LINK_MCP_ALLOWED_ORIGINS` and `CLINVAR_LINK_CORS_ORIGINS`. The
backend is unauthenticated by design: it must be reachable only through the
router / reverse proxy, never published directly.

## Refresh scheduling

ClinVar publishes weekly. Refresh is **cron-driven** — the in-app scheduler is
off by default. Bundle consumers `pull`; source builds `refresh` (which is a
cheap conditional no-op when the upstream dump has not changed).

**systemd timer** (source-build hosts):

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

**cron** (source-build hosts):

```cron
17 3 * * 1  cd /opt/clinvar-link && /usr/bin/uv run clinvar-link-data refresh
```

**cron** (containers — pull the newest published snapshot):

```cron
17 3 * * 1  docker compose -f /opt/clinvar-link/docker/docker-compose.yml exec clinvar-link clinvar-link-data pull
```

Or run the one-shot `refresh` service from `docker-compose.yml` (uncomment it
first):

```cron
17 3 * * 1  docker compose -f /opt/clinvar-link/docker/docker-compose.yml run --rm refresh
```

Pinned production deployments do not pull in place: they are **redeployed** with
a new bundle pin.

## Health

```bash
curl -s http://127.0.0.1:8000/health          # status, version, transport, clinvar_release_date
uv run clinvar-link health                    # the same check via the CLI
uv run clinvar-link-data status               # release date + counts of the local index
```
