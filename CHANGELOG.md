# Changelog

All notable changes to clinvar-link are documented here.

## [0.4.4] - 2026-07-13

### Fixed

- Release evidence now states the data contract this repository actually
  declares. The reusable release workflow hardcoded `data-independent` and a
  fixed `data_requirements: {"mode":"none"}`, so every published manifest
  claimed clinvar-link binds to no data at all, while `container-release.json`
  declares `data-bound` with an immutable pinned ClinVar bundle
  (`bundle-2026-07-07`) and its digest. Because `_require_data_binding` returns
  early for a data-independent contract, the binding assertion in the evidence
  chain was silently skipped as well.
- Re-pin both container workflows to the corrected container standard
  (`86b11f7e`), which sources the contract and the exact data identity from
  `container-release.json` and seals them into the capture artifact. This is an
  evidence-only re-release: the v0.4.3 image and attestations were sound.

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

### Fixed

- Hash the expanded bundle as a stream. `_expanded_tree_sha256` read the whole
  installed index into memory (`Path.read_bytes()`), so verifying the ~4.8 GB
  ClinVar SQLite made the data-init container exceed its memory limit and get
  OOM-killed (exit 137) before it could install the bundle. It is now hashed in
  bounded chunks, matching `mavedb-link`'s streaming implementation. The digest is
  unchanged.

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
