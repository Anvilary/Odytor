"""Configuration and environment management for Git Plus."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Runtime configuration settings."""

    github_token: str | None
    github_api_url: str
    auth_token: str | None
    default_max_diff_lines: int
    default_max_diff_bytes: int
    allowed_repositories: tuple[str, ...]

    @classmethod
    def load(cls) -> Settings:
        """Load settings from environment variables."""
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        auth_token = os.environ.get("GIT_PLUS_AUTH_TOKEN")

        try:
            max_lines = int(os.environ.get("GIT_PLUS_DEFAULT_MAX_DIFF_LINES", "500"))
        except ValueError:
            max_lines = 500

        try:
            max_bytes = int(os.environ.get("GIT_PLUS_DEFAULT_MAX_DIFF_BYTES", "1048576"))
        except ValueError:
            max_bytes = 1048576

        raw_allowed = os.environ.get("GIT_PLUS_ALLOWED_REPOSITORIES", "").strip()
        allowed = tuple(r.strip().casefold() for r in raw_allowed.split(",") if r.strip())

        return cls(
            github_token=token,
            github_api_url=api_url,
            auth_token=auth_token,
            default_max_diff_lines=max_lines,
            default_max_diff_bytes=max_bytes,
            allowed_repositories=allowed,
        )

    def is_repo_allowed(self, repo: str) -> bool:
        """Check if a repository is permitted by allowed_repositories setting."""
        if not self.allowed_repositories:
            return True
        return repo.strip().casefold() in self.allowed_repositories
