"""Configuration and environment management for Git Plus."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

_MIB = 1024 * 1024


@dataclass(frozen=True)
class RunSettings:
    """Revision-pinned test execution; absent unless an isolated runner is explicitly configured."""

    runner_command: tuple[str, ...]
    policies_path: str
    data_dir: str
    max_queued: int = 5
    default_timeout: int = 300
    max_timeout: int = 600
    retention_hours: float = 48.0
    closed_grace_hours: float = 6.0
    max_source_bytes: int = 256 * _MIB
    max_overlay_bytes: int = 65536
    min_free_bytes: int = 1024 * _MIB

    @classmethod
    def load(cls) -> RunSettings | None:
        from git_plus.errors import ValidationError

        raw = os.environ.get("GIT_PLUS_TEST_RUNNER_COMMAND", "").strip()
        if not raw:
            return None
        try:
            command = json.loads(raw)
        except ValueError:
            command = None
        if not (isinstance(command, list) and command and all(isinstance(a, str) and a for a in command)):
            raise ValidationError("GIT_PLUS_TEST_RUNNER_COMMAND must be a JSON array of command arguments.")
        policies = os.environ.get("GIT_PLUS_TEST_POLICIES", "").strip()
        if not policies:
            raise ValidationError("GIT_PLUS_TEST_POLICIES must name the maintainer test policy file.")

        def number(name: str, default, low, high, cast=int):
            text = os.environ.get(f"GIT_PLUS_TEST_{name}", "").strip()
            try:
                value = cast(text) if text else default
            except ValueError:
                value = None
            if value is None or not low <= value <= high:
                raise ValidationError(f"GIT_PLUS_TEST_{name} must be between {low} and {high}.")
            return value

        max_timeout = number("MAX_TIMEOUT", 600, 10, 3600)
        state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
        return cls(
            runner_command=tuple(command), policies_path=policies,
            data_dir=os.environ.get("GIT_PLUS_TEST_DATA_DIR") or os.path.join(state, "git-plus", "tests"),
            max_queued=number("MAX_QUEUED", 5, 1, 50),
            default_timeout=number("DEFAULT_TIMEOUT", 300, 10, max_timeout), max_timeout=max_timeout,
            retention_hours=number("RETENTION_HOURS", 48.0, 0.1, 720.0, float),
            closed_grace_hours=number("CLOSED_GRACE_HOURS", 6.0, 0.1, 720.0, float),
            max_source_bytes=number("MAX_SOURCE_MIB", 256, 1, 4096) * _MIB,
            max_overlay_bytes=number("MAX_OVERLAY_BYTES", 65536, 1024, 1048576),
            min_free_bytes=number("MIN_FREE_MIB", 1024, 64, 1048576) * _MIB)


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
    quick_fix_max_files: int = 10
    quick_fix_max_changed_lines: int = 1000
    quick_fix_max_patch_bytes: int = 262144
    testing: RunSettings | None = None

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

        quick_fix_limits = []
        for name, default, ceiling in (
            ("GIT_PLUS_QUICK_FIX_MAX_FILES", 10, 50),
            ("GIT_PLUS_QUICK_FIX_MAX_CHANGED_LINES", 1000, 5000),
            ("GIT_PLUS_QUICK_FIX_MAX_PATCH_BYTES", 262144, 1048576),
        ):
            raw = os.environ.get(name, "").strip()
            try:
                value = int(raw) if raw else default
            except ValueError:
                value = 0
            if not 1 <= value <= ceiling:
                from git_plus.errors import ValidationError
                raise ValidationError(f"{name} must be an integer from 1 to {ceiling}.")
            quick_fix_limits.append(value)

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
            quick_fix_max_files=quick_fix_limits[0],
            quick_fix_max_changed_lines=quick_fix_limits[1],
            quick_fix_max_patch_bytes=quick_fix_limits[2],
            testing=RunSettings.load(),
        )

    def is_repo_allowed(self, repo: str) -> bool:
        """Check if a repository is permitted by allowed_repositories setting."""
        if not self.allowed_repositories:
            return True
        return repo.strip().casefold() in self.allowed_repositories
