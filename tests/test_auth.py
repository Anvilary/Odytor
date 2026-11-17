"""Tests for authentication configuration and client isolation."""

from unittest.mock import MagicMock, patch

import pytest

from git_plus.auth import create_auth, github_client
from git_plus.config import Settings
from git_plus.errors import SecurityError


def settings(**overrides):
    values = dict(
        github_token="local-token", github_api_url="https://api.github.com",
        auth_token=None, default_max_diff_lines=500, default_max_diff_bytes=1048576,
        allowed_repositories=(),
    )
    values.update(overrides)
    return Settings(**values)


def test_settings_loads_remote_auth_environment(monkeypatch):
    monkeypatch.setenv("GIT_PLUS_GITHUB_CLIENT_ID", "client")
    monkeypatch.setenv("GIT_PLUS_GITHUB_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GIT_PLUS_PUBLIC_URL", "https://mcp.example")
    monkeypatch.setenv("GIT_PLUS_ALLOWED_GITHUB_USERS", "Alice, bob")
    monkeypatch.setenv("GIT_PLUS_OAUTH_SIGNING_KEY", "x" * 32)
    loaded = Settings.load()
    assert loaded.oauth_client_id == "client"
    assert loaded.allowed_github_users == ("alice", "bob")


def test_remote_auth_fails_closed_without_credentials():
    with pytest.raises(SecurityError):
        create_auth(settings(), remote=True)


def test_remote_oauth_requires_nonempty_user_allowlist():
    configured = settings(
        oauth_client_id="client", oauth_client_secret="secret",
        public_base_url="https://mcp.example", oauth_signing_key="x" * 32,
    )
    with pytest.raises(SecurityError):
        create_auth(configured, remote=True)


def test_github_client_closes_its_per_call_session():
    client = MagicMock()
    with patch("git_plus.auth.GitHubClient", return_value=client) as factory:
        with github_client(settings()) as result:
            assert result is client
        factory.assert_called_once_with(token="local-token", base_url="https://api.github.com")
        client.session.close.assert_called_once_with()


def oauth_settings(**overrides):
    values = dict(oauth_client_id='client', oauth_client_secret='secret',
                  public_base_url='https://mcp.example.com', oauth_signing_key='x'*32,
                  allowed_github_users=('alice', 'bob'))
    values.update(overrides)
    return settings(**values)


def test_oauth_clients_use_each_request_identity_without_fallback():
    from fastmcp.server.auth import AccessToken
    from contextlib import ExitStack
    alice = AccessToken(token='alice-upstream', client_id='1', scopes=['repo'], claims={'login': 'Alice'})
    bob = AccessToken(token='bob-upstream', client_id='2', scopes=['repo'], claims={'login': 'Bob'})
    created = []
    def factory(**kwargs):
        created.append(kwargs['token'])
        return MagicMock()
    with patch('fastmcp.server.dependencies.get_access_token', side_effect=[alice, bob]), patch('git_plus.auth.GitHubClient', side_effect=factory):
        # Keep both contexts open to prove identity is not shared across calls.
        with github_client(oauth_settings(), remote=True) as a, github_client(oauth_settings(), remote=True) as b:
            assert a is not b
    assert created == ['alice-upstream', 'bob-upstream']


@pytest.mark.parametrize('token', [None, 'unauthorized', 'missing-upstream'])
def test_oauth_rejects_absent_or_unapproved_identity(token):
    from fastmcp.server.auth import AccessToken
    access = None if token is None else AccessToken(token='' if token == 'missing-upstream' else 'upstream',
                client_id='3', scopes=['repo'], claims={'login': 'mallory' if token == 'unauthorized' else 'alice'})
    with patch('fastmcp.server.dependencies.get_access_token', return_value=access), patch('git_plus.auth.GitHubClient') as factory:
        with pytest.raises(SecurityError):
            with github_client(oauth_settings(), remote=True):
                pass
        factory.assert_not_called()


