"""Tests for owner-scoped repository wildcards and exact allowlists."""
from __future__ import annotations

import pytest

from odytor.auth import create_auth, github_client
from odytor.config import (
    Settings,
    parse_allowed_repositories,
    validate_allowed_repository_pattern,
)
from odytor.errors import SecurityError, ValidationError


def test_parse_allowed_repositories_semantics():
    raw = "maintainer/*, Anvilary/*"
    parsed = parse_allowed_repositories(raw)
    assert parsed == ("maintainer/*", "anvilary/*")

    cfg = Settings(
        github_token="tok",
        github_api_url="https://api.github.com",
        auth_token=None,
        default_max_diff_lines=500,
        default_max_diff_bytes=1048576,
        allowed_repositories=parsed,
    )

    # Allowed per requirement table
    assert cfg.is_repo_allowed("maintainer/smoke-test")
    assert cfg.is_repo_allowed("maintainer/webserv")
    assert cfg.is_repo_allowed("Anvilary/Odytor")
    assert cfg.is_repo_allowed("Anvilary/RoadForge")
    assert cfg.is_repo_allowed("maintainer/new-future-repository")

    # Denied per requirement table
    assert not cfg.is_repo_allowed("other-org/other-repo")
    assert not cfg.is_repo_allowed("maintainer-evil/repository")
    assert not cfg.is_repo_allowed("Anvilary-fake/repository")
    assert not cfg.is_repo_allowed("other/maintainer/repository")


def test_case_insensitivity():
    cfg = Settings(
        github_token="tok",
        github_api_url="https://api.github.com",
        auth_token=None,
        default_max_diff_lines=500,
        default_max_diff_bytes=1048576,
        allowed_repositories=("maintainer/*", "Anvilary/Odytor"),
    )
    assert cfg.is_repo_allowed("MAINTAINER/Smoke-Test")
    assert cfg.is_repo_allowed("anvilary/odytor")
    assert not cfg.is_repo_allowed("MAINTAINER-EVIL/Repo")


@pytest.mark.parametrize("invalid", [
    "*",
    "*/*",
    "owner/*repo",
    "owner/repo*",
    "owner/*/*",
    "../repo",
    "owner/..",
    "owner/.",
    "./repo",
    "owner",
    "/repo",
    "owner/",
    "a/b/c",
    "owner//repo",
    "*owner/repo",
    "owner/repo?",
    "owner/repo#foo",
])
def test_rejected_patterns(invalid: str):
    with pytest.raises(ValidationError):
        validate_allowed_repository_pattern(invalid)


def test_invalid_configuration_in_env_fails_closed(monkeypatch):
    monkeypatch.setenv("ODYTOR_ALLOWED_REPOSITORIES", "maintainer/*, *")
    with pytest.raises(ValidationError):
        Settings.load()


def test_empty_allowlist_permits_in_local_mode():
    cfg = Settings(
        github_token="tok",
        github_api_url="https://api.github.com",
        auth_token=None,
        default_max_diff_lines=500,
        default_max_diff_bytes=1048576,
        allowed_repositories=(),
    )
    assert cfg.is_repo_allowed("any/repo")


def test_malformed_repo_argument_to_is_repo_allowed():
    cfg = Settings(
        github_token="tok",
        github_api_url="https://api.github.com",
        auth_token=None,
        default_max_diff_lines=500,
        default_max_diff_bytes=1048576,
        allowed_repositories=("maintainer/*",),
    )
    assert not cfg.is_repo_allowed("")
    assert not cfg.is_repo_allowed("../evil")
    assert not cfg.is_repo_allowed("owner")
    assert not cfg.is_repo_allowed("a/b/c")
    assert not cfg.is_repo_allowed(None)  # type: ignore


def test_pat_routing_with_owner_wildcard():
    from unittest.mock import MagicMock, patch
    import types
    import sys

    settings = Settings(
        github_token=None,
        github_api_url="https://api.github.com",
        auth_token=None,
        default_max_diff_lines=500,
        default_max_diff_bytes=1048576,
        allowed_repositories=("maintainer/*", "anvilary/*"),
        allowed_github_users=("maintainer",),
        oauth_client_id="oauth-client",
        oauth_client_secret="oauth-secret",
        public_base_url="https://odytor.example.com",
        oauth_signing_key="x" * 32,
        server_pat="ghp_pat_secret",
        server_pat_login="maintainer",
        server_pat_repository=None,
    )

    fake = types.ModuleType("fastmcp.server.dependencies")
    fake.get_access_token = lambda: types.SimpleNamespace(
        token="user-oauth", claims={"login": "maintainer"}
    )
    modules_patch = patch.dict(sys.modules, {
        "fastmcp": types.ModuleType("fastmcp"),
        "fastmcp.server": types.ModuleType("fastmcp.server"),
        "fastmcp.server.dependencies": fake,
    })

    created = []
    def capture(**kwargs):
        created.append(kwargs)
        return MagicMock()

    with modules_patch, patch("odytor.auth.GitHubClient", side_effect=capture):
        # maintainer repo matches maintainer/* -> PAT serves it
        with github_client(settings, remote=True, repo="maintainer/smoke-test"):
            pass
        # anvilary repo matches anvilary/* -> PAT also serves it because pat.repository is None (allowlist-wide)
        with github_client(settings, remote=True, repo="Anvilary/Odytor"):
            pass

    assert len(created) == 2
    assert created[0]["token"] == "ghp_pat_secret"
    assert created[0]["restricted_repo"] == "maintainer/smoke-test"
    assert created[1]["token"] == "ghp_pat_secret"
    assert created[1]["restricted_repo"] == "anvilary/odytor"
