#!/usr/bin/env bash
# Build/refresh the local ClinVar SQLite index before serving so the request
# path never triggers a lazy build, then start the unified server. Refresh is
# owned by cron / a sidecar in production (see docker/README.md), not the
# in-app scheduler.
set -euo pipefail

DATA_DIR="${CLINVAR_LINK_DATA_DIR:-/app/data}"
DB_FILENAME="${CLINVAR_LINK_DB_FILENAME:-clinvar.sqlite}"
DB_PATH="${DATA_DIR}/${DB_FILENAME}"

# Lowercase the bootstrap flag so "True"/"TRUE"/"1" all read as enabled.
AUTO_BOOTSTRAP="$(printf '%s' "${CLINVAR_LINK_AUTO_BOOTSTRAP:-false}" | tr '[:upper:]' '[:lower:]')"

if [ "${AUTO_BOOTSTRAP}" = "true" ] || [ "${AUTO_BOOTSTRAP}" = "1" ] || [ ! -f "${DB_PATH}" ]; then
    echo "[entrypoint] Ensuring the local ClinVar index is built/refreshed (db=${DB_PATH})..."
    if clinvar-link-data refresh; then
        echo "[entrypoint] ClinVar index ready."
    else
        echo "[entrypoint] WARN: build/refresh failed; the server will lazy-bootstrap on first use."
    fi
else
    echo "[entrypoint] Existing ClinVar index found at ${DB_PATH}; skipping refresh."
fi

# exec so the server is PID 1 and receives SIGTERM/SIGINT for graceful shutdown.
exec clinvar-link serve \
    --transport "${CLINVAR_LINK_MCP_TRANSPORT:-unified}" \
    --host "${CLINVAR_LINK_MCP_HOST:-0.0.0.0}" \
    --port "${CLINVAR_LINK_MCP_PORT:-8000}"
