"""Tests for GitHubClient network and API operations."""

from unittest.mock import MagicMock
import pytest
import requests
from git_plus.errors import APIError, SecurityError, ValidationError
from git_plus.github import GitHubClient, validate_repo, validate_sha


def test_validate_repo_valid() -> None:
    owner, name = validate_repo("Anvilary/git-plus")
    assert owner == "Anvilary"
    assert name == "git-plus"


def test_validate_repo_invalid() -> None:
    with pytest.raises(ValidationError):
        validate_repo("invalid_format")
    with pytest.raises(SecurityError):
        validate_repo("../repo")


def test_validate_sha_valid() -> None:
    sha = "1234567890abcdef1234567890abcdef12345678"
    assert validate_sha(sha) == sha


def test_validate_sha_invalid() -> None:
    with pytest.raises(ValidationError):
        validate_sha("short-sha")
    with pytest.raises(ValidationError):
        validate_sha("not-hex-123456789012345678901234567890zzzz")


def test_client_request_rate_limit() -> None:
    mock_session = MagicMock(spec=requests.Session)
    resp = requests.Response()
    resp.status_code = 403
    resp._content = b'{"message": "API rate limit exceeded"}'
    resp._content_consumed = True
    resp.headers["x-ratelimit-reset"] = "1700000000"
    mock_session.request.return_value = resp

    client = GitHubClient(token="test_tok", session=mock_session)
    with pytest.raises(APIError) as exc_info:
        client.get_pr("owner/repo", 1)
    assert exc_info.value.status_code == 403
    assert exc_info.value.diagnostic.get("classification") == "rate_limit_exceeded"


def test_client_request_retry_on_502() -> None:
    mock_session = MagicMock(spec=requests.Session)
    resp_bad = requests.Response()
    resp_bad.status_code = 502
    resp_bad._content = b"Bad Gateway"
    resp_bad._content_consumed = True

    resp_ok = requests.Response()
    resp_ok.status_code = 200
    resp_ok._content = b'{"number": 1, "head": {"sha": "123"}}'
    resp_ok._content_consumed = True

    mock_session.request.side_effect = [resp_bad, resp_ok]

    client = GitHubClient(token="test_tok", session=mock_session, max_retries=2)
    data = client.get_pr("owner/repo", 1)
    assert data["number"] == 1
    assert mock_session.request.call_count == 2


@pytest.mark.parametrize('status', [502, 503, 504])
def test_mutations_are_not_retried(status):
    session = MagicMock(spec=requests.Session)
    response = requests.Response()
    response.status_code = status
    response._content = b'failed'
    response._content_consumed = True
    session.request.return_value = response
    client = GitHubClient(session=session)
    with pytest.raises(APIError):
        client.request('POST', 'repos/owner/repo/actions/workflows/1/dispatches')
    session.request.assert_called_once()


def test_cross_origin_and_invalid_configuration():
    client = GitHubClient(token='secret')
    with pytest.raises(SecurityError):
        client.request('GET', 'https://attacker.example/path')
    with pytest.raises(ValidationError):
        GitHubClient(timeout=0)
    with pytest.raises(ValidationError):
        GitHubClient(max_retries=0)


def test_graphql_partial_errors_fail_closed():
    client = GitHubClient()
    client.request = MagicMock()
    client.request.return_value.json.return_value = {'data': {'repository': {}}, 'errors': [{'message': 'denied'}]}
    with pytest.raises(APIError):
        client.graphql('query { viewer { login } }')


def test_response_bound_is_explicit():
    session = MagicMock(spec=requests.Session)
    response = requests.Response()
    response.status_code = 200
    response._content = b'123456'
    response._content_consumed = True
    session.request.return_value = response
    client = GitHubClient(session=session, max_response_bytes=5)
    with pytest.raises(APIError) as err:
        client.request('GET', 'repos/owner/repo')
    assert err.value.diagnostic['classification'] == 'response_too_large'


def test_signed_log_redirect_has_no_credentials():
    session = MagicMock(spec=requests.Session)
    redirect = MagicMock(spec=requests.Response)
    redirect.status_code = 302
    redirect.headers = {'Location': 'https://logs.blob.core.windows.net/logs?signature=hidden'}
    download = requests.Response()
    download.status_code = 200
    download._content = b'logs'
    download._content_consumed = True
    session.request.side_effect = [redirect, download]
    result = GitHubClient(token='test-token', session=session).get_job_logs('owner/repo', 1)
    assert result == 'logs'
    headers = session.request.call_args_list[-1].kwargs['headers']
    assert 'Authorization' not in headers
    assert session.trust_env is False


def test_untrusted_redirect_rejected_before_request():
    session = MagicMock(spec=requests.Session)
    redirect = MagicMock(spec=requests.Response)
    redirect.status_code = 302
    redirect.headers = {'Location': 'https://attacker.example/path'}
    session.request.return_value = redirect
    with pytest.raises(SecurityError):
        GitHubClient(token='test-token', session=session).get_pr('owner/repo', 1)
    session.request.assert_called_once()


def test_rate_limit_429():
    session = MagicMock(spec=requests.Session)
    response = requests.Response()
    response.status_code = 429
    response._content = b'secondary limit'
    response._content_consumed = True
    response.headers['retry-after'] = '30'
    session.request.return_value = response
    with pytest.raises(APIError) as err:
        GitHubClient(session=session).get_pr('owner/repo', 1)
    assert err.value.diagnostic['retry_after'] == '30'
