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


class ToolInputError(ValueError):
    """Local pre-lookup validation failure.

    The message is developer-authored and contains no user-supplied values, so
    it is safe to surface verbatim in tool error envelopes.
    """

    pass
