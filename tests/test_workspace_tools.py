"""The workspace MCP tools through a real FastMCP client: contracts, annotations and the edit cycle."""
import asyncio
import contextlib

import pytest
from fastmcp import Client

from odytor.config import Settings, WorkspaceSettings
from odytor.server import create_server
from test_git_workspace import client_for, local_fetch, repository, run  # noqa: F401

WORKSPACE_TOOLS = {'workspace_create', 'workspace_read', 'workspace_search', 'workspace_edit', 'workspace_diff',
                   'workspace_run', 'workspace_manage', 'test_cancel', 'browser_run', 'browser_close', 'artifact_get', 'publish_plan', 'publish_apply'}
ORIGINAL_TOOLS = {'pr_evidence', 'ci_diagnose', 'quick_fix', 'pr_conflicts', 'pr_rebase', 'ci_control', 'test_run', 'test_result'}


@pytest.fixture
def stack(tmp_path, monkeypatch, local_fetch):
    repo, sha = local_fetch
    (repo / 'tests').mkdir()
    (repo / 'tests/test_a.py').write_text('def test_ok():\n    assert True\n')
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'tests')
    head = run(repo, 'rev-parse', 'HEAD').decode().strip()
    client = client_for(head)

    @contextlib.contextmanager
    def fake_client(cfg, remote=False, *, repo=None):
        yield client

    monkeypatch.setattr('odytor.server.github_client', fake_client)
    cfg = Settings(github_token='t', github_api_url='https://api.github.com', auth_token=None, default_max_diff_lines=500,
                   default_max_diff_bytes=1048576, allowed_repositories=('owner/repo',),
                   workspaces=WorkspaceSettings(data_dir=str(tmp_path / 'ws'), min_free_bytes=64 * 1024 * 1024))
    return create_server(cfg), head


def call(server, name, **args):
    async def go():
        async with Client(server) as client:
            result = await client.call_tool(name, args, raise_on_error=False)
            return result
    return asyncio.run(go())


def data(server, name, **args):
    result = call(server, name, **args)
    assert not result.is_error, result
    return result.structured_content


def test_catalogue_adds_workspace_tools_and_keeps_the_original_eight(stack):
    server, _ = stack

    async def tools():
        async with Client(server) as client:
            return {t.name: t for t in await client.list_tools()}
    listed = asyncio.run(tools())
    assert ORIGINAL_TOOLS <= listed.keys() and WORKSPACE_TOOLS <= listed.keys()
    read_only = {n for n, t in listed.items() if (t.annotations and t.annotations.read_only_hint)}
    assert {'workspace_read', 'workspace_search', 'workspace_diff', 'odytor_capabilities', 'artifact_get'} <= read_only
    # operations that change state, consume resources or delete are never presented as read-only
    assert not ({'workspace_create', 'workspace_edit', 'workspace_run', 'workspace_manage', 'test_cancel', 'browser_run', 'browser_close', 'publish_plan', 'publish_apply'} & read_only)
    assert listed['workspace_edit'].input_schema['required'] == ['workspace_id', 'expected_version', 'operation']
    assert listed['workspace_edit'].annotations.destructive_hint is False and listed['workspace_manage'].annotations.destructive_hint is True
    assert listed['workspace_create'].annotations.open_world_hint is True and listed['workspace_edit'].annotations.open_world_hint is False


def test_full_inspect_edit_diff_cycle_over_mcp(stack):
    server, head = stack
    created = data(server, 'workspace_create', repo='owner/repo', number=1, expected_head=head)
    w = created['workspace_id']
    assert created['version'] == 0 and created['source']['head_sha'] == head and created['modified'] is False
    tree = data(server, 'workspace_read', workspace_id=w, path='tests')
    assert [e['path'] for e in tree['entries']] == ['tests/test_a.py'] and tree['version'] == 0
    page = data(server, 'workspace_read', workspace_id=w, mode='file', path='tests/test_a.py')
    assert page['lines'][0]['text'] == 'def test_ok():' and page['tree'] == created['tree']
    hits = data(server, 'workspace_search', workspace_id=w, pattern='assert')
    assert hits['matches'][0]['path'] == 'tests/test_a.py' and hits['complete']
    edited = data(server, 'workspace_edit', workspace_id=w, expected_version=0, operation='write', path='tests/test_b.py',
                  content='def test_b():\n    assert 1 + 1 == 2\n')
    assert edited['version'] == 1 and edited['changed'] and edited['modified']
    diff = data(server, 'workspace_diff', workspace_id=w)
    assert [f['path'] for f in diff['files']] == ['tests/test_b.py'] and diff['patch_bytes'] > 0 and diff['tree'] == edited['tree']
    status = data(server, 'workspace_manage', action='status', workspace_id=w)
    assert status['version'] == 1 and status['modified'] is True
    assert [x['workspace_id'] for x in data(server, 'workspace_manage', action='list')['workspaces']] == [w]
    closed = data(server, 'workspace_manage', action='close', workspace_id=w)
    assert closed['closed'] and closed['unpublished_changes']
    assert 'error' in data(server, 'workspace_read', workspace_id=w, path='tests')


def test_stale_version_and_missing_arguments_are_actionable_errors(stack):
    server, head = stack
    w = data(server, 'workspace_create', repo='owner/repo', number=1, expected_head=head)['workspace_id']
    data(server, 'workspace_edit', workspace_id=w, expected_version=0, operation='write', path='x.txt', content='x\n')
    stale = data(server, 'workspace_edit', workspace_id=w, expected_version=0, operation='write', path='y.txt', content='y\n')
    assert stale['error_type'] == 'VersionConflictError' and stale['diagnostic']['current_version'] == 1
    missing = data(server, 'workspace_edit', workspace_id=w, expected_version=1, operation='replace', path='x.txt')
    assert 'requires: old, new' in missing['error']
    assert 'error' in data(server, 'workspace_edit', workspace_id=w, expected_version=1, operation='write', path='../escape', content='x')
    assert data(server, 'workspace_manage', action='status', workspace_id=w)['version'] == 1


def test_malformed_workspace_ids_are_rejected_by_the_schema(stack):
    server, _ = stack
    for bad in ('../../etc', 'ws-123', '/tmp/x', 'ws-' + 'g' * 32):
        assert call(server, 'workspace_read', workspace_id=bad, path='x').is_error


def test_repository_outside_the_allowlist_cannot_open_a_workspace(stack):
    server, head = stack
    result = call(server, 'workspace_create', repo='other/repo', number=1, expected_head=head)
    assert result.is_error and 'not permitted' in str(result)


def test_workspace_run_without_a_runner_explains_how_to_enable_it(stack):
    server, head = stack
    w = data(server, 'workspace_create', repo='owner/repo', number=1, expected_head=head)['workspace_id']
    out = data(server, 'workspace_run', workspace_id=w, expected_version=0)
    assert out['diagnostic']['classification'] == 'runner_not_configured'
    assert data(server, 'test_cancel', run_id='test-' + '0' * 32)['diagnostic']['classification'] == 'runner_not_configured'


def test_disabled_workspaces_say_so(tmp_path):
    cfg = Settings(github_token='t', github_api_url='https://api.github.com', auth_token=None, default_max_diff_lines=500,
                   default_max_diff_bytes=1048576, allowed_repositories=(), workspaces=None)
    server = create_server(cfg)
    assert 'disabled' in data(server, 'workspace_manage', action='list')['error']
    report = data(server, 'odytor_capabilities')
    workspaces = next(c for c in report['capabilities'] if c['name'] == 'workspaces')
    assert workspaces['configured'] is False and workspaces['available'] is False
