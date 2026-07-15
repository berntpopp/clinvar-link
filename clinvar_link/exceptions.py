"""Custom exceptions for the clinvar-link server."""


class ClinVarServerError(Exception):
    """Base exception for clinvar-link server errors."""

    def __init__(self, message: str, transport: str | None = None):
        """Initialize ClinVar server error with message and optional transport context."""
        super().__init__(message)
        self.transport = transport


class TransportError(ClinVarServerError):
    """Exception for transport-related errors."""

    pass


class ConfigurationError(ClinVarServerError):
    """Exception for configuration validation errors."""

    pass


class StartupError(ClinVarServerError):
    """Exception for server startup errors."""

    pass


class ShutdownError(ClinVarServerError):
    """Exception for server shutdown errors."""

    pass


class MCPIntegrationError(TransportError):
    """Exception for MCP integration errors."""

    pass


class HTTPTransportError(TransportError):
    """Exception for HTTP transport errors."""

    pass


class STDIOTransportError(TransportError):
    """Exception for STDIO transport errors."""

    pass


# --- Data-layer exceptions (used by the MCP error envelope) ---


class DataNotFoundError(ClinVarServerError):
    """Raised when a requested record was not found in the local index."""

    pass


class ClinVarDataError(ClinVarServerError):
    """Generic data/index error (corrupt DB, missing schema, query failure)."""

    pass


class DataUnavailableError(ClinVarServerError):
    """Raised when the local index is missing and cannot be built on demand."""

    pass


class DownloadError(ClinVarServerError):
    """Raised when fetching the ClinVar bulk dump fails.

    Carries the upstream HTTP status code when one is available so callers can
    distinguish transport errors from server-side failures.
    """

    def __init__(self, message: str, status_code: int | None = None):
        """Initialize with a message and optional HTTP status code."""
        super().__init__(message)
        self.status_code = status_code


class ToolInputError(ValueError):
    """Local pre-lookup validation failure.

    The message is server-side diagnostic detail only. Several call sites
    interpolate the caller's input (``got {value!r}``), so it is NEVER surfaced
    verbatim: the MCP error boundary emits a FIXED, error-code-specific public
    message (see ``mcp/errors.py``) and keeps this text in the exception chain.

    ``field`` and ``public_reason`` are the SURFACEABLE half, and both are
    server-authored constants: ``field`` is a declared parameter name and
    ``public_reason`` describes the accepted vocabulary or bound (e.g. "must be one of:
    pathogenic, likely_pathogenic, …"). They exist because an error a model cannot act on is a
    defect — "The request was rejected as invalid." names nothing, and naming the wrong
    parameter is worse. Call sites MUST NOT interpolate the caller's rejected VALUE into
    ``public_reason``; that is what ``message`` (server-side only) is for.
    """

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        public_reason: str | None = None,
    ) -> None:
        """Initialize with server-side detail plus the surfaceable field + reason."""
        super().__init__(message)
        self.field = field
        self.public_reason = public_reason
