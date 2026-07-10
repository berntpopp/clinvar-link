"""Configuration settings for the clinvar-link server.

clinvar-link is a LOCAL-data MCP server: instead of calling a remote API at
request time, it serves variant-pathogenicity answers from a bulk NCBI ClinVar
download indexed into a local SQLite database. The settings below therefore
describe where that data lives on disk (``DATA_DIR`` / ``DB_FILENAME``), where
to fetch it from (``SOURCE_URL``), and when it is considered stale
(``REFRESH_TTL_DAYS``) rather than upstream API endpoints/timeouts.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root is the parent of the package directory (``clinvar_link/``); the
# default data directory lives alongside the source tree at ``<repo_root>/data``.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DATA_DIR = _REPO_ROOT / "data"


@dataclass
class ServerConfig:
    """Server configuration with transport selection."""

    transport: Literal["unified", "http"] = "unified"
    host: str = "127.0.0.1"
    port: int = 8000
    mcp_path: str = "/mcp"
    allowed_hosts: list[str] = field(default_factory=lambda: ["localhost", "127.0.0.1", "::1"])
    allowed_origins: list[str] = field(default_factory=list)
    enable_docs: bool = True
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "ServerConfig":
        """Create configuration from environment variables."""
        return cls(
            transport=settings.MCP_TRANSPORT,
            host=settings.MCP_HOST,
            port=settings.MCP_PORT,
            mcp_path=settings.MCP_PATH,
            allowed_hosts=settings.MCP_ALLOWED_HOSTS,
            allowed_origins=settings.MCP_ALLOWED_ORIGINS,
            enable_docs=settings.ENABLE_SWAGGER,
            log_level=settings.LOG_LEVEL,
        )


class Settings(BaseSettings):
    """Application settings with local-data store and transport support."""

    # Local Data Store Configuration
    # Directory holding the bulk download and the indexed SQLite database.
    DATA_DIR: Path = _DEFAULT_DATA_DIR
    # SQLite filename inside DATA_DIR; combined into ``db_path``.
    DB_FILENAME: str = "clinvar.sqlite"
    # Bulk source: NCBI ClinVar tab-delimited variant_summary, gzip-compressed.
    SOURCE_URL: str = (
        "https://ftp.ncbi.nlm.nih.gov/pub/clinvar/tab_delimited/variant_summary.txt.gz"
    )
    # Age (days) after which the local index is considered stale and refreshable.
    REFRESH_TTL_DAYS: int = 7
    # Build the SQLite index on first start if it is absent (off by default; a
    # cron/CLI job owns the refresh in production).
    AUTO_BOOTSTRAP: bool = False
    # Index per-submitter conflict detail (submission_summary.txt.gz). Off in v1.
    ENABLE_SUBMISSION_SUMMARY: bool = False
    # Opt-in: also ingest hgvs4variation.txt.gz (ALL transcript-version HGVS
    # expressions, ~12 keys/variant). Robust but roughly doubles the DB to
    # ~8 GB; off by default so the shipped bundle stays lean.
    ENABLE_HGVS4VARIATION: bool = False
    # Secondary source: NCBI ClinVar all-HGVS-expressions table, gzip-compressed.
    HGVS4VARIATION_URL: str = (
        "https://ftp.ncbi.nlm.nih.gov/pub/clinvar/tab_delimited/hgvs4variation.txt.gz"
    )
    SOURCE_MAX_BYTES: int = Field(
        default=1 << 30,
        gt=0,
        description="Raw source limit; measured 414 MiB on 2026-07-10; override if needed.",
    )
    SOURCE_MAX_EXPANDED_BYTES: int = Field(
        default=8 << 30,
        gt=0,
        description="Expanded source limit; measured below 4 GiB on 2026-07-10; override if needed.",
    )

    # Prebuilt-bundle distribution. Instead of building the index locally, clients
    # can fetch a zstd-compressed SQLite snapshot published to GitHub Releases by
    # CI. ``GITHUB_REPO`` is the source of releases; ``BUNDLE_URL`` selects the
    # asset (``"latest"`` resolves the newest release asset, ``""`` disables the
    # bundle path, or a full ``.sqlite.zst`` URL pins a specific snapshot).
    GITHUB_REPO: str = "berntpopp/clinvar-link"
    BUNDLE_URL: str = "latest"
    # Fall back to a full local build when no prebuilt bundle is available.
    BUILD_LOCAL: bool = False
    # Stable asset name uploaded by CI for the prebuilt snapshot.
    BUNDLE_ASSET_NAME: str = "clinvar.sqlite.zst"
    # Staging directory for the downloaded ``.zst`` before decompression.
    BUNDLE_DOWNLOAD_DIR: Path = _DEFAULT_DATA_DIR
    BUNDLE_EXPECTED_SHA256: str | None = None
    BUNDLE_MAX_BYTES: int = Field(
        default=2 << 30,
        gt=0,
        description="Bundle limit; measured below 1 GiB on 2026-07-10; override if needed.",
    )
    BUNDLE_MAX_EXPANDED_BYTES: int = Field(
        default=8 << 30,
        gt=0,
        description="Expanded DB limit; measured below 4 GiB on 2026-07-10; override if needed.",
    )
    METADATA_MAX_BYTES: int = Field(
        default=1 << 20,
        gt=0,
        description="Release metadata limit; measured below 512 KiB on 2026-07-10; override if needed.",
    )
    MAX_DOWNLOAD_SECONDS: float = Field(
        default=3600.0,
        gt=0,
        description="Total download limit; measured under 1800 seconds on 2026-07-10; override if needed.",
    )

    # Cache Configuration
    CACHE_SIZE: int = 1024  # Maximum number of records to cache
    CACHE_TTL_MINUTES: int = 60  # Cache time-to-live in minutes

    # Transport Configuration
    MCP_TRANSPORT: Literal["unified", "http"] = "unified"
    MCP_HOST: str = "127.0.0.1"
    MCP_PORT: int = 8000
    MCP_PATH: str = "/mcp"
    MCP_ALLOWED_HOSTS: list[str] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "::1"],
        description="Exact Host header values accepted by the request guard.",
    )
    MCP_ALLOWED_ORIGINS: list[str] = Field(
        default_factory=list,
        description="Browser Origin values accepted by the request guard.",
    )

    # Logging Configuration
    LOG_LEVEL: str = "INFO"
    # Renderer selection for structlog: "json" in production, "console" in dev.
    LOG_FORMAT: str = "json"

    # Server Configuration
    CORS_ORIGINS: str = "*"  # Comma-separated list of allowed origins
    ENABLE_SWAGGER: bool = True
    ENABLE_MONITORING: bool = True

    # Production Configuration
    GRACEFUL_SHUTDOWN_TIMEOUT: int = 30
    MAX_PAGE_SIZE: int = 100

    model_config = SettingsConfigDict(
        env_prefix="CLINVAR_LINK_",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    @field_validator("CORS_ORIGINS")
    @classmethod
    def validate_cors_origins(cls, v: str) -> str:
        """Validate CORS origins format."""
        if not v or v == "*":
            return v
        # Basic validation for comma-separated origins
        origins = [origin.strip() for origin in v.split(",")]
        return ",".join(origins)

    @field_validator("MCP_PATH")
    @classmethod
    def validate_mcp_path(cls, v: str) -> str:
        """Ensure MCP path starts with /."""
        if not v.startswith("/"):
            return f"/{v}"
        return v

    @field_validator("MCP_ALLOWED_HOSTS")
    @classmethod
    def reject_wildcard_hosts(cls, v: list[str]) -> list[str]:
        """Require exact production Host values rather than wildcard patterns."""
        if any(any(marker in host for marker in "*?[]") for host in v):
            raise ValueError("wildcard patterns are not allowed in MCP_ALLOWED_HOSTS")
        return v

    @model_validator(mode="after")
    def _default_bundle_download_dir(self) -> "Settings":
        """Default the bundle staging dir to ``DATA_DIR`` unless one was given.

        ``BUNDLE_DOWNLOAD_DIR`` and ``DATA_DIR`` share the same default sentinel,
        so a caller that overrides only ``DATA_DIR`` still stages the ``.zst``
        alongside the database it is about to install.
        """
        if self.BUNDLE_DOWNLOAD_DIR == _DEFAULT_DATA_DIR:
            object.__setattr__(self, "BUNDLE_DOWNLOAD_DIR", self.DATA_DIR)
        return self

    @property
    def db_path(self) -> Path:
        """Absolute path to the SQLite index (``DATA_DIR / DB_FILENAME``)."""
        return self.DATA_DIR / self.DB_FILENAME

    @property
    def cors_origins_list(self) -> list[str]:
        """Get CORS origins as a list."""
        if self.CORS_ORIGINS == "*":
            return ["*"]
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def mcp_url(self) -> str:
        """Get the full MCP URL."""
        return f"http://{self.MCP_HOST}:{self.MCP_PORT}{self.MCP_PATH}"


# Global settings instance
settings = Settings()
