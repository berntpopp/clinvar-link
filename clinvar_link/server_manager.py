"""Unified server manager for ClinVar Link.

A thin FastAPI host exposes ``/health`` and mounts the MCP HTTP app at the
configured ``mcp_path`` (default ``/mcp``). Unlike a remote-API server, the
service is backed by a local read-only SQLite index built by
``clinvar-link-data build``; the repository is opened once in the FastAPI
lifespan, stored on ``app.state`` for the MCP service factory, and closed on
shutdown.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from typing import Any, cast

# fastmcp >=3.4.3 defaults http_host_origin_protection on, which returns 421
# Misdirected Request for any proxied /mcp request whose Host is not localhost
# (e.g. traffic from the genefoundry-router). NPM already validates the Host
# via server_name + TLS SNI, so disable the redundant app-layer guard. This is
# a no-op on fastmcp <3.4.3 (the setting does not exist yet), so it is safe to
# land before the version bump that would otherwise break federation.
import fastmcp
import structlog
import uvicorn
from asgi_correlation_id import CorrelationIdMiddleware
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastmcp import FastMCP

from clinvar_link import __version__
from clinvar_link.config import ServerConfig, settings
from clinvar_link.exceptions import ConfigurationError, MCPIntegrationError, StartupError
from clinvar_link.logging_config import configure_logging
from clinvar_link.mcp.facade import create_clinvar_mcp
from clinvar_link.services.clinvar_service import ClinVarService

if hasattr(fastmcp.settings, "http_host_origin_protection"):
    fastmcp.settings.http_host_origin_protection = False

_BUILD_HINT = "Build it with 'clinvar-link-data build' before starting the server."


class UnifiedServerManager:
    def __init__(self) -> None:
        self.app: FastAPI | None = None
        self.mcp: FastMCP | None = None
        self.shutdown_event = asyncio.Event()
        self.logger: Any = None
        self._current_transport = "unknown"

    # ---------------- service factory helpers ----------------

    def _create_service(self) -> ClinVarService:
        """Open the local SQLite index and wrap it in a service.

        If the database is missing, either build it on demand (when
        ``AUTO_BOOTSTRAP`` is on and a source file exists) or raise a
        :class:`StartupError` with the build hint.
        """
        from clinvar_link.data.repository import ClinVarRepository

        if not settings.db_path.exists():
            self._handle_missing_db()
        repo = ClinVarRepository(settings.db_path)
        return ClinVarService(repo)

    def _handle_missing_db(self) -> None:
        """React to a missing index: bootstrap if possible, else raise."""
        self.logger.error(
            "ClinVar database not found",
            db_path=str(settings.db_path),
            hint=_BUILD_HINT,
        )
        if not settings.AUTO_BOOTSTRAP:
            raise StartupError(f"ClinVar database not found at {settings.db_path}. {_BUILD_HINT}")
        source_path = settings.DATA_DIR / "variant_summary.txt.gz"
        if not source_path.exists():
            raise StartupError(
                f"AUTO_BOOTSTRAP is enabled but no source dump exists at "
                f"{source_path}. {_BUILD_HINT}"
            )
        self.logger.info("AUTO_BOOTSTRAP: building ClinVar index", source_path=str(source_path))
        from clinvar_link.ingest.builder import build_database

        build_database(settings, source_path=source_path)
        self.logger.info("AUTO_BOOTSTRAP: build complete", db_path=str(settings.db_path))

    # ---------------- FastAPI host (health only) ----------------

    async def _create_fastapi_app(self, config: ServerConfig) -> FastAPI:
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            self.logger.info("Starting ClinVar Link host application...")
            service = self._create_service()
            app.state.clinvar_service = service
            self.logger.info("Service ready", db_path=str(settings.db_path))
            try:
                yield
            finally:
                self.logger.info("Shutting down host application...")
                # Release the read-only SQLite connection cleanly.
                with suppress(Exception):  # best-effort teardown
                    service.repo.close()

        enable_docs = config.enable_docs and settings.ENABLE_SWAGGER
        app = FastAPI(
            title="ClinVar Link MCP Host",
            description="Thin FastAPI host that exposes /health and mounts the MCP HTTP app at /mcp.",
            version=__version__,
            lifespan=lifespan,
            docs_url="/docs" if enable_docs else None,
            redoc_url="/redoc" if enable_docs else None,
            openapi_url="/openapi.json" if enable_docs else None,
        )
        app.add_middleware(CorrelationIdMiddleware)
        cors_origins = settings.cors_origins_list
        # Never pair wildcard origins with credentials: browsers reject that
        # combination and it is a CORS anti-pattern (reflected-origin credential
        # exposure). Allow credentials only when an explicit allowlist is set.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=cors_origins != ["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @app.get("/health")
        async def health() -> dict[str, Any]:
            release_date: str | None = None
            service = getattr(app.state, "clinvar_service", None)
            if service is not None:
                with suppress(Exception):  # best-effort; never fail health on meta
                    meta = await service.get_clinvar_meta()
                    release_date = meta.get("release_date")
            return {
                "status": "healthy",
                "version": __version__,
                "transport": "streamable-http-stateless",
                "clinvar_release_date": release_date,
            }

        return app

    # ---------------- MCP creation ----------------

    def _create_mcp_server(self, service_factory: Callable[[], ClinVarService]) -> FastMCP:
        try:
            mcp = create_clinvar_mcp(service_factory=service_factory)
            self.logger.info("MCP facade created")
            return mcp
        except Exception as e:
            raise MCPIntegrationError(f"Failed to create MCP server: {e}", "mcp") from e

    @staticmethod
    def _compose_lifespan(app: FastAPI, mcp_app: Any) -> None:
        fastapi_lifespan = app.router.lifespan_context
        mcp_lifespan = mcp_app.lifespan

        @asynccontextmanager
        async def combined(parent_app: FastAPI):
            async with fastapi_lifespan(parent_app):
                async with mcp_lifespan(mcp_app):
                    yield

        app.router.lifespan_context = combined

    # ---------------- signal handlers ----------------

    def _setup_signal_handlers(self) -> None:
        def handler(signum, _frame) -> None:
            self.logger.info(f"Received signal {signum}; shutting down...")
            self.shutdown_event.set()

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    # ---------------- entry points ----------------

    async def start_unified_server(self, config: ServerConfig, *, dev: bool = False) -> None:
        try:
            self._current_transport = "unified"
            log_format = "console" if dev else settings.LOG_FORMAT
            configure_logging(config.log_level, log_format)
            self.logger = structlog.get_logger("clinvar_link")

            self.app = await self._create_fastapi_app(config)

            def service_factory() -> ClinVarService:
                if self.app is None:
                    raise RuntimeError("FastAPI host not initialized")
                return cast(ClinVarService, self.app.state.clinvar_service)

            self.mcp = self._create_mcp_server(service_factory)
            # Bake the MCP path ("/mcp") into the ASGI sub-app's own routes and
            # mount it at the project root. Mounting at "/mcp" instead would make
            # the streamable-http endpoint live at "/mcp/" and turn POST /mcp into
            # a 307 redirect; the rest of the fleet serves /mcp directly. The
            # FastAPI host's own routes (/health, /api/...) are registered before
            # this mount and therefore take precedence.
            mcp_http_app = self.mcp.http_app(
                path=config.mcp_path, stateless_http=True, json_response=True
            )
            self._compose_lifespan(self.app, mcp_http_app)
            self.app.mount("/", mcp_http_app)

            self.logger.info(f"MCP HTTP at http://{config.host}:{config.port}{config.mcp_path}")
            self.logger.info(f"Health at http://{config.host}:{config.port}/health")

            self._setup_signal_handlers()

            uvicorn_config = uvicorn.Config(
                app=self.app,
                host=config.host,
                port=config.port,
                log_level=config.log_level.lower(),
                access_log=True,
            )
            await uvicorn.Server(uvicorn_config).serve()
        except StartupError:
            raise
        except Exception as e:
            raise StartupError(f"Failed to start unified server: {e}", "unified") from e

    async def start_server(self, config: ServerConfig, *, dev: bool = False) -> None:
        if config.transport in {"unified", "http"}:
            await self.start_unified_server(config, dev=dev)
        else:
            raise ConfigurationError(f"Unknown transport: {config.transport}")
