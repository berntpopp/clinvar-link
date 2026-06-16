"""Tests for the typer CLI and the stdio MCP entrypoint."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from clinvar_link import __version__
from clinvar_link.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_config() -> None:
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0


def test_config_validate() -> None:
    result = runner.invoke(app, ["config", "--validate"])
    assert result.exit_code == 0


def test_serve_rejects_bad_transport() -> None:
    result = runner.invoke(app, ["serve", "--transport", "bogus"])
    assert result.exit_code == 2


def test_serve_rejects_bad_mcp_path() -> None:
    result = runner.invoke(app, ["serve", "--mcp-path", "mcp"])
    assert result.exit_code == 2


def test_build_mcp(built_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``mcp_server._build_mcp`` builds a server against the fixture DB."""
    import mcp_server
    from clinvar_link.config import Settings

    fixture_settings = Settings(DATA_DIR=built_db.parent, DB_FILENAME=built_db.name)
    monkeypatch.setattr(mcp_server, "settings", fixture_settings)

    mcp = mcp_server._build_mcp()
    assert mcp is not None
