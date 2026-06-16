"""Command-line interface for building and refreshing the ClinVar index.

Exposed as the ``clinvar-link-data`` console script and intended as the cron
entry point. Commands: ``build`` (force a download + rebuild), ``refresh``
(conditional rebuild — the cron job), and ``status`` (print provenance of the
existing DB).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from clinvar_link.config import settings
from clinvar_link.exceptions import ClinVarServerError, DownloadError
from clinvar_link.ingest.builder import build_database
from clinvar_link.ingest.downloader import download_source
from clinvar_link.ingest.lock import build_lock

app = typer.Typer(
    add_completion=False,
    help="Build and refresh the local ClinVar SQLite index from variant_summary.txt.gz.",
)
console = Console()

_SOURCE_FILENAME = "variant_summary.txt.gz"
_CACHE_FILENAME = "download_cache.json"


def _source_path() -> Path:
    return settings.DATA_DIR / _SOURCE_FILENAME


def _cache_path() -> Path:
    return settings.DATA_DIR / _CACHE_FILENAME


def _print_summary(summary: dict[str, Any], *, header: str) -> None:
    """Render a build summary dict as a rich table."""
    table = Table(title=header, show_header=False)
    table.add_column("field", style="bold cyan")
    table.add_column("value")
    table.add_row("variant_count", str(summary.get("variant_count")))
    table.add_row("gene_count", str(summary.get("gene_count")))
    table.add_row("clinvar_release_date", str(summary.get("clinvar_release_date")))
    table.add_row("db_path", str(summary.get("db_path")))
    console.print(table)


def _read_meta(db_path: Path) -> sqlite3.Row | None:
    """Read the single ``meta`` row from an existing DB (read-only), or None."""
    if not db_path.exists():
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        row: sqlite3.Row | None = conn.execute("SELECT * FROM meta WHERE id = 1").fetchone()
        return row
    finally:
        conn.close()


def _is_fresh(meta: sqlite3.Row | None) -> bool:
    """True if ``meta.build_utc`` is younger than the configured refresh TTL."""
    if meta is None:
        return False
    build_utc = meta["build_utc"]
    if not build_utc:
        return False
    try:
        built = datetime.fromisoformat(build_utc)
    except ValueError:
        return False
    if built.tzinfo is None:
        built = built.replace(tzinfo=UTC)
    age = datetime.now(tz=UTC) - built
    return age < timedelta(days=settings.REFRESH_TTL_DAYS)


@app.command()
def build() -> None:
    """Force a download and full rebuild of the database."""
    try:
        with build_lock(settings.DATA_DIR):
            download = download_source(
                settings.SOURCE_URL,
                _source_path(),
                cache_path=_cache_path(),
                force=True,
            )
            summary = build_database(
                settings,
                source_path=_source_path(),
                etag=download.get("etag"),
                last_modified=download.get("last_modified"),
            )
    except (DownloadError, ClinVarServerError, OSError, sqlite3.Error) as exc:
        console.print(f"[red]ERROR:[/red] build failed: {exc}")
        raise typer.Exit(1) from exc
    _print_summary(summary, header="Built ClinVar database")


@app.command()
def refresh() -> None:
    """Conditionally refresh the database; rebuild only if the dump changed."""
    try:
        meta = _read_meta(settings.db_path)
        if settings.db_path.exists() and _is_fresh(meta):
            download = download_source(
                settings.SOURCE_URL,
                _source_path(),
                cache_path=_cache_path(),
                force=False,
            )
            if download.get("status") == "not_modified":
                console.print("[green]Index is fresh, skipping rebuild.[/green]")
                return
        else:
            download = download_source(
                settings.SOURCE_URL,
                _source_path(),
                cache_path=_cache_path(),
                force=False,
            )

        with build_lock(settings.DATA_DIR):
            summary = build_database(
                settings,
                source_path=_source_path(),
                etag=download.get("etag"),
                last_modified=download.get("last_modified"),
            )
    except (DownloadError, ClinVarServerError, OSError, sqlite3.Error) as exc:
        console.print(f"[red]ERROR:[/red] refresh failed: {exc}")
        raise typer.Exit(1) from exc
    _print_summary(summary, header="Refreshed ClinVar database")


@app.command()
def status() -> None:
    """Print provenance of the existing database, or a hint to build it."""
    try:
        meta = _read_meta(settings.db_path)
    except sqlite3.Error as exc:
        console.print(f"[red]ERROR:[/red] could not read database: {exc}")
        raise typer.Exit(1) from exc

    if meta is None:
        console.print(f"[yellow]No ClinVar database at {settings.db_path}.[/yellow]")
        console.print("Run [bold]clinvar-link-data build[/bold] to download and build it.")
        return

    table = Table(title=f"ClinVar database at {settings.db_path}", show_header=False)
    table.add_column("field", style="bold cyan")
    table.add_column("value")
    for key in meta.keys():  # noqa: SIM118 - sqlite3.Row.keys(), iteration yields values
        table.add_row(key, str(meta[key]))
    console.print(table)


def main() -> None:
    """Console-script entry point for ``clinvar-link-data``."""
    app()


if __name__ == "__main__":
    main()
