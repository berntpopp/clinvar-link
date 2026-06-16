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
from clinvar_link.ingest.bundle import pack_bundle, pull_latest
from clinvar_link.ingest.downloader import download_source
from clinvar_link.ingest.lock import build_lock

app = typer.Typer(
    add_completion=False,
    help="Build and refresh the local ClinVar SQLite index from variant_summary.txt.gz.",
)
console = Console()

_SOURCE_FILENAME = "variant_summary.txt.gz"
_HGVS_SOURCE_FILENAME = "hgvs4variation.txt.gz"
_CACHE_FILENAME = "download_cache.json"


def _source_path() -> Path:
    return settings.DATA_DIR / _SOURCE_FILENAME


def _hgvs_source_path() -> Path:
    return settings.DATA_DIR / _HGVS_SOURCE_FILENAME


def _cache_path() -> Path:
    return settings.DATA_DIR / _CACHE_FILENAME


def _maybe_download_hgvs(*, force: bool) -> Path | None:
    """Download the optional hgvs4variation source; never fail the main build.

    Returns the local path on success, or ``None`` (logging a warning) when the
    feature is disabled or the secondary download fails for any reason.
    """
    if not settings.ENABLE_HGVS4VARIATION:
        return None
    try:
        download_source(
            settings.HGVS4VARIATION_URL,
            _hgvs_source_path(),
            cache_path=_cache_path(),
            force=force,
        )
    except (DownloadError, ClinVarServerError, OSError) as exc:
        console.print(
            f"[yellow]WARNING:[/yellow] hgvs4variation download failed, building without it: {exc}"
        )
        return None
    return _hgvs_source_path()


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


def _build_local(*, force: bool) -> dict[str, Any]:
    """Download the source(s) and run a full local build, returning the summary."""
    with build_lock(settings.DATA_DIR):
        download = download_source(
            settings.SOURCE_URL,
            _source_path(),
            cache_path=_cache_path(),
            force=force,
        )
        hgvs_path = _maybe_download_hgvs(force=force)
        return build_database(
            settings,
            source_path=_source_path(),
            hgvs_source_path=hgvs_path,
            etag=download.get("etag"),
            last_modified=download.get("last_modified"),
            source_sha256=download.get("sha256"),
        )


@app.command()
def build() -> None:
    """Force a download and full rebuild of the database."""
    try:
        summary = _build_local(force=True)
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
            hgvs_path = _maybe_download_hgvs(force=False)
            summary = build_database(
                settings,
                source_path=_source_path(),
                hgvs_source_path=hgvs_path,
                etag=download.get("etag"),
                last_modified=download.get("last_modified"),
                source_sha256=download.get("sha256"),
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


def _print_pull_summary(result: dict[str, Any], *, header: str) -> None:
    """Render a bundle-pull summary dict as a rich table."""
    table = Table(title=header, show_header=False)
    table.add_column("field", style="bold cyan")
    table.add_column("value")
    table.add_row("release_tag", str(result.get("release_tag")))
    table.add_row("bytes_compressed", str(result.get("bytes_compressed")))
    table.add_row("bytes_db", str(result.get("bytes_db")))
    table.add_row("db_path", str(result.get("db_path")))
    console.print(table)


def _has_valid_index() -> bool:
    """True if a database with a populated ``meta`` row exists at ``db_path``."""
    try:
        meta = _read_meta(settings.db_path)
    except sqlite3.Error:
        return False
    return meta is not None


@app.command()
def pull() -> None:
    """Download and install the latest prebuilt SQLite snapshot from GitHub."""
    try:
        result = pull_latest(settings)
    except (DownloadError, ClinVarServerError, OSError, sqlite3.Error) as exc:
        console.print(f"[red]ERROR:[/red] pull failed: {exc}")
        raise typer.Exit(1) from exc
    _print_pull_summary(result, header="Installed ClinVar bundle")


@app.command()
def bootstrap() -> None:
    """Ensure a usable index exists: reuse it, pull a bundle, or build locally.

    Precedence (the container entrypoint contract):

    1. A valid local index is present -> reuse it.
    2. ``BUNDLE_URL`` is set -> pull + install the prebuilt snapshot.
    3. ``BUILD_LOCAL`` is true -> download source(s) and build locally.
    4. Otherwise -> error with a hint.

    If the bundle pull (step 2) fails and ``BUILD_LOCAL`` is true, it falls
    through to step 3 (the pull failure is logged as a warning).
    """
    if settings.db_path.exists() and _has_valid_index():
        console.print(f"[green]Index present at {settings.db_path}; skipping.[/green]")
        return

    if settings.BUNDLE_URL.strip():
        try:
            result = pull_latest(settings)
        except (DownloadError, ClinVarServerError, OSError, sqlite3.Error) as exc:
            if settings.BUILD_LOCAL:
                console.print(
                    f"[yellow]WARNING:[/yellow] bundle pull failed, "
                    f"falling back to a local build: {exc}"
                )
            else:
                console.print(f"[red]ERROR:[/red] bundle pull failed: {exc}")
                raise typer.Exit(1) from exc
        else:
            _print_pull_summary(result, header="Bootstrapped from ClinVar bundle")
            return

    if settings.BUILD_LOCAL:
        try:
            summary = _build_local(force=False)
        except (DownloadError, ClinVarServerError, OSError, sqlite3.Error) as exc:
            console.print(f"[red]ERROR:[/red] local build failed: {exc}")
            raise typer.Exit(1) from exc
        _print_summary(summary, header="Bootstrapped via local build")
        return

    console.print(
        "[red]ERROR:[/red] no local index; set BUNDLE_URL to pull a prebuilt "
        "snapshot or run 'clinvar-link-data build'."
    )
    raise typer.Exit(1)


@app.command()
def pack(
    out_dir: Path | None = typer.Option(
        None,
        "--out-dir",
        help="Directory to write clinvar.sqlite.zst (+ .sha256). Defaults to DATA_DIR.",
    ),
) -> None:
    """Pack the local database into a compressed release asset (CI producer)."""
    if not settings.db_path.exists():
        console.print(
            f"[red]ERROR:[/red] no database at {settings.db_path}; "
            "build it first with 'clinvar-link-data build'."
        )
        raise typer.Exit(1)

    target = out_dir if out_dir is not None else settings.DATA_DIR
    try:
        result = pack_bundle(settings.db_path, target)
    except (DownloadError, OSError) as exc:
        console.print(f"[red]ERROR:[/red] pack failed: {exc}")
        raise typer.Exit(1) from exc

    table = Table(title="Packed ClinVar bundle", show_header=False)
    table.add_column("field", style="bold cyan")
    table.add_column("value")
    table.add_row("release_tag", str(result.get("release_tag")))
    table.add_row("sha256", str(result.get("sha256")))
    table.add_row("size_bytes", str(result.get("size_bytes")))
    table.add_row("zst_path", str(result.get("zst_path")))
    console.print(table)


def main() -> None:
    """Console-script entry point for ``clinvar-link-data``."""
    app()


if __name__ == "__main__":
    main()
