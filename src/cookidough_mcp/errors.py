"""Domain exception hierarchy for the Cookidoo MCP server."""


class CookidooMcpError(Exception):
    """Base class for all errors raised by this server."""


class AuthenticationError(CookidooMcpError):
    """Raised when login (or a re-login after session expiry) fails."""


class NotFoundError(CookidooMcpError):
    """Raised when a requested resource does not exist."""


class UpstreamApiError(CookidooMcpError):
    """Raised when an upstream Cookidoo request fails for a transport reason."""


class QualityGateError(CookidooMcpError):
    """Raised when a custom recipe fails the configured quality bar."""

    def __init__(self, message: str, score: int, threshold: int) -> None:
        super().__init__(message)
        self.score = score
        self.threshold = threshold


class WebImportError(CookidooMcpError):
    """Raised when a remote recipe cannot be scraped or mapped."""
