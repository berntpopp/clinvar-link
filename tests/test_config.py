"""Tests for clinvar-link configuration (config.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from clinvar_link.config import ServerConfig, Settings, settings


def test_settings_loads_with_defaults() -> None:
    """A bare Settings() constructs with the documented defaults."""
    config = Settings()
    assert config.DB_FILENAME == "clinvar.sqlite"
    assert isinstance(config.DATA_DIR, Path)
    assert config.AUTO_BOOTSTRAP is False
    assert config.ENABLE_SUBMISSION_SUMMARY is False
    # hgvs4variation is opt-in (default off) so the shipped bundle stays lean.
    assert config.ENABLE_HGVS4VARIATION is False
    assert config.CACHE_SIZE == 1024
    assert config.CACHE_TTL_MINUTES == 60
    assert config.LOG_FORMAT == "json"
    assert config.CORS_ORIGINS == "*"
    assert config.SOURCE_URL.endswith("variant_summary.txt.gz")


def test_db_path_joins_data_dir_and_filename() -> None:
    """db_path is DATA_DIR / DB_FILENAME on the module singleton."""
    assert settings.db_path == settings.DATA_DIR / settings.DB_FILENAME


def test_db_path_on_fresh_instance() -> None:
    """db_path composes correctly on a freshly-constructed instance too."""
    config = Settings()
    assert config.db_path == config.DATA_DIR / config.DB_FILENAME


def test_refresh_ttl_days_default() -> None:
    """REFRESH_TTL_DAYS defaults to 7."""
    assert settings.REFRESH_TTL_DAYS == 7


def test_env_override_db_filename(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLINVAR_LINK_ env prefix overrides DB_FILENAME for a fresh Settings()."""
    monkeypatch.setenv("CLINVAR_LINK_DB_FILENAME", "x.sqlite")
    config = Settings()
    assert config.DB_FILENAME == "x.sqlite"
    assert config.db_path == config.DATA_DIR / "x.sqlite"


def test_env_override_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """DATA_DIR is overridable via the env prefix and flows into db_path."""
    monkeypatch.setenv("CLINVAR_LINK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CLINVAR_LINK_DB_FILENAME", "clinvar.sqlite")
    config = Settings()
    assert tmp_path == config.DATA_DIR
    assert config.db_path == tmp_path / "clinvar.sqlite"


def test_cors_origins_list_wildcard() -> None:
    """cors_origins_list returns ['*'] for the wildcard default."""
    assert Settings().cors_origins_list == ["*"]


def test_cors_origins_list_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    """cors_origins_list splits comma-separated origins."""
    monkeypatch.setenv("CLINVAR_LINK_CORS_ORIGINS", "https://a.test, https://b.test")
    config = Settings()
    assert config.cors_origins_list == ["https://a.test", "https://b.test"]


def test_server_config_from_env_has_valid_port() -> None:
    """ServerConfig.from_env() returns a config with a usable port."""
    config = ServerConfig.from_env()
    assert isinstance(config, ServerConfig)
    assert isinstance(config.port, int)
    assert 1 <= config.port <= 65535
    assert config.mcp_path.startswith("/")
