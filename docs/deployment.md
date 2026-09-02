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

### Fleet deploy contract

The GeneFoundry fleet controller (`strato_v6_docker_npm`) deploys and validates
only `docker/docker-compose.npm.yml`. Every service there declares a numeric
`user: "<uid>:<gid>"` — this image's own value from `docker/Dockerfile`, never
copied from a sibling `-link` repo. `user` must **not** appear in the Compose
files `container-release.json` lists (`docker-compose.yml`,
`docker-compose.prod.yml`); the shared release gate forbids it there. Self-check
the same way the controller does before releasing:

```bash
CLINVAR_LINK_IMAGE=ghcr.io/berntpopp/clinvar-link@sha256:<64 zeros> \
CLINVAR_DATA_BUNDLE_URL=https://example.invalid/bundle.tar.gz \
CLINVAR_DATA_RELEASE_TAG=bundle-2026-08-31 \
CLINVAR_DATA_SHA256=<64 zeros> CLINVAR_DATA_EXPANDED_SHA256=<64 zeros> \
docker compose -f docker/docker-compose.npm.yml config --format json > /tmp/clinvar.json
# from a strato_v6_docker_npm checkout:
uv run python -c "import sys, json; sys.path.insert(0, 'scripts'); \
from utils.deployment_preflight import canonical_projection; \
print(canonical_projection(json.load(open('/tmp/clinvar.json')), project='clinvar-link')['services'].keys())"
```

### Runtime data identity (`runtime-v1`)

The fleet controller deploys a new **data** release by switching the physical volume the
stack mounts, so it has to be able to ask a running container what data it is actually
serving. `clinvar-link` answers that on `/health`:

```jsonc
{
  "status": "healthy",
  "data_available": true,
  "release_identity": {
    "schema_version": 1,
    "data_identity": {
      "expected": { "release_tag": "bundle-2026-08-31", "digest": "sha256:70e8…" },
      "actual":   { "release_tag": "bundle-2026-08-31", "digest": "sha256:70e8…" }
    }
  }
}
```

`expected` is what the deployment was configured for (`CLINVAR_LINK_BUNDLE_RELEASE_TAG` +
`CLINVAR_LINK_DATA_IDENTITY_DIGEST`, which is `container-release.json` `.data.digest`).
`actual` is proven from the bytes on the volume: `clinvar-data-init` seals a canonical
`data-identity-manifest.json` — every authoritative file's path, size and SHA-256 —
beside the index it just installed, and the server rehashes all of it once when it opens
the store. **Unequal is not healthy**: `/health` returns `503` with
`data_available: false` and no `release_identity`, so a proxy and the controller both stop
short of serving the wrong data release. Verification is a store-open cost, not a
per-request one; the index is ~4.8 GB.

The controller also execs a deterministic, read-only semantic probe in the app container:

```bash
docker compose exec -T clinvar_link python -m clinvar_link.data_probe
# {"data_schema_version":"1","query_result_sha256":"d4735e3a…","record_count":4558706}
```

It opens the index `mode=ro&immutable=1` (so observing can never create a `-wal` sidecar
and invalidate the volume's identity), needs no network, and runs as the image's non-root
user.

The data volume's logical Compose key is `clinvar-data` — the key the controller's
reviewed adapter table names — and its physical name is selectable so a candidate volume
can be switched in:

```yaml
volumes:
  clinvar-data:
    name: "${CLINVAR_DATA_VOLUME:-clinvar-link-npm_clinvar-data}"
```

The default is the volume that already exists on the server, so an unchanged environment
renders an unchanged name.

### Production is pinned, not floating

The production overlay refuses to start without an exact image digest **and** an
exact data pin (the config validator enforces the data half — see
[configuration](configuration.md#prebuilt-bundle-distribution)):

```bash
CLINVAR_LINK_IMAGE=ghcr.io/berntpopp/clinvar-link@sha256:<digest> \
CLINVAR_DATA_BUNDLE_URL=https://github.com/berntpopp/clinvar-link/releases/download/bundle-2026-08-31/clinvar.sqlite.zst \
CLINVAR_DATA_RELEASE_TAG=bundle-2026-08-31 \
CLINVAR_DATA_SHA256=463a73b2ae8aab3bc2758e703b3207d3b82eb23ca90b56b51f3fef3c73babac1 \
CLINVAR_DATA_EXPANDED_SHA256=afb1e6cbc7e4487e2db586e1726b8ca81eaa29b88b35a901c08a2e668ba72b85 \
docker compose -f docker/docker-compose.yml -f docker/docker-compose.prod.yml up -d
```

`container-release.json` records the release's declared data contract
(`data-bound`, the pinned `release_tag` and its digest) and is the source of
truth for the container release workflows. The NPM overlay consumes the same
exact pin from `.env.docker` and runs in production mode; it never uses
`latest`.

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
