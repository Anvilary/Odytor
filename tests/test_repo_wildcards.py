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
    raw = "alteixeira20/*, Anvilary/*"
    parsed = parse_allowed_repositories(raw)
    assert parsed == ("alteixeira20/*", "anvilary/*")

    cfg = Settings(
        github_token="tok",
        github_api_url="https://api.github.com",
        auth_token=None,
        default_max_diff_lines=500,
        default_max_diff_bytes=1048576,
        allowed_repositories=parsed,
    )

    # Allowed per requirement table
    assert cfg.is_repo_allowed("alteixeira20/odytor-smoke-test")
    assert cfg.is_repo_allowed("alteixeira20/42_webserv")
    assert cfg.is_repo_allowed("Anvilary/Odytor")
    assert cfg.is_repo_allowed("Anvilary/RoadForge")
    assert cfg.is_repo_allowed("alteixeira20/new-future-repository")

    # Denied per requirement table
    assert not cfg.is_repo_allowed("odysseus-dev/odysseus")
    assert not cfg.is_repo_allowed("alteixeira20-evil/repository")
    assert not cfg.is_repo_allowed("Anvilary-fake/repository")
    assert not cfg.is_repo_allowed("other/alteixeira20/repository")


def test_case_insensitivity():
    cfg = Settings(
        github_token="tok",
        github_api_url="https://api.github.com",
        auth_token=None,
        default_max_diff_lines=500,
        default_max_diff_bytes=1048576,
        allowed_repositories=("alteixeira20/*", "Anvilary/Odytor"),
    )
    assert cfg.is_repo_allowed("ALTEIXEIRA20/Odytor-Smoke-Test")
    assert cfg.is_repo_allowed("anvilary/odytor")
    assert not cfg.is_repo_allowed("ALTEIXEIRA20-EVIL/Repo")


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
    monkeypatch.setenv("ODYTOR_ALLOWED_REPOSITORIES", "alteixeira20/*, *")
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
        allowed_repositories=("alteixeira20/*",),
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
        allowed_repositories=("alteixeira20/*", "anvilary/*"),
        allowed_github_users=("alteixeira20",),
        oauth_client_id="oauth-client",
        oauth_client_secret="oauth-secret",
        public_base_url="https://odytor.example.com",
        oauth_signing_key="x" * 32,
        server_pat="ghp_pat_secret",
        server_pat_login="alteixeira20",
        server_pat_repository=None,
    )

    fake = types.ModuleType("fastmcp.server.dependencies")
    fake.get_access_token = lambda: types.SimpleNamespace(
        token="user-oauth", claims={"login": "alteixeira20"}
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
        # alteixeira20 repo matches alteixeira20/* -> PAT serves it
        with github_client(settings, remote=True, repo="alteixeira20/odytor-smoke-test"):
            pass
        # anvilary repo matches anvilary/* -> PAT also serves it because pat.repository is None (allowlist-wide)
        with github_client(settings, remote=True, repo="Anvilary/Odytor"):
            pass

    assert len(created) == 2
    assert created[0]["token"] == "ghp_pat_secret"
    assert created[0]["restricted_repo"] == "alteixeira20/odytor-smoke-test"
    assert created[1]["token"] == "ghp_pat_secret"
    assert created[1]["restricted_repo"] == "anvilary/odytor"
