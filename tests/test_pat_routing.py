"""Server-held classic PAT must never become an unscoped GitHub credential."""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from odytor.auth import create_auth, github_client
from odytor.config import Settings
from odytor.errors import APIError, SecurityError
from odytor.git_workspace import GitWorkspace, pr_context
from odytor.github import GitHubClient


TARGET = "odysseus-dev/odysseus"
PAT = "ghp_secret_that_must_not_leak"


def settings(**changes):
    defaults = dict(
        github_token=None, github_api_url="https://api.github.com", auth_token=None,
        default_max_diff_lines=500, default_max_diff_bytes=1048576,
        allowed_repositories=(TARGET, "anvilary/git-plus"),
        allowed_github_users=("maintainer",),
        oauth_client_id="oauth-client", oauth_client_secret="oauth-secret",
        public_base_url="https://git-plus.example.com", oauth_signing_key="x" * 32,
        odysseus_pat=PAT, odysseus_pat_login="maintainer",
    )
    defaults.update(changes)
    return Settings(**defaults)


@pytest.mark.parametrize("changes", [
    {"allowed_repositories": ()},
    {"allowed_repositories": ("anvilary/git-plus",)},
    {"odysseus_pat_login": None},
    {"odysseus_pat_login": "mallory"},
    {"oauth_client_secret": None},
    {"auth_token": "unapproved-static-token"},
])
def test_pat_requires_explicit_oauth_identity_and_repo_allowlist(changes):
    with pytest.raises(SecurityError):
        create_auth(settings(**changes), remote=True)


def _with_mocked_oauth_token(login, upstream):
    """Patch only FastMCP's contextual token accessor (without needing HTTP)."""
    fake = types.ModuleType("fastmcp.server.dependencies")
    fake.get_access_token = lambda: types.SimpleNamespace(
        token=upstream, claims={"login": login}
    )
    return patch.dict(sys.modules, {
        "fastmcp": types.ModuleType("fastmcp"),
        "fastmcp.server": types.ModuleType("fastmcp.server"),
        "fastmcp.server.dependencies": fake,
    })


def test_pat_used_only_for_exact_authenticated_identity_and_target():
    created = []
    def capture(**kwargs):
        created.append(kwargs)
        return MagicMock()
    with _with_mocked_oauth_token("maintainer", "normal-upstream"), patch(
        "odytor.auth.GitHubClient", side_effect=capture
    ):
        with github_client(settings(), remote=True, repo=TARGET):
            pass
        with github_client(settings(), remote=True, repo="Anvilary/git-plus"):
            pass
    assert created == [
        {"token": PAT, "base_url": "https://api.github.com", "timeout": 120.0, "restricted_repo": TARGET,
         "principal": "maintainer"},
        {"token": "normal-upstream", "base_url": "https://api.github.com", "timeout": 120.0, "principal": "maintainer"},
    ]


def test_pat_never_used_for_another_allowed_identity():
    with _with_mocked_oauth_token("another-user", "their-upstream"), patch(
        "odytor.auth.GitHubClient", return_value=MagicMock()
    ) as factory:
        with pytest.raises(SecurityError):
            with github_client(settings(allowed_github_users=("maintainer",)), remote=True, repo=TARGET):
                pass
        factory.assert_not_called()


def test_pat_requires_repository_context_and_upstream_oauth():
    with _with_mocked_oauth_token("maintainer", "normal-upstream"):
        with pytest.raises(SecurityError, match="Repository context"):
            with github_client(settings(), remote=True):
                pass
    with _with_mocked_oauth_token("maintainer", None):
        with pytest.raises(SecurityError, match="unavailable"):
            with github_client(settings(), remote=True, repo=TARGET):
                pass


def test_pat_rest_api_scope_rejected_before_network_access():
    client = GitHubClient(token=PAT, restricted_repo=TARGET, session=MagicMock())
    with pytest.raises(SecurityError):
        client.request("GET", "repos/contributor/odysseus")
    with pytest.raises(SecurityError):
        client.request("POST", "repos/contributor/odysseus/actions/workflows/123/dispatches")
    with pytest.raises(SecurityError):
        client.request("GET", "user")
    client.session.request.assert_not_called()


