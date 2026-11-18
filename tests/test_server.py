"""Tests for FastMCP server tool registration and routing."""

import asyncio
import pytest
from odytor.config import Settings
from odytor.errors import SecurityError
from odytor.server import create_server


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
    assert server.name == "Odytor"
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


def _local_settings(**overrides) -> Settings:
    values = dict(github_token="dummy_token", github_api_url="https://api.github.com", auth_token=None,
                  default_max_diff_lines=500, default_max_diff_bytes=1048576, allowed_repositories=())
    values.update(overrides)
    return Settings(**values)


def test_quick_fix_passes_configured_limits_to_the_applier(monkeypatch) -> None:
    from unittest.mock import MagicMock
    import odytor.server as server_module
    applier = MagicMock(return_value={"applied": True})
    monkeypatch.setattr(server_module, "apply_quick_fix", applier)
    server = create_server(_local_settings(quick_fix_max_files=12, quick_fix_max_changed_lines=1500,
                                           quick_fix_max_patch_bytes=300000))
    tool = asyncio.run(server.get_tool("quick_fix"))
    assert tool.fn(repo="o/r", number=1, expected_head="a" * 40, patch="p", commit_message="m") == {"applied": True}
    kwargs = applier.call_args.kwargs
    assert (kwargs["max_files"], kwargs["max_changed_lines"], kwargs["max_bytes"]) == (12, 1500, 300000)


def test_quick_fix_rejects_repositories_outside_the_allowlist() -> None:
    server = create_server(_local_settings(allowed_repositories=("allowed/repo",)))
    tool = asyncio.run(server.get_tool("quick_fix"))
    with pytest.raises(SecurityError):
        tool.fn(repo="forbidden/repo", number=1, expected_head="a" * 40, patch="p", commit_message="m")
