"""Configuration and environment management for Git Plus."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    """Runtime configuration settings."""

    github_token: str | None = field(repr=False)
    github_api_url: str
    auth_token: str | None = field(repr=False)
    default_max_diff_lines: int
    default_max_diff_bytes: int
    allowed_repositories: tuple[str, ...]
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = field(default=None, repr=False)
    public_base_url: str | None = None
    allowed_github_users: tuple[str, ...] = ()
    oauth_signing_key: str | None = field(default=None, repr=False)
    odysseus_pat: str | None = field(default=None, repr=False)
    odysseus_pat_login: str | None = None

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

        if not 1 <= max_lines <= 5000 or not 1 <= max_bytes <= 8 * 1024 * 1024:
            from git_plus.errors import ValidationError
            raise ValidationError("Diff limits must be positive, at most 5000 lines and 8 MiB.")

        raw_allowed = os.environ.get("GIT_PLUS_ALLOWED_REPOSITORIES", "").strip()
        allowed = tuple(r.strip().casefold() for r in raw_allowed.split(",") if r.strip())
        raw_users = os.environ.get("GIT_PLUS_ALLOWED_GITHUB_USERS", "").strip()
        users = tuple(u.strip().casefold() for u in raw_users.split(",") if u.strip())

        return cls(
            github_token=token,
            github_api_url=api_url,
            auth_token=auth_token,
            default_max_diff_lines=max_lines,
            default_max_diff_bytes=max_bytes,
            allowed_repositories=allowed,
            oauth_client_id=os.environ.get("GIT_PLUS_GITHUB_CLIENT_ID"),
            oauth_client_secret=os.environ.get("GIT_PLUS_GITHUB_CLIENT_SECRET"),
            public_base_url=os.environ.get("GIT_PLUS_PUBLIC_URL"),
            allowed_github_users=users,
            oauth_signing_key=os.environ.get("GIT_PLUS_OAUTH_SIGNING_KEY"),
            odysseus_pat=os.environ.get("GIT_PLUS_ODYSSEUS_PAT"),
            odysseus_pat_login=os.environ.get("GIT_PLUS_ODYSSEUS_PAT_LOGIN"),
        )

    def is_repo_allowed(self, repo: str) -> bool:
        """Check if a repository is permitted by allowed_repositories setting."""
        if not self.allowed_repositories:
            return True
        return repo.strip().casefold() in self.allowed_repositories
