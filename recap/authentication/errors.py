class AuthenticationError(Exception):
    """Base error for failed authentication."""


class InvalidCredentialsError(AuthenticationError):
    """Raised when supplied credentials cannot be authenticated."""
