"""Generic GitHub credential configuration: local PATs, OAuth identity, server-side PAT routing, accurate errors."""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest
import requests

from git_plus.auth import create_auth, github_client
from git_plus.config import Settings
from git_plus.errors import APIError, SecurityError
from git_plus.github import GitHubClient

REPO = "acme/widgets"
PAT = "github_pat_11AAAA_server_secret_that_must_not_leak"
CLASSIC = "ghp_" + "c" * 36
FINE = "github_pat_" + "f" * 40


def settings(**changes):
    defaults = dict(
        github_token=None, github_api_url="https://api.github.com", auth_token=None,
        default_max_diff_lines=500, default_max_diff_bytes=1048576,
        allowed_repositories=(REPO, "other/project"), allowed_github_users=("maintainer", "bob"),
        oauth_client_id="client", oauth_client_secret="secret", public_base_url="https://git-plus.example.com",
        oauth_signing_key="x" * 32, server_pat=PAT, server_pat_login="maintainer", server_pat_repository=REPO)
    defaults.update(changes)
    return Settings(**defaults)


def oauth(login, upstream):
    fake = types.ModuleType("fastmcp.server.dependencies")
    fake.get_access_token = lambda: types.SimpleNamespace(token=upstream, claims={"login": login})
    return patch.dict(sys.modules, {"fastmcp": types.ModuleType("fastmcp"), "fastmcp.server": types.ModuleType("fastmcp.server"),
                                    "fastmcp.server.dependencies": fake})


def response(status, body='{"message":"x"}', **headers):
    resp = requests.Response()
    resp.status_code = status
    resp._content = body.encode()
    resp._content_consumed = True
    resp.headers.update(headers)
    return resp


def client_with(token, resp):
    session = MagicMock(spec=requests.Session)
    session.request.return_value = resp
    return GitHubClient(token=token, session=session), session


# --- configuration ---------------------------------------------------------------------------------------------

def test_generic_environment_loading_and_no_secret_in_repr(monkeypatch):
    for key in ("GIT_PLUS_ODYSSEUS_PAT", "GIT_PLUS_ODYSSEUS_PAT_LOGIN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GIT_PLUS_SERVER_PAT", PAT)
    monkeypatch.setenv("GIT_PLUS_SERVER_PAT_LOGIN", "Maintainer")
    monkeypatch.setenv("GIT_PLUS_SERVER_PAT_REPOSITORY", "Acme/Widgets")
    loaded = Settings.load()
    credential = loaded.server_pat_credential()
    assert (credential.login, credential.repository, credential.token) == ("Maintainer", REPO, PAT)
    assert PAT not in repr(loaded) and PAT not in repr(credential)


def test_legacy_odysseus_variables_map_to_the_same_mechanism():
    legacy = settings(server_pat=None, server_pat_login=None, server_pat_repository=None,
                      odysseus_pat=PAT, odysseus_pat_login="maintainer", allowed_repositories=("odysseus-dev/odysseus",))
    credential = legacy.server_pat_credential()
    assert credential.repository == "odysseus-dev/odysseus" and credential.login == "maintainer"
    create_auth(legacy, remote=True)  # accepted: same validation as the generic form


@pytest.mark.parametrize("changes", [
    {"odysseus_pat": "ghp_other"},                       # both generations configured
    {"server_pat_login": None},                          # token without an owner
    {"server_pat": None},                                # owner without a token
])
def test_ambiguous_or_incomplete_server_pat_configuration_is_refused(changes):
    with pytest.raises(SecurityError):
        settings(**changes).server_pat_credential()


@pytest.mark.parametrize("changes", [
    {"allowed_repositories": ("other/project",)},        # the PAT's repository must be explicitly allowed
    {"allowed_github_users": ("bob",)},                  # the PAT owner must be an allowed user
    {"oauth_client_secret": None},                       # OAuth is required
    {"auth_token": "static-service-token"},              # no mixing with static auth
])
def test_server_pat_requires_oauth_owner_and_repository_allowlist(changes):
    with pytest.raises(SecurityError):
        create_auth(settings(**changes), remote=True)


# --- credential selection --------------------------------------------------------------------------------------

def test_server_pat_is_used_only_for_its_owner_and_its_repository():
    with oauth("maintainer", "user-oauth"), patch("git_plus.auth.GitHubClient", return_value=MagicMock()) as factory:
        with github_client(settings(), remote=True, repo="Acme/Widgets"):
            pass
        factory.assert_called_once_with(token=PAT, base_url="https://api.github.com", timeout=120.0, restricted_repo=REPO, principal="maintainer")
    for login, repo in (("bob", REPO), ("maintainer", "other/project")):
        with oauth(login, "user-oauth"), patch("git_plus.auth.GitHubClient", return_value=MagicMock()) as factory:
            with github_client(settings(), remote=True, repo=repo):
                pass
            factory.assert_called_once_with(token="user-oauth", base_url="https://api.github.com", timeout=120.0, principal=login)


