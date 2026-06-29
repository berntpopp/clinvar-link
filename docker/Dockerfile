# syntax=docker/dockerfile:1
# Multi-stage Dockerfile for clinvar-link.
#
# Unlike a remote-API server, clinvar-link serves answers from a LOCAL SQLite
# index built from the NCBI ClinVar weekly bulk download. The image ships no
# data: on first boot the entrypoint runs `clinvar-link-data bootstrap`, which
# downloads the latest prebuilt SQLite bundle from GitHub Releases, verifies it,
# and atomically installs it into the /app/data volume (or, with BUILD_LOCAL,
# builds from source instead). It then serves the unified FastAPI host (/health)
# + MCP streamable-HTTP app (/mcp) on one port.

FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV VIRTUAL_ENV="/opt/venv" \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /home/app/web
COPY uv.lock pyproject.toml README.md ./

RUN pip install --upgrade pip uv && \
    uv sync --frozen --no-dev --active --no-install-project


FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf AS production

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/home/app/web" \
    CLINVAR_LINK_MCP_TRANSPORT=unified \
    CLINVAR_LINK_MCP_HOST=0.0.0.0 \
    CLINVAR_LINK_MCP_PORT=8000 \
    CLINVAR_LINK_MCP_PATH=/mcp \
    CLINVAR_LINK_DATA_DIR=/app/data \
    CLINVAR_LINK_DB_FILENAME=clinvar.sqlite \
    # Bundle mode (the default): on first boot the entrypoint runs
    # `clinvar-link-data bootstrap`, which downloads the latest prebuilt SQLite
    # snapshot from GitHub Releases (BUNDLE_URL=latest of GITHUB_REPO), verifies
    # its sha256, decompresses, and atomically installs it — far faster than a
    # local build. Set CLINVAR_LINK_BUILD_LOCAL=true to build from the NCBI bulk
    # dump instead (heavy; see docker/README.md).
    CLINVAR_LINK_BUNDLE_URL=latest \
    CLINVAR_LINK_GITHUB_REPO=berntpopp/clinvar-link \
    CLINVAR_LINK_BUILD_LOCAL=false \
    CLINVAR_LINK_BUNDLE_DOWNLOAD_DIR=/app/data \
    CLINVAR_LINK_LOG_FORMAT=json \
    TMPDIR=/tmp/clinvar-link

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

RUN groupadd --system app && \
    useradd --system --gid app --home /home/app --create-home app && \
    mkdir -p /tmp/clinvar-link /app/data && \
    chown -R app:app /tmp/clinvar-link /app/data /home/app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /home/app/web

COPY --chown=app:app ./clinvar_link ./clinvar_link
COPY --chown=app:app ./mcp_server.py ./pyproject.toml ./README.md ./
COPY --chown=app:app ./docker/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN /opt/venv/bin/pip install . --no-deps && chmod +x /usr/local/bin/entrypoint.sh

USER app

EXPOSE 8000

# The installed SQLite index lives under CLINVAR_LINK_DATA_DIR (/app/data).
# Mount a volume there so the index persists across container restarts and the
# first-boot bundle download happens only once.
VOLUME ["/app/data"]

# start-period covers the first-boot bundle download: pulling the prebuilt
# clinvar.sqlite.zst (~hundreds of MB) and decompressing it is far faster than a
# ~9 GB local build, so 5 minutes is ample before /health turns healthy.
HEALTHCHECK --interval=30s --timeout=10s --retries=5 --start-period=300s \
    CMD curl -f http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
