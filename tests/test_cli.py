"""Tests for transport selection in the command-line entry point."""

from unittest.mock import MagicMock, patch

from odytor import cli


def test_http_transport_uses_remote_server_and_streamable_http():
    settings = MagicMock(public_base_url=None)
    server = MagicMock()
    with patch.object(cli.Settings, "load", return_value=settings), patch.object(
        cli, "create_server", return_value=server
    ) as create:
        cli.main(["--transport", "http", "--port", "9123"])
    create.assert_called_once_with(settings, remote=True)
    server.run.assert_called_once_with(
        transport="streamable-http", host="127.0.0.1", port=9123, stateless_http=True,
        host_origin_protection=True, allowed_hosts=["127.0.0.1:9123"],
        allowed_origins=["http://127.0.0.1:9123"]
    )


def test_stdio_transport_uses_local_server():
    settings = MagicMock(github_token="t", allowed_repositories=("o/r",), testing=object())
    server = MagicMock()
    with patch.object(cli.Settings, "load", return_value=settings), patch.object(
        cli, "create_server", return_value=server
    ) as create:
        cli.main(["--transport", "stdio"])
    create.assert_called_once_with(settings)
    server.run.assert_called_once_with(transport="stdio")


def test_stdio_start_explains_missing_credential_open_allowlist_and_no_runner(capsys):
    settings = MagicMock(github_token=None, allowed_repositories=(), testing=None)
    with patch.object(cli.Settings, "load", return_value=settings), patch.object(cli, "create_server", return_value=MagicMock()):
        cli.main(["--transport", "stdio"])
    captured = capsys.readouterr()
    assert captured.out == ""  # stdout is reserved for the MCP protocol
    assert "GITHUB_TOKEN" in captured.err and "ALLOWED_REPOSITORIES" in captured.err and "isolated testing" in captured.err


def test_stdio_start_is_silent_when_fully_configured(capsys):
    settings = MagicMock(github_token="t", allowed_repositories=("o/r",), testing=object())
    with patch.object(cli.Settings, "load", return_value=settings), patch.object(cli, "create_server", return_value=MagicMock()):
        cli.main(["--transport", "stdio"])
    assert capsys.readouterr() == ("", "")


def test_http_start_prints_no_local_notes(capsys):
    settings = MagicMock(public_base_url=None, github_token=None, allowed_repositories=(), testing=None)
    with patch.object(cli.Settings, "load", return_value=settings), patch.object(cli, "create_server", return_value=MagicMock()):
        cli.main(["--transport", "http"])
    assert capsys.readouterr().err == ""
