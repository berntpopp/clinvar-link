"""Command line interface for clinvar-link (GeneFoundry CLI Standard v1).

A single ``typer`` application exposing ``serve``, ``config``, ``health``, and
``version``. The console script ``clinvar-link`` resolves to :data:`app`. The
server is local-data backed (SQLite index built by ``clinvar-link-data``);
there is no stdio transport here (use ``clinvar-link-mcp`` for stdio). Streamable
HTTP only.
"""

from __future__ import annotations

import asyncio

import httpx
import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import ServerConfig, settings

app = typer.Typer(
    name="clinvar-link",
    add_completion=False,
    no_args_is_help=True,
    help="clinvar-link — MCP server grounding variant-pathogenicity questions in NCBI ClinVar.",
)

console = Console()

TransportOption = typer.Option("unified", "--transport", help="Transport mode (unified or http).")


@app.command()
def serve(
    transport: str = TransportOption,
    host: str = typer.Option(settings.MCP_HOST, "--host", help="Host to bind to."),
    port: int = typer.Option(settings.MCP_PORT, "--port", help="Port to bind to."),
    mcp_path: str = typer.Option("/mcp", "--mcp-path", help="MCP endpoint path."),
    log_level: str = typer.Option("INFO", "--log-level", help="Log level."),
    disable_docs: bool = typer.Option(False, "--disable-docs", help="Disable API docs."),
    dev: bool = typer.Option(False, "--dev", help="Development mode (console logs)."),
) -> None:
    """Start the unified FastAPI host (/health) with the MCP HTTP app at /mcp."""
    if transport not in {"unified", "http"}:
        console.print(f"[red]Invalid transport {transport!r}; choose 'unified' or 'http'.[/red]")
        raise typer.Exit(code=2)
    if not mcp_path.startswith("/"):
        console.print("[red]MCP path must start with '/'.[/red]")
        raise typer.Exit(code=2)

    config = ServerConfig(
        transport="unified" if transport == "unified" else "http",
        host=host,
        port=port,
        mcp_path=mcp_path,
        allowed_hosts=settings.MCP_ALLOWED_HOSTS,
        allowed_origins=settings.MCP_ALLOWED_ORIGINS,
        enable_docs=not disable_docs,
        log_level=log_level,
    )

    from .server_manager import UnifiedServerManager

    manager = UnifiedServerManager()
    try:
        asyncio.run(manager.start_server(config, dev=dev))
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutdown requested by user[/yellow]")
        raise typer.Exit(code=0) from None


@app.command()
def config(
    validate: bool = typer.Option(False, "--validate", help="Validate configuration."),
) -> None:
    """Show (and optionally validate) the resolved configuration."""
    cfg = ServerConfig.from_env()

    table = Table(title="clinvar-link configuration")
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("db_path", str(settings.db_path))
    table.add_row("data_dir", str(settings.DATA_DIR))
    table.add_row("source_url", settings.SOURCE_URL)
    table.add_row("transport", cfg.transport)
    table.add_row("host", cfg.host)
    table.add_row("port", str(cfg.port))
    table.add_row("mcp_path", cfg.mcp_path)
    table.add_row("enable_docs", str(cfg.enable_docs))
    table.add_row("log_level", cfg.log_level)
    table.add_row("log_format", settings.LOG_FORMAT)
    table.add_row("cors_origins", settings.CORS_ORIGINS)
    console.print(table)

    if validate:
        if cfg.port < 1 or cfg.port > 65535:
            console.print("[red]Invalid port number[/red]")
            raise typer.Exit(code=1)
        if not cfg.mcp_path.startswith("/"):
            console.print("[red]MCP path must start with '/'[/red]")
            raise typer.Exit(code=1)
        console.print("[green]Configuration is valid[/green]")


@app.command()
def health(
    url: str = typer.Option("http://127.0.0.1:8000", "--url", help="Server base URL to check."),
) -> None:
    """Check the running server's /health endpoint."""
    try:
        response = httpx.get(f"{url}/health", timeout=5)
    except httpx.HTTPError as exc:
        console.print(f"[red]Failed to connect to server: {exc}[/red]")
        raise typer.Exit(code=1) from exc
    if response.status_code != 200:
        console.print(f"[red]Server returned status {response.status_code}[/red]")
        raise typer.Exit(code=1)
    data = response.json()
    console.print("[green]Server is healthy[/green]")
    console.print(f"Status: {data.get('status', 'unknown')}")
    console.print(f"Transport: {data.get('transport', 'unknown')}")
    console.print(f"ClinVar release date: {data.get('clinvar_release_date', 'unknown')}")


@app.command()
def version() -> None:
    """Print the clinvar-link version."""
    console.print(f"clinvar-link {__version__}")


if __name__ == "__main__":
    app()