def test_unlisted_users_and_missing_identity_never_reach_a_credential():
    with oauth("mallory", "mallory-oauth"), pytest.raises(SecurityError, match="not permitted"):
        with github_client(settings(), remote=True, repo=REPO):
            pass
    with oauth("maintainer", "user-oauth"), pytest.raises(SecurityError, match="Repository context"):
        with github_client(settings(), remote=True):
            pass
    fake = types.ModuleType("fastmcp.server.dependencies")
    fake.get_access_token = lambda: None
    with patch.dict(sys.modules, {"fastmcp": types.ModuleType("fastmcp"), "fastmcp.server": types.ModuleType("fastmcp.server"),
                                  "fastmcp.server.dependencies": fake}), pytest.raises(SecurityError):
        with github_client(settings(), remote=True, repo=REPO):
            pass


def test_server_pat_client_refuses_other_repositories_before_any_request():
    client, session = client_with(PAT, response(200))
    client.restricted_repo = REPO
    with pytest.raises(SecurityError):
        client.request("GET", "repos/other/project/pulls/1")
    session.request.assert_not_called()


@pytest.mark.parametrize("variable", ["GITHUB_TOKEN", "GH_TOKEN"])
@pytest.mark.parametrize("token", [CLASSIC, FINE])
def test_local_stdio_uses_any_pat_shape_from_either_variable_without_oauth(monkeypatch, variable, token):
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(variable, token)
    loaded = Settings.load()
    assert create_auth(loaded, remote=False) is None            # no OAuth App needed locally
    with github_client(loaded, remote=False, repo=REPO) as client:
        assert client.token == token and client.restricted_repo is None and client._headers()["Authorization"] == f"Bearer {token}"


# --- accurate permission reporting ----------------------------------------------------------------------------

def failure(token, status, **headers):
    client, _ = client_with(token, response(status, **headers))
    with pytest.raises(APIError) as raised:
        client.request("GET", "repos/acme/widgets/pulls/1")
    return raised.value


def test_rejected_credential_is_reported_as_bad_credentials():
    err = failure(FINE, 401)
    assert err.diagnostic["classification"] == "bad_credentials" and err.status_code == 401


def test_insufficient_permissions_pass_on_what_github_reported():
    fine = failure(FINE, 403, **{"x-accepted-github-permissions": "pull_requests=write"})
    classic = failure(CLASSIC, 403, **{"x-oauth-scopes": "read:user", "x-accepted-github-permissions": "pull_requests=write"})
    assert fine.diagnostic["classification"] == "insufficient_permissions"
    assert fine.diagnostic["accepted_permissions"] == "pull_requests=write" and "token_scopes" not in fine.diagnostic
    assert classic.diagnostic["token_scopes"] == "read:user"  # reported, not assumed equal to a fine-grained grant


def test_404_is_not_asserted_to_mean_missing_and_no_credential_is_called_out():
    assert failure(CLASSIC, 404).diagnostic["classification"] == "not_found_or_no_access"
    anonymous = failure(None, 404)
    assert anonymous.diagnostic["classification"] == "no_credential_configured" and "GITHUB_TOKEN" in anonymous.diagnostic["hint"]


def test_rate_limits_keep_their_own_classification():
    client, _ = client_with(CLASSIC, response(403, '{"message":"API rate limit exceeded"}', **{"x-ratelimit-remaining": "0"}))
    with pytest.raises(APIError) as raised:
        client.request("GET", "repos/acme/widgets")
    assert raised.value.diagnostic["classification"] == "rate_limit_exceeded"


def test_credentials_are_redacted_even_when_github_echoes_them():
    client, _ = client_with(CLASSIC, response(403, f'{{"message":"bad token {CLASSIC}"}}'))
    with pytest.raises(APIError) as raised:
        client.request("GET", "repos/acme/widgets")
    assert CLASSIC not in str(raised.value.to_dict())


# --- one PAT for every allowlisted repository (no repository configured) ---------------------------------------

def test_unset_repository_means_every_allowlisted_repository_for_the_owner_only():
    all_repos = settings(server_pat_repository=None)
    assert all_repos.server_pat_credential().repository is None
    create_auth(all_repos, remote=True)
    for repo in (REPO, "other/project", "Other/Project"):
        with oauth("maintainer", "user-oauth"), patch("git_plus.auth.GitHubClient", return_value=MagicMock()) as factory:
            with github_client(all_repos, remote=True, repo=repo):
                pass
            factory.assert_called_once_with(token=PAT, base_url="https://api.github.com", timeout=120.0, restricted_repo=repo.casefold(), principal="maintainer")
    with oauth("maintainer", "user-oauth"), patch("git_plus.auth.GitHubClient", return_value=MagicMock()) as factory:
        with github_client(all_repos, remote=True, repo="not/allowed"):
            pass
        assert factory.call_args.kwargs["token"] == "user-oauth"       # the PAT never serves a repository off the allowlist
    with oauth("bob", "bob-oauth"), patch("git_plus.auth.GitHubClient", return_value=MagicMock()) as factory:
        with github_client(all_repos, remote=True, repo=REPO):
            pass
        assert factory.call_args.kwargs["token"] == "bob-oauth"        # nor another login


def test_all_repository_pat_still_needs_an_allowlist_and_scopes_each_client_to_one_repository():
    with pytest.raises(SecurityError):
        create_auth(settings(server_pat_repository="*", allowed_repositories=()), remote=True)
    assert settings(server_pat_repository="*").server_pat_credential().repository is None
    client, session = client_with(PAT, response(200))
    client.restricted_repo = REPO
    with pytest.raises(SecurityError):
        client.request("GET", "repos/other/project/pulls/1")           # a client built for one repository cannot touch another
    session.request.assert_not_called()
