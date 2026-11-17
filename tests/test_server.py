"""Tests for FastMCP server tool registration and routing."""

import asyncio
import pytest
from git_plus.config import Settings
from git_plus.errors import SecurityError
from git_plus.server import create_server


def test_server_registration() -> None:
    settings = Settings(
        github_token="dummy_token",
        github_api_url="https://api.github.com",
        auth_token=None,
        default_max_diff_lines=500,
        default_max_diff_bytes=1048576,
        allowed_repositories=(),
    )
    server = create_server(settings)
    assert server.name == "Git Plus"
    tool = asyncio.run(server.get_tool("pr_evidence"))
    assert tool is not None
    assert tool.name == "pr_evidence"


def test_repo_allowlist_restriction() -> None:
    settings = Settings(
        github_token="dummy_token",
        github_api_url="https://api.github.com",
        auth_token=None,
        default_max_diff_lines=500,
        default_max_diff_bytes=1048576,
        allowed_repositories=("allowed/repo",),
    )
    server = create_server(settings)
    tool = asyncio.run(server.get_tool("pr_evidence"))
    assert tool is not None
    
    with pytest.raises(SecurityError) as exc_info:
        tool.fn(repo="forbidden/repo", number=1)

    assert "not permitted" in str(exc_info.value)


def test_all_recovered_schemas():
    from fastmcp import Client
    async def verify():
        async with Client(create_server()) as client:
            tools = {t.name: t for t in await client.list_tools()}
            assert {'pr_evidence', 'ci_diagnose', 'quick_fix'} <= tools.keys()
            assert tools['quick_fix'].input_schema['required'] == ['repo', 'number', 'expected_head', 'patch', 'commit_message']
    asyncio.run(verify())