def test_pat_graphql_scope_rejected_before_network_access():
    client = GitHubClient(token=PAT, restricted_repo=TARGET, session=MagicMock())
    read_query = "query($owner: String!, $repo: String!) { repository(owner: $owner, name: $repo) { reviewThreads(first: 1) { nodes { id } } } }"
    with pytest.raises(SecurityError):
        client.graphql(read_query, {"owner": "contributor", "repo": "odysseus"})
    mutation = "mutation($input: CreateCommitOnBranchInput!) { createCommitOnBranch(input: $input) { commit { oid } } }"
    with pytest.raises(SecurityError):
        client.graphql(mutation, {"input": {"branch": {"repositoryNameWithOwner": "contributor/odysseus"}}})
    with pytest.raises(SecurityError):
        client.graphql("query { viewer { login } }")
    client.session.request.assert_not_called()


def test_pat_fetches_fork_anonymously_and_base_with_credentials():
    client = GitHubClient(token=PAT, restricted_repo=TARGET)
    with GitWorkspace(client) as work:
        fork_env = work.environment(network=True, credential_repo="contributor/odysseus")
        base_env = work.environment(network=True, credential_repo=TARGET)
        assert not any(PAT in value for value in fork_env.values())
        assert "GIT_CONFIG_COUNT" not in fork_env
        assert base_env["GIT_CONFIG_COUNT"] == "1"
        with pytest.raises(SecurityError):
            work.environment(network=True)
        with pytest.raises(SecurityError):
            work.git("ls-remote", "https://github.com/contributor/odysseus.git",
                     network=True, credential_repo=TARGET)


def test_pat_fork_write_only_for_quick_fix_and_never_for_rebase():
    client = GitHubClient(token=PAT, restricted_repo=TARGET, principal="maintainer", session=MagicMock())
    pr = {"state": "open", "merged": False, "maintainer_can_modify": True,
          "head": {"sha": "a" * 40, "ref": "feature", "repo": {"full_name": "contributor/odysseus", "fork": True}},
          "base": {"sha": "b" * 40, "ref": "dev", "repo": {"full_name": TARGET}}}
    with patch.object(client, "get_pr", return_value=pr):
        with pytest.raises(SecurityError, match="additive single-commit operations"):
            pr_context(client, TARGET, 42, "a" * 40, write=True)
        with pytest.raises(SecurityError, match="additive single-commit operations"):
            pr_context(client, TARGET, 42, "a" * 40, write=True, operation="rebase")
    assert client.fork_grant is None
    client.session.request.assert_not_called()


def test_code_scanning_permission_errors_are_not_empty_findings():
    client = GitHubClient(token=PAT, restricted_repo=TARGET)
    with patch.object(client, "request", side_effect=APIError("403 denied", status_code=403)):
        with pytest.raises(APIError):
            client.get_code_scanning_alerts(TARGET)


def test_allowed_scoped_api_request_sends_pat_only_to_target():
    import requests
    response = requests.Response()
    response.status_code = 200
    response._content = b'{"ok":true}'
    response._content_consumed = True
    session = MagicMock()
    session.request.return_value = response
    client = GitHubClient(token=PAT, restricted_repo=TARGET, session=session)
    assert client.request("GET", f"repos/{TARGET}/pulls/1").json() == {"ok": True}
    sent_headers = session.request.call_args.kwargs["headers"]
    assert sent_headers["Authorization"] == f"Bearer {PAT}"


def test_scoped_api_refuses_same_origin_redirect_to_other_repo():
    import requests
    redirected = requests.Response()
    redirected.status_code = 302
    redirected.headers["Location"] = "https://api.github.com/repos/contributor/odysseus/pulls/1"
    redirected._content = b""
    redirected._content_consumed = True
    session = MagicMock()
    session.request.return_value = redirected
    client = GitHubClient(token=PAT, restricted_repo=TARGET, session=session)
    with pytest.raises(SecurityError):
        client.request("GET", f"repos/{TARGET}/pulls/1")
    session.request.assert_called_once()


def test_non_pat_oauth_identity_can_still_use_its_own_token():
    with _with_mocked_oauth_token("bob", "bob-oauth"), patch(
        "odytor.auth.GitHubClient", return_value=MagicMock()
    ) as factory:
        with github_client(settings(allowed_github_users=("maintainer", "bob")), remote=True, repo=TARGET):
            pass
        factory.assert_called_once_with(token="bob-oauth", base_url="https://api.github.com", timeout=120.0, principal="bob")


def test_odysseus_pat_environment_loading(monkeypatch):
    monkeypatch.setenv("ODYTOR_ODYSSEUS_PAT", PAT)
    monkeypatch.setenv("ODYTOR_ODYSSEUS_PAT_LOGIN", "maintainer")
    result = Settings.load()
    assert result.odysseus_pat == PAT
    assert result.odysseus_pat_login == "maintainer"
    assert PAT not in repr(result)
