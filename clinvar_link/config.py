"""Configuration settings for the clinvar-link server.

clinvar-link is a LOCAL-data MCP server: instead of calling a remote API at
request time, it serves variant-pathogenicity answers from a bulk NCBI ClinVar
download indexed into a local SQLite database. The settings below therefore
describe where that data lives on disk (``DATA_DIR`` / ``DB_FILENAME``), where
to fetch it from (``SOURCE_URL``), and when it is considered stale
(``REFRESH_TTL_DAYS``) rather than upstream API endpoints/timeouts.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import field_validator
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
    # Also ingest hgvs4variation.txt.gz to index ALL HGVS expressions per
    # variant (coding/protein forms), making HGVS lookups robust.
    ENABLE_HGVS4VARIATION: bool = True
    # Secondary source: NCBI ClinVar all-HGVS-expressions table, gzip-compressed.
    HGVS4VARIATION_URL: str = (
        "https://ftp.ncbi.nlm.nih.gov/pub/clinvar/tab_delimited/hgvs4variation.txt.gz"
    )

    # Cache Configuration
    CACHE_SIZE: int = 1024  # Maximum number of records to cache
    CACHE_TTL_MINUTES: int = 60  # Cache time-to-live in minutes

    # Transport Configuration
    MCP_TRANSPORT: Literal["unified", "http"] = "unified"
    MCP_HOST: str = "127.0.0.1"
    MCP_PORT: int = 8000
    MCP_PATH: str = "/mcp"

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
