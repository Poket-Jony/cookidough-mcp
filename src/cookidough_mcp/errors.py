"""Domain exception hierarchy for the cookidough MCP server."""


class CookidoughMcpError(Exception):
    """Base class for all errors raised by this server."""


class AuthenticationError(CookidoughMcpError):
    """Raised when login (or a re-login after session expiry) fails."""


class NotFoundError(CookidoughMcpError):
    """Raised when a requested resource does not exist."""


class UpstreamApiError(CookidoughMcpError):
    """Raised when an upstream request fails for a transport reason."""


class SessionClosedError(UpstreamApiError):
    """Raised when a call reaches a session that has already been closed."""


class QualityGateError(CookidoughMcpError):
    """Raised when a custom recipe fails the configured quality bar."""

    def __init__(self, message: str, score: int, threshold: int) -> None:
        super().__init__(message)
        self.score = score
        self.threshold = threshold


class WebImportError(CookidoughMcpError):
    """Raised when a remote recipe cannot be scraped or mapped."""