def test_oauth_discovery_and_unauthenticated_http_rejection(tmp_path, monkeypatch):
    from git_plus.server import create_server
    from starlette.testclient import TestClient
    monkeypatch.setenv('FASTMCP_HOME', str(tmp_path))
    server = create_server(oauth_settings(), remote=True)
    with TestClient(server.http_app(stateless_http=True)) as client:
        response = client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        assert response.status_code == 401
        assert 'resource_metadata' in response.headers['www-authenticate']
        metadata = client.get('/.well-known/oauth-protected-resource/mcp')
        assert metadata.status_code == 200
        assert metadata.json()['authorization_servers']


def test_authenticated_streamable_http_tool_discovery():
    from git_plus.server import create_server
    from starlette.testclient import TestClient
    configured = settings(auth_token='test-service-token', allowed_repositories=('owner/repo',))
    server = create_server(configured, remote=True)
    with TestClient(server.http_app(stateless_http=True, json_response=True)) as client:
        headers = {'Authorization': 'Bearer test-service-token', 'Accept': 'application/json, text/event-stream'}
        response = client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        assert response.status_code == 200
        tools = response.json()['result']['tools']
        assert {t['name'] for t in tools} == {'pr_evidence', 'ci_diagnose', 'quick_fix', 'pr_conflicts', 'pr_rebase', 'ci_control'}
        rejected = client.post('/mcp', headers={**headers, 'Authorization': 'Bearer wrong'},
                               json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        assert rejected.status_code == 401


@pytest.mark.parametrize('overrides', [{'public_base_url': 'http://example.com'},
                                    {'oauth_signing_key': 'short'},
                                    {'github_api_url': 'https://evil.example.com'}])
def test_invalid_oauth_configuration(overrides):
    with pytest.raises(SecurityError):
        create_auth(oauth_settings(**overrides), remote=True)


def test_static_auth_cannot_fall_back_without_request_credentials():
    with patch('fastmcp.server.dependencies.get_access_token', return_value=None):
        with pytest.raises(SecurityError):
            with github_client(settings(auth_token='static', allowed_repositories=('owner/repo',)), remote=True):
                pass


def test_streamable_http_rejects_untrusted_origin():
    from git_plus.server import create_server
    from starlette.testclient import TestClient
    server = create_server(settings(auth_token='test-service-token', allowed_repositories=('owner/repo',)), remote=True)
    app = server.http_app(stateless_http=True, host_origin_protection=True,
                          allowed_hosts=['testserver'], allowed_origins=['https://mcp.example.com'])
    with TestClient(app) as client:
        response = client.post('/mcp', headers={'Authorization': 'Bearer test-service-token',
                    'Origin': 'https://untrusted.example'}, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        assert response.status_code == 403


def test_http_calls_keep_oauth_identities_separate_and_enforce_user_allowlist(tmp_path, monkeypatch):
    from fastmcp.server.auth import AccessToken
    from git_plus.server import create_server
    from starlette.testclient import TestClient
    from unittest.mock import AsyncMock
    monkeypatch.setenv('FASTMCP_HOME', str(tmp_path))
    server = create_server(oauth_settings(), remote=True)
    identities = {
        'alice': AccessToken(token='alice-upstream', client_id='1', scopes=['repo'], claims={'login': 'alice'}),
        'bob': AccessToken(token='bob-upstream', client_id='2', scopes=['repo'], claims={'login': 'bob'}),
        'mallory': AccessToken(token='mallory-upstream', client_id='3', scopes=['repo'], claims={'login': 'mallory'}),
    }
    server.auth.verify_token = AsyncMock(side_effect=lambda value: identities.get(value))
    observed = []
    def evidence(client, repo, number, **kwargs):
        observed.append(client.token)
        return {'repo': repo, 'number': number}
    with patch('git_plus.server.get_pr_evidence', side_effect=evidence):
        with TestClient(server.http_app(stateless_http=True, json_response=True)) as client:
            for identity in identities:
                response = client.post('/mcp', headers={'Authorization': f'Bearer {identity}',
                    'Accept': 'application/json, text/event-stream'},
                    json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                          'params': {'name': 'pr_evidence', 'arguments': {'repo': 'owner/repo', 'number': 1}}})
                assert response.status_code == 200
                if identity == 'mallory':
                    assert 'SecurityError' in response.text
    assert observed == ['alice-upstream', 'bob-upstream']


def test_conflicting_authentication_configuration_rejected():
    with pytest.raises(SecurityError):
        create_auth(oauth_settings(auth_token='legacy'), remote=True)
