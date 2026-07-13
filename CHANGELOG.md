# Changelog

All notable changes to clinvar-link are documented here.

## [0.4.3] - 2026-07-13

### Added

- Split data materialization out of the server: a one-shot `clinvar-data-init`
  sidecar downloads and verifies the pinned ClinVar bundle into the
  `clinvar-reference` named volume and exits; `clinvar-link` waits for
  `service_completed_successfully` and then mounts the same volume **read-only**.
- Declare the sidecar in `container-release.json` under `service.auxiliary` with
  its `init` role, `approved-networks` egress (the bundle is fetched from GitHub
  Releases), and its exact `writable_targets` (`/data`, `/tmp`), so the central
  fleet compose gate authorizes it **by role, never by name**.

### Changed

- Adopt the GeneFoundry container-release caller workflow and code-only
  production image release configuration bound to the published ClinVar
  `bundle-2026-07-07` external data artifact.
- Production now runs `clinvar-link-data pull` in the init sidecar rather than
  `bootstrap`, so advancing the pinned bundle installs exactly that release
  instead of reusing whatever index the volume already holds.
- Move every container mount onto the two writable targets the fleet compose
  policy approves: the reference volume is mounted at `/data` (was
  `/app/reference`) and scratch is a size-capped tmpfs at `/tmp` (was
  `/tmp/clinvar-link`, which the image's `TMPDIR` now also points at).
- Harden both services to the Container & Deployment Hardening Standard:
  digest-pinned untagged image, `read_only` rootfs, `cap_drop: [ALL]`,
  `no-new-privileges`, `deploy.resources.limits` (cpus/memory/pids) instead of
  the service-level `pids_limit`, bounded `json-file` logging, no published
  ports, no `container_name`, and the standard `GF_HEALTHCHECK_HOST` healthcheck.
- Inline the compose service definitions: top-level `x-*` anchors are emitted
  verbatim by `docker compose config` and are rejected as unapproved fields.
