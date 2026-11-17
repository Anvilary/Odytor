"""Error definitions for Git Plus."""

from __future__ import annotations

from typing import Any


class GitPlusError(Exception):
    """Base exception for all Git Plus errors."""

    def __init__(self, message: str, diagnostic: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.diagnostic = diagnostic or {}

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "error": self.message,
            "error_type": self.__class__.__name__,
        }
        if self.diagnostic:
            result["diagnostic"] = self.diagnostic
        return result


class APIError(GitPlusError):
    """Raised when GitHub REST or GraphQL API encounters an error."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        diagnostic: dict[str, Any] | None = None,
    ) -> None:
        diag = diagnostic or {}
        if status_code is not None:
            diag["status_code"] = status_code
        super().__init__(message, diag)
        self.status_code = status_code


class RevisionMismatchError(GitPlusError):
    """Raised when expected_head does not match the actual PR head SHA."""

    def __init__(
        self,
        expected: str,
        actual: str,
        message: str | None = None,
    ) -> None:
        msg = message or f"PR head revision mismatch: expected {expected}, found {actual}."
        super().__init__(msg, {"expected_head": expected, "actual_head": actual})
        self.expected = expected
        self.actual = actual


class ValidationError(GitPlusError):
    """Raised on invalid input parameters, malformed diffs, or invalid SHAs."""


class SecurityError(GitPlusError):
    """Raised when an operation violates security boundaries."""


class GitOperationError(GitPlusError):
    """Raised when an isolated local git operation fails."""
