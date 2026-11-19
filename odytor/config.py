"""Configuration and environment management for Odytor."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from odytor.envvars import EnvConflictError, default_state_dir, lookup

_MIB = 1024 * 1024


def _env(suffix: str) -> str | None:
    """ODYTOR_<suffix> with the temporary ODYTOR_<suffix> fallback; an ambiguous pair fails closed."""
    from odytor.errors import ValidationError
    try:
        return lookup(suffix)
    except EnvConflictError as exc:
        raise ValidationError(str(exc)) from exc


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
        from odytor.errors import ValidationError

        raw = (_env("TEST_RUNNER_COMMAND") or "").strip()
        if not raw:
            return None
        try:
            command = json.loads(raw)
        except ValueError:
            command = None
        if not (isinstance(command, list) and command and all(isinstance(a, str) and a for a in command)):
            raise ValidationError("ODYTOR_TEST_RUNNER_COMMAND must be a JSON array of command arguments.")
        policies = (_env("TEST_POLICIES") or "").strip()
        if not policies:
            raise ValidationError("ODYTOR_TEST_POLICIES must name the maintainer test policy file.")

        def number(name: str, default, low, high, cast=int):
            text = (_env(f"TEST_{name}") or "").strip()
            try:
                value = cast(text) if text else default
            except ValueError:
                value = None
            if value is None or not low <= value <= high:
                raise ValidationError(f"ODYTOR_TEST_{name} must be between {low} and {high}.")
            return value

        max_timeout = number("MAX_TIMEOUT", 600, 10, 3600)
        state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
        return cls(
            runner_command=tuple(command), policies_path=policies,
            data_dir=_env("TEST_DATA_DIR") or default_state_dir(state, "odytor", "git-plus", "tests"),
            max_queued=number("MAX_QUEUED", 5, 1, 50),
            default_timeout=number("DEFAULT_TIMEOUT", 300, 10, max_timeout), max_timeout=max_timeout,
            retention_hours=number("RETENTION_HOURS", 48.0, 0.1, 720.0, float),
            closed_grace_hours=number("CLOSED_GRACE_HOURS", 6.0, 0.1, 720.0, float),
            max_source_bytes=number("MAX_SOURCE_MIB", 256, 1, 4096) * _MIB,
            max_overlay_bytes=number("MAX_OVERLAY_BYTES", 65536, 1024, 1048576),
            min_free_bytes=number("MIN_FREE_MIB", 1024, 64, 1048576) * _MIB)


@dataclass(frozen=True)
class WorkspaceSettings:
    """Disposable engineering workspaces: git objects and a version counter, never a checked-out tree.

    Every limit is a hard cap enforced by the workspace service; there is no unbounded mode.
    """

    data_dir: str
    ttl_hours: float = 24.0
    max_ttl_hours: float = 72.0
    max_per_owner: int = 5
    max_workspace_bytes: int = 512 * _MIB
    max_total_bytes: int = 4096 * _MIB
    max_file_bytes: int = 1 * _MIB
    max_changed_files: int = 300
    max_checkpoints: int = 20
    max_runs: int = 40
    min_free_bytes: int = 1024 * _MIB
    allow_publish_apply: bool = False

    @classmethod
    def load(cls) -> WorkspaceSettings | None:
        from odytor.errors import ValidationError

        if (_env("WORKSPACES") or "on").strip().lower() in {"off", "0", "false", "no"}:
            return None

        def number(name: str, default, low, high, cast=int):
            text = (_env(f"WORKSPACE_{name}") or "").strip()
            try:
                value = cast(text) if text else default
            except ValueError:
                value = None
            if value is None or not low <= value <= high:
                raise ValidationError(f"ODYTOR_WORKSPACE_{name} must be between {low} and {high}.")
            return value

        state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
        max_ttl = number("MAX_TTL_HOURS", 72.0, 0.1, 720.0, float)
        allow_publish_apply = (_env("WORKSPACE_ALLOW_PUBLISH_APPLY") or "off").strip().lower() in {"on", "1", "true", "yes"}
        return cls(
            data_dir=_env("WORKSPACE_DIR") or os.path.join(state, "odytor", "workspaces"),
            ttl_hours=number("TTL_HOURS", 24.0, 0.1, max_ttl, float), max_ttl_hours=max_ttl,
            max_per_owner=number("MAX_PER_OWNER", 5, 1, 50),
            max_workspace_bytes=number("MAX_MIB", 512, 8, 16384) * _MIB,
            max_total_bytes=number("MAX_TOTAL_MIB", 4096, 8, 1048576) * _MIB,
            max_file_bytes=number("MAX_FILE_BYTES", 1048576, 1024, 8 * _MIB),
            max_changed_files=number("MAX_CHANGED_FILES", 300, 1, 5000),
            max_checkpoints=number("MAX_CHECKPOINTS", 20, 1, 100),
            max_runs=number("MAX_RUNS", 40, 1, 1000),
            min_free_bytes=number("MIN_FREE_MIB", 1024, 64, 1048576) * _MIB,
            allow_publish_apply=allow_publish_apply)


@dataclass(frozen=True)
class ArtifactSettings:
    """Bounded evidence storage (screenshots, logs, traces). Every artifact expires; nothing is served by URL."""

    data_dir: str
    retention_hours: float = 24.0
    max_artifact_bytes: int = 16 * _MIB
    max_image_delivery_bytes: int = 2 * _MIB
    max_owner_bytes: int = 512 * _MIB
    max_total_bytes: int = 4096 * _MIB

    @classmethod
    def load(cls) -> ArtifactSettings | None:
        from odytor.errors import ValidationError

        if (_env("ARTIFACTS") or "on").strip().lower() in {"off", "0", "false", "no"}:
            return None

        def number(name: str, default, low, high, cast=int):
            text = (_env(f"ARTIFACT_{name}") or "").strip()
            try:
                value = cast(text) if text else default
            except ValueError:
                value = None
            if value is None or not low <= value <= high:
                raise ValidationError(f"ODYTOR_ARTIFACT_{name} must be between {low} and {high}.")
            return value

        state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
        return cls(
            data_dir=_env("ARTIFACT_DIR") or os.path.join(state, "odytor", "artifacts"),
            retention_hours=number("RETENTION_HOURS", 24.0, 0.1, 720.0, float),
            max_artifact_bytes=number("MAX_MIB", 16, 1, 256) * _MIB,
            max_image_delivery_bytes=number("MAX_IMAGE_BYTES", 2 * _MIB, 4096, 16 * _MIB),
            max_owner_bytes=number("MAX_OWNER_MIB", 512, 8, 65536) * _MIB,
            max_total_bytes=number("MAX_TOTAL_MIB", 4096, 8, 1048576) * _MIB)


LEGACY_PAT_REPOSITORY = "odysseus-dev/odysseus"


@dataclass(frozen=True)
class ServerPat:
    """One server-held PAT, usable only by one exact OAuth login.

    `repository` names the single repository it may serve; None means every repository on the allowlist.
    Either way each request's client is scoped to the one repository that request targets.
    """

    token: str = field(repr=False)
    login: str
    repository: str | None


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
    workspaces: WorkspaceSettings | None = None
    artifacts: ArtifactSettings | None = None
    github_timeout: float = 120.0
    max_response_bytes: int = 262144
    server_pat: str | None = field(default=None, repr=False)
    server_pat_login: str | None = None
    server_pat_repository: str | None = None

    def server_pat_credential(self) -> ServerPat | None:
        """The configured server-side PAT, from the generic settings or the documented Odysseus aliases."""
        from odytor.errors import SecurityError

        generic = any((self.server_pat, self.server_pat_login, self.server_pat_repository))
        legacy = any((self.odysseus_pat, self.odysseus_pat_login))
        if generic and legacy:
            raise SecurityError("Configure either ODYTOR_SERVER_PAT* or the legacy ODYTOR_ODYSSEUS_PAT*, not both.")
        if not generic and not legacy:
            return None
        if generic:
            token, login, repository = self.server_pat, self.server_pat_login, self.server_pat_repository
        else:
            token, login, repository = self.odysseus_pat, self.odysseus_pat_login, LEGACY_PAT_REPOSITORY
        if not token or not (login or "").strip():
            raise SecurityError("A server-side PAT needs the token and the exact GitHub login allowed to use it.")
        repository = (repository or "").strip()
        return ServerPat(token, login.strip(), None if repository in {"", "*"} else repository.casefold())

    @classmethod
    def load(cls) -> Settings:
        """Load settings from environment variables."""
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        auth_token = _env("AUTH_TOKEN")

        try:
            max_lines = int(_env("DEFAULT_MAX_DIFF_LINES") or "500")
        except ValueError:
            max_lines = 500

        try:
            max_bytes = int(_env("DEFAULT_MAX_DIFF_BYTES") or "1048576")
        except ValueError:
            max_bytes = 1048576

        if not 1 <= max_lines <= 5000 or not 1 <= max_bytes <= 8 * 1024 * 1024:
            from odytor.errors import ValidationError
            raise ValidationError("Diff limits must be positive, at most 5000 lines and 8 MiB.")

        quick_fix_limits = []
        for name, default, ceiling in (
            ("QUICK_FIX_MAX_FILES", 10, 50),
            ("QUICK_FIX_MAX_CHANGED_LINES", 1000, 5000),
            ("QUICK_FIX_MAX_PATCH_BYTES", 262144, 1048576),
        ):
            raw = (_env(name) or "").strip()
            try:
                value = int(raw) if raw else default
            except ValueError:
                value = 0
            if not 1 <= value <= ceiling:
                from odytor.errors import ValidationError
                raise ValidationError(f"ODYTOR_{name} must be an integer from 1 to {ceiling}.")
            quick_fix_limits.append(value)

        raw_allowed = (_env("ALLOWED_REPOSITORIES") or "").strip()
        allowed = tuple(r.strip().casefold() for r in raw_allowed.split(",") if r.strip())
        raw_users = (_env("ALLOWED_GITHUB_USERS") or "").strip()
        users = tuple(u.strip().casefold() for u in raw_users.split(",") if u.strip())

        raw_timeout = (_env("GITHUB_TIMEOUT") or "").strip()
        try:
            github_timeout = float(raw_timeout) if raw_timeout else 120.0
        except ValueError:
            github_timeout = 0.0
        if not 10.0 <= github_timeout <= 600.0:
            from odytor.errors import ValidationError
            raise ValidationError("ODYTOR_GITHUB_TIMEOUT must be between 10 and 600 seconds.")

        raw_budget = (_env("MAX_RESPONSE_BYTES") or "").strip()
        try:
            response_budget = int(raw_budget) if raw_budget else 262144
        except ValueError:
            response_budget = 0
        if not 16384 <= response_budget <= 4 * 1024 * 1024:
            from odytor.errors import ValidationError
            raise ValidationError("ODYTOR_MAX_RESPONSE_BYTES must be between 16384 and 4194304.")

        return cls(
            github_token=token,
            github_timeout=github_timeout,
            max_response_bytes=response_budget,
            github_api_url=api_url,
            auth_token=auth_token,
            default_max_diff_lines=max_lines,
            default_max_diff_bytes=max_bytes,
            allowed_repositories=allowed,
            oauth_client_id=_env("GITHUB_CLIENT_ID"),
            oauth_client_secret=_env("GITHUB_CLIENT_SECRET"),
            public_base_url=_env("PUBLIC_URL"),
            allowed_github_users=users,
            oauth_signing_key=_env("OAUTH_SIGNING_KEY"),
            odysseus_pat=_env("ODYSSEUS_PAT"),
            odysseus_pat_login=_env("ODYSSEUS_PAT_LOGIN"),
            server_pat=_env("SERVER_PAT"),
            server_pat_login=_env("SERVER_PAT_LOGIN"),
            server_pat_repository=_env("SERVER_PAT_REPOSITORY"),
            quick_fix_max_files=quick_fix_limits[0],
            quick_fix_max_changed_lines=quick_fix_limits[1],
            quick_fix_max_patch_bytes=quick_fix_limits[2],
            testing=RunSettings.load(),
            workspaces=WorkspaceSettings.load(),
            artifacts=ArtifactSettings.load(),
        )

    def is_repo_allowed(self, repo: str) -> bool:
        """Check if a repository is permitted by allowed_repositories setting."""
        if not self.allowed_repositories:
            return True
        return repo.strip().casefold() in self.allowed_repositories
