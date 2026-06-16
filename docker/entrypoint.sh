#!/usr/bin/env bash
# Ensure a usable ClinVar SQLite index exists before serving, then start the
# unified server. `clinvar-link-data bootstrap` owns the policy:
#   1. a valid local index is already present  -> reuse it (no-op),
#   2. else BUNDLE_URL is set                  -> pull the prebuilt snapshot
#      (download .zst, verify sha256, decompress, atomic-swap into place),
#   3. else BUILD_LOCAL=true                   -> build from the NCBI bulk dump,
#   4. else                                    -> error and exit non-zero.
# Bootstrap failures are fatal: we exit non-zero rather than serving an empty DB
# (the container fails loudly instead of silently lazy-building on first use).
set -euo pipefail

DATA_DIR="${CLINVAR_LINK_DATA_DIR:-/app/data}"
DB_FILENAME="${CLINVAR_LINK_DB_FILENAME:-clinvar.sqlite}"
DB_PATH="${DATA_DIR}/${DB_FILENAME}"

echo "[entrypoint] Bootstrapping the local ClinVar index (db=${DB_PATH})..."
if clinvar-link-data bootstrap; then
    echo "[entrypoint] ClinVar index ready."
else
    status=$?
    echo "[entrypoint] ERROR: bootstrap failed (exit ${status}); refusing to serve without an index." >&2
    exit "${status}"
fi

# exec so the server is PID 1 and receives SIGTERM/SIGINT for graceful shutdown.
exec clinvar-link serve \
    --transport "${CLINVAR_LINK_MCP_TRANSPORT:-unified}" \
    --host "${CLINVAR_LINK_MCP_HOST:-0.0.0.0}" \
    --port "${CLINVAR_LINK_MCP_PORT:-8000}"
